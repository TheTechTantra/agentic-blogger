"""Alembic migration environment for agentic-blogger."""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text
from alembic import context

config = context.config

# Load env vars
if config.get_section("sqlalchemy.url") is None:
    db_url = os.getenv("DATABASE_URL") or (
        f"postgresql://agentic_blogger:{os.getenv('POSTGRES_PASSWORD')}@"
        f"localhost:25432/blogger"
    )
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={}
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.begin() as connection:
        # Advisory lock to serialize concurrent migrations
        connection.execute(text("SELECT pg_advisory_lock(hashtext('agentic_blogger_migration'))"))

        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()

        connection.execute(text("SELECT pg_advisory_unlock(hashtext('agentic_blogger_migration'))"))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
