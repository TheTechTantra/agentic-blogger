"""Blogger publishing with beacon-based reconcile.

A crash between the API call and the checkpoint write leaves no record —
Blogger's posts.insert accepts no idempotency key. The beacon comment
embedded in the HTML by the format node is what survives into the remote
object and lets a retry tell "timed out but succeeded" from "actually failed".
"""

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry

from agentic_blogger.resilience import tenacity_kwargs
from agentic_blogger.secrets.store import SecretStore

logger = logging.getLogger(__name__)

# Google-side throttling was observed to surface as HTTP 400
# reason=badRequest ("Request contains an invalid argument") after heavy
# posts.insert traffic — not the 403/429 shape you'd normally filter on.
# Confirmed empirically: identical minimal payloads (even title+content,
# no labels) failed uniformly once request volume climbed, then the
# generic message is all Blogger gives back. Treat that shape as
# retryable alongside the standard rate-limit/server-error codes.
_RETRYABLE_HTTP_STATUS = {400, 403, 429, 500, 502, 503, 504}


def _describe(exc: HttpError) -> str:
    """Everything Blogger told us about a failure, on one line.

    The generic 400 body ("Request contains an invalid argument") names no
    field, so the reason code and the raw body are the only discriminators
    between a throttle and a malformed payload — and both were previously
    swallowed, leaving only the exception's repr in the traceback.
    """
    status = exc.resp.status if exc.resp is not None else "?"
    reason = getattr(exc.resp, "reason", "") if exc.resp is not None else ""
    try:
        body = (exc.content or b"").decode("utf-8", "replace")[:500]
    except Exception:
        body = "(undecodable)"
    return f"status={status} reason={reason!r} body={body}"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, InvalidGrantError):
        logger.error("Blogger grant invalid — not retryable, needs blogger_authorize.py")
        return False  # needs a human, not a retry
    if isinstance(exc, HttpError):
        retryable = exc.resp is not None and exc.resp.status in _RETRYABLE_HTTP_STATUS
        logger.warning("Blogger HttpError retryable=%s %s", retryable, _describe(exc))
        return retryable
    logger.warning("Blogger call raised non-retryable %s: %s", type(exc).__name__, exc)
    return False


class InvalidGrantError(Exception):
    """Refresh token expired/revoked — needs re-authorization (blogger_authorize.py)."""


def _service():
    store = SecretStore()
    creds = Credentials(
        token=None,
        refresh_token=store.get("BLOGGER_REFRESH_TOKEN"),
        client_id=store.get("BLOGGER_CLIENT_ID"),
        client_secret=store.get("BLOGGER_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"],
    )
    try:
        creds.refresh(Request())
    except Exception as e:
        if "invalid_grant" in str(e):
            logger.error("Blogger OAuth refresh rejected as invalid_grant")
            raise InvalidGrantError(str(e)) from e
        logger.error("Blogger OAuth refresh failed: %s: %s", type(e).__name__, e)
        raise
    # Expiry is the thing that silently kills this pipeline (the grant is on a
    # Testing-mode consent screen), so print it every publish rather than
    # discovering it from a 400 weeks later.
    logger.info("Blogger credentials refreshed, access token expires %s", creds.expiry)
    blog_id = store.get("BLOGGER_BLOG_ID")
    return build("blogger", "v3", credentials=creds, cache_discovery=False), blog_id


def _find_by_beacon(service, blog_id: str, job_id: str, title: str) -> dict | None:
    """Reconcile path: look for a DRAFT post already carrying this job's
    beacon (a prior attempt that succeeded remotely but crashed before we
    recorded it)."""
    beacon = f"agentic-blogger:job={job_id}"
    try:
        resp = service.posts().list(blogId=blog_id, status=["DRAFT"], fetchBodies=True,
                                     view="AUTHOR").execute()
    except HttpError as e:
        logger.warning("Reconcile list failed — proceeding to insert: %s", _describe(e))
        return None
    items = resp.get("items", [])
    for post in items:
        content = post.get("content", "")
        if beacon in content:
            logger.info("job=%s reconcile matched on beacon post_id=%s", job_id, post["id"])
            return post
        if post.get("title") == title:
            logger.info("job=%s reconcile matched on title post_id=%s", job_id, post["id"])
            return post
    logger.info("job=%s reconcile found no prior draft among %d existing drafts",
                job_id, len(items))
    return None


# Attempts and backoff come from the shared policy; the _is_retryable
# predicate stays here because it is specific to this call site — an expired
# OAuth grant must never be retried, and only certain HTTP statuses should be.
# Blogger enforces two independent caps on a post's labels: at most 20 of
# them, and at most 200 characters across the comma-joined set. The count cap
# is the one everybody remembers; the length cap is the one that bites, since
# the seo role reliably returns a full 20 descriptive labels and those run
# 250-330 characters. Over either cap, posts.insert returns a bare
# 400 badRequest "Request contains an invalid argument" naming no field.
#
# Labels are kept in the order the seo role emitted them (most relevant
# first), and dropped from the tail — a partial label would be worse than a
# missing one, so nothing is cut mid-string. What was dropped is logged at
# WARNING so the trimming is reviewable after the fact.
_MAX_LABELS = 20
_MAX_LABEL_CHARS = 200


def _fit_labels(job_id: str, labels: list[str]) -> list[str]:
    """Return the longest prefix of `labels` that Blogger will accept."""
    kept: list[str] = []
    used = 0
    for label in labels[:_MAX_LABELS]:
        # +1 for the comma Blogger counts between labels, except before the first.
        cost = len(label) + (1 if kept else 0)
        if used + cost > _MAX_LABEL_CHARS:
            break
        kept.append(label)
        used += cost

    dropped = labels[len(kept):]
    if dropped:
        logger.warning(
            "job=%s dropped %d of %d labels to fit Blogger's caps "
            "(%d labels / %d chars): %s",
            job_id, len(dropped), len(labels), _MAX_LABELS, _MAX_LABEL_CHARS,
            ", ".join(repr(d) for d in dropped),
        )
    return kept


@retry(**tenacity_kwargs(_is_retryable, logger))
def publish_draft(job_id: str, title: str, html: str, labels: list[str]) -> dict:
    """Insert (or adopt, via reconcile) a Blogger draft post. Returns
    {"remote_post_id", "remote_url", "state": "DRAFT"}.

    Retries the WHOLE reconcile-then-insert attempt, not just the insert
    call — each retry re-checks the beacon first, so a prior attempt that
    actually succeeded server-side (but errored client-side) is adopted
    instead of double-posted. num_retries=0 below still stands: it blocks
    googleapiclient's own blind re-POST; this decorator is the sanctioned
    retry path, and it goes through reconcile every time.
    """
    service, blog_id = _service()

    existing = _find_by_beacon(service, blog_id, job_id, title)
    if existing:
        logger.info("job=%s reconciled to existing draft post_id=%s", job_id, existing["id"])
        return {
            "remote_post_id": existing["id"],
            "remote_url": existing.get("url"),
            "state": "DRAFT",
            "api_response": existing,
        }

    fitted = _fit_labels(job_id, labels)
    body = {"title": title, "content": html, "labels": fitted}
    # The payload dimensions are what any future 400 investigation needs, and
    # they are not recoverable after the fact — the request is not stored.
    logger.info("job=%s inserting draft blog_id=%s title=%dch html=%dch labels=%d/%dch",
                job_id, blog_id, len(title), len(html), len(fitted),
                len(",".join(fitted)))
    # num_retries=0 is mandatory: googleapiclient's built-in retry does a
    # blind re-POST, which is exactly the double-post this design guards
    # against. Our reconcile *is* the retry.
    resp = service.posts().insert(blogId=blog_id, body=body, isDraft=True,
                                   fetchImages=False).execute(num_retries=0)

    logger.info("job=%s inserted post_id=%s url=%s", job_id, resp["id"], resp.get("url"))
    return {
        "remote_post_id": resp["id"],
        "remote_url": resp.get("url"),
        "state": "DRAFT",
        "api_response": resp,
    }
