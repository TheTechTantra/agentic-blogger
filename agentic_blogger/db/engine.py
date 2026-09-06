"""SQLAlchemy engine for the app schema (jobs, topics, etc.)."""

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "agentic_blogger")
    password = os.getenv("POSTGRES_PASSWORD")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "25432")
    db = os.getenv("POSTGRES_DB", "blogger")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(_database_url(), pool_pre_ping=True, future=True)


def psycopg_url() -> str:
    """Plain postgresql:// URL for psycopg v3 consumers (PostgresSaver)."""
    user = os.getenv("POSTGRES_USER", "agentic_blogger")
    password = os.getenv("POSTGRES_PASSWORD")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "25432")
    db = os.getenv("POSTGRES_DB", "blogger")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"
