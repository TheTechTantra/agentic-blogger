"""Database models and session management."""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# Get database URL from environment or construct from components
db_url = os.getenv(
    "DATABASE_URL",
    f"postgresql://agentic_blogger:{os.getenv('POSTGRES_PASSWORD', 'password')}"
    f"@localhost:25432/blogger",
)

engine = create_engine(
    db_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    """Get a database session."""
    return SessionLocal()


__all__ = ["engine", "SessionLocal", "get_session"]
