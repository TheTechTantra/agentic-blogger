#!/usr/bin/env python3
"""
Probe: does Blogger v3 let us control (or read early) a post's permalink?

Answers four questions the discovery document cannot:

  Q1  Does posts.insert honour a caller-supplied `url`?
  Q2  Does a DRAFT post come back with a `url` at all? (If yes, the whole
      predict-the-permalink problem disappears — insert draft, read the real
      URL, cross-link, then publish.)
  Q3  Does posts.patch let us change `url` on an existing draft?
  Q4  What are the slug derivation rules — punctuation, length, collisions?

Every post is created with isDraft=True and deleted in a finally block.
The one step that would make something publicly visible (Q5, publishing for
real to see whether publishDate drives the YYYY/MM path segment) is gated
behind --allow-publish and is off by default.

Usage:
    python -m scripts.probe_blogger_permalink
    python -m scripts.probe_blogger_permalink --allow-publish
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from agentic_blogger.secrets.store import SecretStore


def service_and_blog():
    store = SecretStore()
    creds = Credentials(
        token=None,
        refresh_token=store.get("BLOGGER_REFRESH_TOKEN"),
        client_id=store.get("BLOGGER_CLIENT_ID"),
        client_secret=store.get("BLOGGER_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"],
    )
    creds.refresh(Request())
    return build("blogger", "v3", credentials=creds, cache_discovery=False), store.get("BLOGGER_BLOG_ID")


def insert_draft(svc, blog_id, body, created):
    resp = svc.posts().insert(
        blogId=blog_id, body=body, isDraft=True, fetchImages=False
    ).execute(num_retries=0)
    created.append(resp["id"])
    return resp


def main(allow_publish: bool) -> int:
    svc, blog_id = service_and_blog()
    tag = uuid4().hex[:8]
    created: list[str] = []
    verdicts: dict[str, str] = {}

    try:
        # --- Q1: is `url` honoured on insert? -------------------------------
        wanted = f"https://example.invalid/2026/01/probe-desired-{tag}.html"
        r = insert_draft(svc, blog_id, {
            "title": f"[PROBE {tag}] Q1 supplied url",
            "content": "<p>probe</p>",
            "url": wanted,
        }, created)
        got = r.get("url")
        logger.info("\n--- Q1 insert with url= ---")
        logger.info("  requested : %s", wanted)
        logger.info("  returned  : %s", got)
        verdicts["Q1 insert honours url"] = "YES" if got == wanted else "NO (ignored)"

        # --- Q2: does a DRAFT expose a url at all? --------------------------
        r2 = insert_draft(svc, blog_id, {
            "title": f"[PROBE {tag}] Q2 plain draft",
            "content": "<p>probe</p>",
        }, created)
        logger.info("\n--- Q2 draft url visibility ---")
        logger.info("  insert response url : %r", r2.get("url"))
        logger.info("  insert response status: %r", r2.get("status"))
        got2 = svc.posts().get(
            blogId=blog_id, postId=r2["id"], view="AUTHOR"
        ).execute()
        logger.info("  posts.get(AUTHOR) url : %r", got2.get("url"))
        draft_url = got2.get("url") or r2.get("url")
        verdicts["Q2 draft exposes url"] = "YES" if draft_url else "NO"

        # --- Q3: can patch change the url? ----------------------------------
        patch_want = f"https://example.invalid/2026/01/probe-patched-{tag}.html"
        logger.info("\n--- Q3 patch url ---")
        try:
            r3 = svc.posts().patch(
                blogId=blog_id, postId=r2["id"], body={"url": patch_want},
                fetchBody=False,
            ).execute(num_retries=0)
            logger.info("  requested : %s", patch_want)
            logger.info("  returned  : %s", r3.get("url"))
            verdicts["Q3 patch honours url"] = "YES" if r3.get("url") == patch_want else "NO (ignored)"
        except HttpError as e:
            logger.info("  patch failed: %s", e)
            verdicts["Q3 patch honours url"] = f"ERROR {e.resp.status}"

        # --- Q4: slug derivation rules --------------------------------------
        logger.info("\n--- Q4 slug derivation ---")
        cases = {
            "punctuation": f"[PROBE {tag}] C++ & Rust: what's *really* different?",
            "very_long": f"[PROBE {tag}] " + " ".join(["kubernetes"] * 20),
            "stopwords": f"[PROBE {tag}] a an the of and to in for with on at",
            "unicode": f"[PROBE {tag}] Café naïve résumé 日本語 test",
        }
        for name, title in cases.items():
            rc = insert_draft(svc, blog_id, {"title": title, "content": "<p>probe</p>"}, created)
            u = svc.posts().get(
                blogId=blog_id, postId=rc["id"], view="AUTHOR"
            ).execute().get("url")
            logger.info("  %-12s title=%r", name, title[:60])
            logger.info("  %-12s url  =%r", "", u)

        # collision: same title twice
        dup_title = f"[PROBE {tag}] collision candidate"
        dup_urls = []
        for _ in range(2):
            rd = insert_draft(svc, blog_id, {"title": dup_title, "content": "<p>probe</p>"}, created)
            dup_urls.append(svc.posts().get(
                blogId=blog_id, postId=rd["id"], view="AUTHOR"
            ).execute().get("url"))
        logger.info("  %-12s %r", "collision", dup_urls)
        verdicts["Q4 collision suffixed"] = "YES" if dup_urls[0] != dup_urls[1] else "NO (identical!)"

        # --- Q5: does publishDate drive the YYYY/MM path segment? -----------
        logger.info("\n--- Q5 publishDate vs url path ---")
        if not allow_publish:
            logger.info("  SKIPPED (re-run with --allow-publish; makes a post briefly live)")
            verdicts["Q5 publishDate drives path"] = "SKIPPED"
        else:
            rp = insert_draft(svc, blog_id, {
                "title": f"[PROBE {tag}] Q5 publish date",
                "content": "<p>probe</p>",
            }, created)
            pre = svc.posts().get(
                blogId=blog_id, postId=rp["id"], view="AUTHOR"
            ).execute().get("url")
            future = (datetime.now(timezone.utc) + timedelta(days=95)).replace(microsecond=0)
            pub = svc.posts().publish(
                blogId=blog_id, postId=rp["id"],
                publishDate=future.isoformat().replace("+00:00", "Z"),
            ).execute(num_retries=0)
            logger.info("  draft url      : %r", pre)
            logger.info("  publishDate    : %s", future.isoformat())
            logger.info("  published url  : %r", pub.get("url"))
            logger.info("  published stat : %r", pub.get("status"))
            verdicts["Q5 url stable across publish"] = "YES" if pre == pub.get("url") else "NO (changed)"

        logger.info("\n================ VERDICTS ================")
        for k, v in verdicts.items():
            logger.info("  %-30s %s", k, v)
        logger.info("==========================================")
        return 0

    finally:
        logger.info("\nCleaning up %d probe post(s)...", len(created))
        for pid in created:
            try:
                svc.posts().delete(blogId=blog_id, postId=pid).execute(num_retries=0)
            except Exception as e:
                logger.error("  FAILED to delete post %s: %s -- delete it by hand", pid, e)
            else:
                logger.info("  deleted %s", pid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-publish", action="store_true",
                    help="run Q5, which briefly publishes a real post")
    sys.exit(main(ap.parse_args().allow_publish))
