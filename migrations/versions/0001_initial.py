"""Initial schema: jobs, topics, sources, drafts, publications, video tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-05 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create schema if not exists
    op.execute("CREATE SCHEMA IF NOT EXISTS app")

    # Create enum types (idempotent via DO block)
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'job_state') THEN
            CREATE TYPE app.job_state AS ENUM (
                'QUEUED', 'RUNNING', 'PUBLISHED', 'FAILED', 'BLOCKED', 'CANCELLED');
        END IF;
    END $$;
    """)

    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'pub_state') THEN
            CREATE TYPE app.pub_state AS ENUM (
                'PENDING', 'DRAFT', 'LIVE', 'FAILED');
        END IF;
    END $$;
    """)

    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'pipeline_kind') THEN
            CREATE TYPE app.pipeline_kind AS ENUM (
                'blog', 'video');
        END IF;
    END $$;
    """)

    # Topics table
    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized", sa.Text(), nullable=False),
        sa.Column("topic_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        schema="app"
    )
    op.create_index("ix_topics_topic_hash", "topics", ["topic_hash"], schema="app")

    # Jobs table
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pipeline", sa.Text(), nullable=False, server_default="blog"),
        sa.Column("state", postgresql.ENUM("QUEUED", "RUNNING", "PUBLISHED", "FAILED", "BLOCKED", "CANCELLED", name="job_state", schema="app", create_type=False), nullable=False, server_default="QUEUED"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column("config_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("failure_class", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["topic_id"], ["app.topics.id"]),
        sa.ForeignKeyConstraint(["parent_job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        schema="app"
    )
    op.create_index("ix_jobs_claim", "jobs", ["priority", "created_at"],
                    postgresql_where=sa.text("state IN ('QUEUED','RUNNING')"),
                    schema="app")

    # Node runs table (audit + cost ledger)
    op.create_table(
        "node_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("mlflow_span_id", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        schema="app"
    )

    # Research sources table
    op.create_table(
        "research_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Date(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_sha256", sa.Text(), nullable=True),
        sa.Column("content_chars", sa.Integer(), nullable=True),
        sa.Column("extract_path", sa.Text(), nullable=True),
        sa.Column("search_provider", sa.Text(), nullable=False),
        sa.Column("search_query", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("used_in_draft", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("credibility", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "url"),
        schema="app"
    )

    # Drafts table
    op.create_table(
        "drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("outline_json", postgresql.JSONB(), nullable=True),
        sa.Column("seo_json", postgresql.JSONB(), nullable=True),
        sa.Column("produced_by", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "version"),
        schema="app"
    )

    # Revisions table
    op.create_table(
        "revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("findings_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_draft_id"], ["app.drafts.id"]),
        sa.ForeignKeyConstraint(["to_draft_id"], ["app.drafts.id"]),
        schema="app"
    )

    # Publications table
    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="blogger"),
        sa.Column("blog_id", sa.Text(), nullable=False),
        sa.Column("remote_post_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("state", postgresql.ENUM("PENDING", "DRAFT", "LIVE", "FAILED", name="pub_state", schema="app", create_type=False), nullable=False, server_default="PENDING"),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("api_response", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_id"], ["app.drafts.id"]),
        sa.UniqueConstraint("job_id", "platform"),
        sa.UniqueConstraint("platform", "remote_post_id"),
        schema="app"
    )

    # Phase 2: Video tables
    op.create_table(
        "video_scripts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scenes_json", postgresql.JSONB(), nullable=False),
        sa.Column("total_duration_s", sa.Numeric(8, 2), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_id"], ["app.drafts.id"]),
        sa.UniqueConstraint("job_id", "version"),
        schema="app"
    )

    op.create_table(
        "video_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("script_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scene_index", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Numeric(8, 2), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_id"], ["app.video_scripts.id"]),
        schema="app"
    )

    op.create_table(
        "video_renders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("script_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("renderer", sa.Text(), nullable=False, server_default="ffmpeg"),
        sa.Column("output_uri", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Numeric(8, 2), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("ffmpeg_cmd", sa.Text(), nullable=True),
        sa.Column("ffmpeg_log_path", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_id"], ["app.video_scripts.id"]),
        schema="app"
    )

    op.create_table(
        "video_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.func.gen_random_uuid()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("render_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False, server_default="youtube"),
        sa.Column("remote_video_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("privacy_status", sa.Text(), nullable=False, server_default="private"),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("state", postgresql.ENUM("PENDING", "DRAFT", "LIVE", "FAILED", name="pub_state", schema="app", create_type=False), nullable=False, server_default="PENDING"),
        sa.Column("api_response", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["app.jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["render_id"], ["app.video_renders.id"]),
        sa.UniqueConstraint("job_id", "platform"),
        sa.UniqueConstraint("platform", "remote_video_id"),
        schema="app"
    )


def downgrade() -> None:
    op.drop_table("video_publications", schema="app")
    op.drop_table("video_renders", schema="app")
    op.drop_table("video_assets", schema="app")
    op.drop_table("video_scripts", schema="app")
    op.drop_table("publications", schema="app")
    op.drop_table("revisions", schema="app")
    op.drop_table("drafts", schema="app")
    op.drop_table("research_sources", schema="app")
    op.drop_table("node_runs", schema="app")
    op.drop_table("jobs", schema="app")
    op.drop_table("topics", schema="app")
    op.execute("DROP TYPE app.pipeline_kind")
    op.execute("DROP TYPE app.pub_state")
    op.execute("DROP TYPE app.job_state")
    op.execute("DROP SCHEMA app")
