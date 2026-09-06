#!/usr/bin/env python3
"""
One-time OAuth authorization for Blogger API access.

Must run ON THE HOST (not in Docker) because it opens a browser.
Prompts for OAuth consent and stores the refresh token in TinyDB.

Usage:
  python3 scripts/blogger_authorize.py

This creates a local server on port 0 (random unused port) and opens the
browser to Google's OAuth consent screen. After consent, the refresh token
is extracted and stored in TinyDB under BLOGGER_REFRESH_TOKEN.

IMPORTANT: Before running:
  1. Create a Desktop OAuth client (not Web) in Google Cloud Console
  2. Download the client_secret_*.json file
  3. Place it in this directory as client_secret.json
  4. Ensure TinyDB service is running (docker compose up -d tinydb)
  5. Ensure .env is set with TinyDB secrets
"""

import os
import sys
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError as e:
    logger.error(f"Google Auth import failed: {e}")
    logger.error("Install: pip install google-auth-oauthlib google-auth-httplib2")
    sys.exit(1)

# Import secrets store
try:
    from agentic_blogger.secrets.store import SecretStore
except ImportError as e:
    logger.error(f"Failed to import SecretStore: {e}")
    logger.error("Make sure .env is configured and TinyDB is running")
    sys.exit(1)


def authorize_blogger():
    """Run OAuth flow and store refresh token."""
    logger.info("=== Blogger OAuth Authorization ===\n")

    # Check for client_secret.json
    client_secret_path = Path("client_secret.json")
    if not client_secret_path.exists():
        logger.error(f"client_secret.json not found")
        logger.error("\nHow to fix:")
        logger.error("  1. Go to Google Cloud Console")
        logger.error("  2. Create a Desktop OAuth 2.0 client")
        logger.error("  3. Download the credentials JSON")
        logger.error("  4. Rename to client_secret.json in this directory")
        sys.exit(1)

    logger.info(f"Using client_secret.json from {client_secret_path.absolute()}")

    # Define scopes
    SCOPES = ["https://www.googleapis.com/auth/blogger"]

    # Run the OAuth flow
    logger.info("\nStarting OAuth flow...")
    logger.info("A browser window will open asking for consent.")
    logger.info("After consent, copy the authorization code if prompted.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path),
            scopes=SCOPES,
        )

        # Run local server on a random port
        # prompt="consent" ensures we get a refresh token on re-auth
        creds = flow.run_local_server(
            port=0,  # Use any available port
            access_type="offline",
            prompt="consent",  # Force consent to get refresh_token
        )

        logger.info("✓ Authorization successful")
    except Exception as e:
        logger.error(f"OAuth flow failed: {e}")
        sys.exit(1)

    # Extract tokens
    if not creds.refresh_token:
        logger.error("No refresh token in credentials!")
        logger.error("Ensure prompt='consent' was used in the flow.")
        sys.exit(1)

    refresh_token = creds.refresh_token
    logger.info(f"Refresh token: {refresh_token[:20]}...")

    # Store in TinyDB
    logger.info("\nStoring credentials in TinyDB...")
    store = SecretStore()

    try:
        store.put("BLOGGER_REFRESH_TOKEN", refresh_token)
        logger.info("✓ BLOGGER_REFRESH_TOKEN stored")
    except Exception as e:
        logger.error(f"Failed to store token: {e}")
        sys.exit(1)

    # Also extract and store client info if not already present
    try:
        with open(client_secret_path) as f:
            client_config = json.load(f)

        installed = client_config.get("installed", {})
        client_id = installed.get("client_id")
        client_secret = installed.get("client_secret")

        if client_id:
            try:
                store.put("BLOGGER_CLIENT_ID", client_id)
                logger.info("✓ BLOGGER_CLIENT_ID stored")
            except Exception as e:
                logger.warning(f"Could not store client_id: {e}")

        if client_secret:
            try:
                store.put("BLOGGER_CLIENT_SECRET", client_secret)
                logger.info("✓ BLOGGER_CLIENT_SECRET stored")
            except Exception as e:
                logger.warning(f"Could not store client_secret: {e}")
    except Exception as e:
        logger.warning(f"Could not extract client info: {e}")

    logger.info("\n=== Authorization Complete ===")
    logger.info("Next steps:")
    logger.info("  1. Get your Blog ID from https://www.blogger.com")
    logger.info("  2. Run: scripts/seed_secrets.sh")
    logger.info("  3. Enter BLOGGER_BLOG_ID when prompted")

    return True


if __name__ == "__main__":
    try:
        success = authorize_blogger()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
