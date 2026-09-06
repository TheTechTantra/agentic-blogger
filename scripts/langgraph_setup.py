#!/usr/bin/env python3
"""
Initialize PostgresSaver checkpoint tables in the blogger database.

Runs once, in step 4 of the build order, after alembic upgrade head.
LangGraph manages these tables independently (not via Alembic).

Usage:
  docker compose run --rm orchestrator python -m scripts.langgraph_setup
"""

import os
import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from langgraph.checkpoint.postgres import PostgresSaver
except ImportError as e:
    logger.error(f"LangGraph import failed: {e}")
    logger.error("Verify requirements.txt: langgraph-checkpoint-postgres installed")
    sys.exit(1)


def _psycopg_url() -> str:
    """Build a plain postgresql:// URL — PostgresSaver uses psycopg v3,
    which doesn't take the '+psycopg2' SQLAlchemy driver suffix."""
    user = os.getenv("POSTGRES_USER", "agentic_blogger")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD not set")
        sys.exit(1)
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "25432")
    db = os.getenv("POSTGRES_DB", "blogger")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def setup_langgraph():
    """Initialize PostgresSaver tables."""
    logger.info("=== LangGraph PostgresSaver Setup ===")

    postgres_url = _psycopg_url()
    logger.info(f"Database: {postgres_url.split('@')[1] if '@' in postgres_url else '...'}")

    # Call setup() to create checkpoint tables
    logger.info("Calling PostgresSaver.setup()...")
    try:
        with PostgresSaver.from_conn_string(postgres_url) as saver:
            saver.setup()
        logger.info("✓ PostgresSaver tables created (or already present — setup() is idempotent)")
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        sys.exit(1)

    logger.info("\n=== Setup Complete ===")
    logger.info("LangGraph checkpoint tables are ready:")
    logger.info("  - public.checkpoint")
    logger.info("  - public.checkpoint_writes")
    logger.info("  - public.checkpoint_blobs")

    return True


if __name__ == "__main__":
    try:
        success = setup_langgraph()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
