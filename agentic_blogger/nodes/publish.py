"""Publish node — no LLM call. Commits a PENDING publication row before
touching Google (the idempotency anchor), then inserts or reconciles."""

import logging

from agentic_blogger.db import repo
from agentic_blogger.publishing.blogger_client import InvalidGrantError, publish_draft
from agentic_blogger.secrets.store import SecretStore

logger = logging.getLogger(__name__)


class JobBlocked(Exception):
    """Raised for failures that need a human, not a retry (e.g. expired OAuth grant)."""


def publish_node(state: dict) -> dict:
    job_id = state["job_id"]
    draft_id = state["draft_id"]
    seo = state.get("seo_meta") or {}
    title = seo.get("title") or state.get("draft_title", "")
    html = state.get("html", "")
    labels = seo.get("labels", [])

    existing = repo.get_pending_publication(job_id)
    if existing and existing["state"] in ("DRAFT", "LIVE"):
        logger.info("job=%s already published (state=%s) — skipping", job_id, existing["state"])
        return {"published": {
            "remote_post_id": existing["remote_post_id"],
            "remote_url": existing["remote_url"],
            "state": existing["state"],
        }}

    blog_id = SecretStore().get("BLOGGER_BLOG_ID")
    repo.insert_publication_intent(job_id, draft_id, blog_id, request_id=job_id)

    try:
        result = publish_draft(job_id, title, html, labels)
    except InvalidGrantError as e:
        repo.update_publication(job_id, state="FAILED", error_message=str(e))
        raise JobBlocked(
            "Blogger refresh token expired/revoked — re-run scripts/blogger_authorize.py"
        ) from e
    except Exception as e:
        repo.update_publication(job_id, state="FAILED", error_message=str(e))
        raise

    repo.update_publication(
        job_id, state=result["state"], remote_post_id=result["remote_post_id"],
        remote_url=result["remote_url"], is_draft=True, api_response=result.get("api_response"),
    )

    return {"published": {
        "remote_post_id": result["remote_post_id"],
        "remote_url": result["remote_url"],
        "state": result["state"],
    }}
