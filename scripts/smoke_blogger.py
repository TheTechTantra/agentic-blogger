#!/usr/bin/env python3
"""
Smoke test: Blogger API integration.

Tests that:
  - OAuth refresh token works
  - Can insert a draft post
  - Can delete the post
  - isDraft parameter works correctly
"""

import os
import sys
import logging
import time
from uuid import uuid4

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google.api_core.gapic_v1 import client_info as grpc_client_info
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError as e:
    logger.error(f"Google API import failed: {e}")
    logger.error("Verify google-auth, google-auth-oauthlib, google-api-python-client are installed")
    sys.exit(1)

# Import secrets store
try:
    from agentic_blogger.secrets.store import SecretStore
except ImportError as e:
    logger.error(f"Failed to import SecretStore: {e}")
    sys.exit(1)


def get_credentials() -> Credentials:
    """Get OAuth credentials from TinyDB refresh token."""
    store = SecretStore()

    try:
        client_id = store.get("BLOGGER_CLIENT_ID")
        client_secret = store.get("BLOGGER_CLIENT_SECRET")
        refresh_token = store.get("BLOGGER_REFRESH_TOKEN")
    except Exception as e:
        logger.error(f"Failed to fetch credentials from TinyDB: {e}")
        logger.error("Run ./scripts/blogger_authorize.py first")
        sys.exit(1)

    if not all([client_id, client_secret, refresh_token]):
        logger.error("Missing one or more credentials in TinyDB")
        logger.error("Required: BLOGGER_CLIENT_ID, BLOGGER_CLIENT_SECRET, BLOGGER_REFRESH_TOKEN")
        sys.exit(1)

    logger.info(f"Loaded client_id: {client_id[:20]}...")
    logger.info(f"Loaded refresh_token: {refresh_token[:20]}...")

    # Create credentials object
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/blogger"],
    )

    # Refresh to get access token
    logger.info("Refreshing OAuth token...")
    try:
        creds.refresh(Request())
        logger.info("✓ Token refreshed successfully")
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        if "invalid_grant" in str(e):
            logger.error("Refresh token may have expired (7-day testing mode)")
            logger.error("Run ./scripts/blogger_authorize.py again")
        sys.exit(1)

    return creds


def test_blogger_api():
    """Test Blogger API: insert, verify, delete."""
    logger.info("=== Smoke Test: Blogger API ===")

    # Get credentials
    creds = get_credentials()

    # Get blog_id from TinyDB
    store = SecretStore()
    blog_id = store.get("BLOGGER_BLOG_ID")
    if not blog_id:
        logger.error("BLOGGER_BLOG_ID not set in TinyDB")
        sys.exit(1)

    logger.info(f"Blog ID: {blog_id}")

    # Build Blogger service
    logger.info("Building Blogger service...")
    service = build("blogger", "v3", credentials=creds)

    # Generate unique title (to avoid conflicts if test runs multiple times)
    unique_id = uuid4().hex[:8]
    title = f"[SMOKE TEST {unique_id}] Kubernetes Operators"
    content = f"""<h2>Test Post</h2>
<p>This is a test post created by smoke_blogger.py at {time.time()}</p>
<p>Unique ID: {unique_id}</p>
<!-- agentic-blogger:job=smoke-test-{unique_id} v=1 -->"""

    logger.info(f"Inserting draft post: {title}")
    try:
        response = service.posts().insert(
            blogId=blog_id,
            body={
                "title": title,
                "content": content,
                "labels": ["smoke-test", f"smoke-{unique_id}"],
            },
            isDraft=True,
            fetchImages=False,
        ).execute(num_retries=0)

        post_id = response.get("id")
        post_status = response.get("status")
        logger.info(f"✓ Post created")
        logger.info(f"  Post ID: {post_id}")
        logger.info(f"  Status: {post_status}")

        if post_status != "DRAFT":
            logger.warning(f"Expected status=DRAFT, got {post_status}")
    except HttpError as e:
        logger.error(f"Failed to insert post: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error during insert: {e}")
        sys.exit(1)

    # Verify post exists
    logger.info(f"Retrieving post {post_id}...")
    try:
        # view='AUTHOR' is required: the default READER view cannot see drafts
        post = service.posts().get(
            blogId=blog_id,
            postId=post_id,
            view="AUTHOR",
        ).execute()

        retrieved_title = post.get("title")
        retrieved_status = post.get("status")
        logger.info(f"✓ Post retrieved")
        logger.info(f"  Title: {retrieved_title}")
        logger.info(f"  Status: {retrieved_status}")

        if retrieved_status != "DRAFT":
            logger.error(f"Post status is {retrieved_status}, expected DRAFT")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to retrieve post: {e}")
        sys.exit(1)

    # Delete post
    logger.info(f"Deleting post {post_id}...")
    try:
        service.posts().delete(
            blogId=blog_id,
            postId=post_id,
        ).execute(num_retries=0)
        logger.info("✓ Post deleted successfully")
    except Exception as e:
        logger.error(f"Failed to delete post: {e}")
        sys.exit(1)

    # Verify deletion
    logger.info(f"Verifying deletion...")
    try:
        post = service.posts().get(
            blogId=blog_id,
            postId=post_id,
            view="AUTHOR",
        ).execute()
        logger.error(f"Post still exists after deletion!")
        sys.exit(1)
    except HttpError as e:
        if e.resp.status == 404:
            logger.info("✓ Post confirmed deleted (404)")
        else:
            logger.error(f"Unexpected HTTP error: {e}")
            sys.exit(1)

    logger.info("\n=== SMOKE TEST PASSED ===")
    logger.info("Blogger API integration is working:")
    logger.info("  ✓ OAuth refresh succeeded")
    logger.info("  ✓ Draft post created with isDraft=True")
    logger.info("  ✓ Post retrieved successfully")
    logger.info("  ✓ Post deleted successfully")
    logger.info("  ✓ Deletion verified (404)")

    return True


if __name__ == "__main__":
    try:
        success = test_blogger_api()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
