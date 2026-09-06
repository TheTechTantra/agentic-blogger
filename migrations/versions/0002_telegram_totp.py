"""Telegram TOTP replay-protection state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06 12:00:00.000000

Plain table, no enum types — unlike 0001, this needs no idempotency
workaround: CREATE TABLE IF NOT EXISTS works fine for ordinary tables.
"""
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS app.telegram_totp_state (
            user_id text PRIMARY KEY,
            last_step bigint NOT NULL DEFAULT 0,
            failed_attempts int NOT NULL DEFAULT 0,
            locked_until timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.drop_table("telegram_totp_state", schema="app")
