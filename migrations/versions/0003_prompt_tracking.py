"""Record which registry prompts produced each node run.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09 12:00:00.000000

One column, not three. Prompts resolve per node under an alias, and a single
node sends several of them (a system prompt, a user prompt, whatever
conditional fragments applied), so "the prompt version for this run" is a list
rather than a scalar. Shape:

    [{"name": "draft_user", "version": 7, "alias": "production"}, ...]

The alias is stored per entry rather than once on the job because alias
resolution happens per node — two nodes in one job can legitimately see
different versions if a prompt is republished mid-run.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.node_runs
        ADD COLUMN IF NOT EXISTS prompts_json jsonb
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE app.node_runs DROP COLUMN IF EXISTS prompts_json")
