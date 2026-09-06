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
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

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


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, InvalidGrantError):
        return False  # needs a human, not a retry
    if isinstance(exc, HttpError):
        return exc.resp is not None and exc.resp.status in _RETRYABLE_HTTP_STATUS
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
            raise InvalidGrantError(str(e)) from e
        raise
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
        logger.warning("Reconcile list failed (%s) — proceeding to insert", e)
        return None
    for post in resp.get("items", []):
        content = post.get("content", "")
        if beacon in content or post.get("title") == title:
            return post
    return None


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
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

    body = {"title": title, "content": html, "labels": labels[:20]}
    # num_retries=0 is mandatory: googleapiclient's built-in retry does a
    # blind re-POST, which is exactly the double-post this design guards
    # against. Our reconcile *is* the retry.
    resp = service.posts().insert(blogId=blog_id, body=body, isDraft=True,
                                   fetchImages=False).execute(num_retries=0)

    return {
        "remote_post_id": resp["id"],
        "remote_url": resp.get("url"),
        "state": "DRAFT",
        "api_response": resp,
    }
