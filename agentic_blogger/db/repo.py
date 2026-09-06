"""Repository functions over the app schema — raw SQL via SQLAlchemy Core.

No ORM mapping: the schema is simple enough that hand-written SQL is more
direct than maintaining a parallel set of ORM models. Every function takes
or opens a connection from get_engine().
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import text

from agentic_blogger.db.engine import get_engine

logger = logging.getLogger(__name__)


def _hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def create_topic_and_job(
    raw_text: str,
    *,
    source: str,
    requested_by: Optional[str],
    pipeline: str = "blog",
    config_snapshot: dict,
) -> dict:
    """Create (or reuse) a topic, then create a job for it.

    Returns {"job_id": ..., "reused": bool} — reused=True means an
    idempotency-key collision found an existing job instead of inserting.
    """
    normalized = " ".join(raw_text.strip().lower().split())
    topic_hash = _hash(normalized)
    config_version = str(config_snapshot.get("config_version", "v1"))
    idempotency_key = _hash(topic_hash, pipeline, config_version)

    engine = get_engine()
    with engine.begin() as conn:
        existing_job = conn.execute(
            text("SELECT id, state FROM app.jobs WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        ).mappings().first()
        if existing_job:
            return {"job_id": str(existing_job["id"]), "reused": True, "state": existing_job["state"]}

        topic_row = conn.execute(
            text("SELECT id FROM app.topics WHERE topic_hash = :h"),
            {"h": topic_hash},
        ).mappings().first()
        if topic_row:
            topic_id = topic_row["id"]
        else:
            topic_id = uuid4()
            conn.execute(
                text(
                    "INSERT INTO app.topics (id, raw_text, normalized, topic_hash, source, requested_by) "
                    "VALUES (:id, :raw_text, :normalized, :topic_hash, :source, :requested_by)"
                ),
                {
                    "id": topic_id,
                    "raw_text": raw_text,
                    "normalized": normalized,
                    "topic_hash": topic_hash,
                    "source": source,
                    "requested_by": requested_by,
                },
            )

        job_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO app.jobs (id, topic_id, pipeline, idempotency_key, thread_id, "
                "config_snapshot, telegram_chat_id) "
                "VALUES (:id, :topic_id, :pipeline, :idempotency_key, :thread_id, "
                ":config_snapshot, :telegram_chat_id)"
            ),
            {
                "id": job_id,
                "topic_id": topic_id,
                "pipeline": pipeline,
                "idempotency_key": idempotency_key,
                "thread_id": str(job_id),
                "config_snapshot": json.dumps(config_snapshot),
                "telegram_chat_id": config_snapshot.get("telegram_chat_id"),
            },
        )
        return {"job_id": str(job_id), "reused": False, "state": "QUEUED"}


def claim_job(worker_id: str, lease_minutes: int = 15) -> Optional[dict]:
    """Claim one QUEUED job, or a RUNNING job whose lease expired."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE app.jobs SET state='RUNNING', lease_owner=:worker_id,
                       lease_expires_at = now() + make_interval(mins => :lease_minutes),
                       attempt = attempt + 1,
                       started_at = COALESCE(started_at, now()),
                       updated_at = now()
                WHERE id = (
                  SELECT id FROM app.jobs
                   WHERE (state = 'QUEUED' OR (state = 'RUNNING' AND lease_expires_at < now()))
                     AND cancel_requested = false
                   ORDER BY priority, created_at
                   FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING id, topic_id, pipeline, attempt, max_attempts, config_snapshot, thread_id
                """
            ),
            {"worker_id": worker_id, "lease_minutes": lease_minutes},
        ).mappings().first()
        if not row:
            return None
        topic = conn.execute(
            text("SELECT raw_text FROM app.topics WHERE id = :id"),
            {"id": row["topic_id"]},
        ).mappings().first()
        return {
            "job_id": str(row["id"]),
            "thread_id": row["thread_id"],
            "topic": topic["raw_text"] if topic else "",
            "attempt": row["attempt"],
            "max_attempts": row["max_attempts"],
            "config_snapshot": row["config_snapshot"],
        }


def heartbeat_lease(job_id: str, lease_minutes: int = 15) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app.jobs SET lease_expires_at = now() + make_interval(mins => :m), "
                "updated_at = now() WHERE id = :id"
            ),
            {"m": lease_minutes, "id": job_id},
        )


def mark_job_state(
    job_id: str,
    state: str,
    *,
    failure_class: Optional[str] = None,
    failure_reason: Optional[str] = None,
    mlflow_run_id: Optional[str] = None,
) -> None:
    engine = get_engine()
    terminal = state in ("PUBLISHED", "FAILED", "BLOCKED", "CANCELLED")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE app.jobs SET state = :state,
                       failure_class = COALESCE(:failure_class, failure_class),
                       failure_reason = COALESCE(:failure_reason, failure_reason),
                       mlflow_run_id = COALESCE(:mlflow_run_id, mlflow_run_id),
                       finished_at = CASE WHEN :terminal THEN now() ELSE finished_at END,
                       updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "state": state,
                "failure_class": failure_class,
                "failure_reason": failure_reason,
                "mlflow_run_id": mlflow_run_id,
                "terminal": terminal,
                "id": job_id,
            },
        )


def add_job_cost(job_id: str, cost_usd: Decimal, tokens_in: int, tokens_out: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app.jobs SET cost_usd = cost_usd + :cost, tokens_in = tokens_in + :ti, "
                "tokens_out = tokens_out + :to_, updated_at = now() WHERE id = :id"
            ),
            {"cost": cost_usd, "ti": tokens_in, "to_": tokens_out, "id": job_id},
        )


def record_node_run(
    job_id: str,
    node_name: str,
    *,
    attempt: int = 0,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    role: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[Decimal] = None,
    latency_ms: Optional[int] = None,
    mlflow_span_id: Optional[str] = None,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.node_runs
                    (id, job_id, node_name, attempt, provider, model, role,
                     tokens_in, tokens_out, cost_usd, latency_ms, mlflow_span_id,
                     error_class, error_message, started_at, finished_at)
                VALUES
                    (:id, :job_id, :node_name, :attempt, :provider, :model, :role,
                     :tokens_in, :tokens_out, :cost_usd, :latency_ms, :mlflow_span_id,
                     :error_class, :error_message, :started_at, :finished_at)
                """
            ),
            {
                "id": uuid4(),
                "job_id": job_id,
                "node_name": node_name,
                "attempt": attempt,
                "provider": provider,
                "model": model,
                "role": role,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "latency_ms": latency_ms,
                "mlflow_span_id": mlflow_span_id,
                "error_class": error_class,
                "error_message": error_message,
                "started_at": started_at,
                "finished_at": finished_at,
            },
        )


def insert_research_sources(job_id: str, sources: list[dict], search_provider: str, search_query: str) -> None:
    if not sources:
        return
    engine = get_engine()
    with engine.begin() as conn:
        for rank, s in enumerate(sources):
            conn.execute(
                text(
                    """
                    INSERT INTO app.research_sources
                        (id, job_id, url, title, snippet, search_provider, search_query, rank)
                    VALUES (:id, :job_id, :url, :title, :snippet, :search_provider, :search_query, :rank)
                    ON CONFLICT (job_id, url) DO NOTHING
                    """
                ),
                {
                    "id": uuid4(),
                    "job_id": job_id,
                    "url": s.get("url", ""),
                    "title": s.get("title"),
                    "snippet": s.get("snippet"),
                    "search_provider": search_provider,
                    "search_query": search_query,
                    "rank": rank,
                },
            )


def insert_draft(
    job_id: str,
    version: int,
    title: str,
    markdown: str,
    *,
    html: Optional[str] = None,
    word_count: Optional[int] = None,
    outline_json: Optional[dict] = None,
    seo_json: Optional[dict] = None,
    produced_by: str,
) -> str:
    # Idempotent by (job_id, version): a node-level RetryPolicy re-running
    # draft_node after a transient failure past this insert must not collide
    # on the unique constraint — upsert instead of a bare insert.
    engine = get_engine()
    draft_id = uuid4()
    content_sha256 = hashlib.sha256(markdown.encode()).hexdigest()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO app.drafts
                    (id, job_id, version, title, markdown, html, word_count,
                     outline_json, seo_json, produced_by, content_sha256)
                VALUES
                    (:id, :job_id, :version, :title, :markdown, :html, :word_count,
                     :outline_json, :seo_json, :produced_by, :content_sha256)
                ON CONFLICT (job_id, version) DO UPDATE SET
                    title = EXCLUDED.title, markdown = EXCLUDED.markdown,
                    html = EXCLUDED.html, word_count = EXCLUDED.word_count,
                    outline_json = EXCLUDED.outline_json, seo_json = EXCLUDED.seo_json,
                    produced_by = EXCLUDED.produced_by, content_sha256 = EXCLUDED.content_sha256
                RETURNING id
                """
            ),
            {
                "id": draft_id,
                "job_id": job_id,
                "version": version,
                "title": title,
                "markdown": markdown,
                "html": html,
                "word_count": word_count,
                "outline_json": json.dumps(outline_json) if outline_json else None,
                "seo_json": json.dumps(seo_json) if seo_json else None,
                "produced_by": produced_by,
                "content_sha256": content_sha256,
            },
        ).mappings().first()
    return str(row["id"])


def insert_revision(job_id: str, from_draft_id: Optional[str], to_draft_id: Optional[str],
                     reason: str, findings: list[dict]) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.revisions (id, job_id, from_draft_id, to_draft_id, reason, findings_json)
                VALUES (:id, :job_id, :from_draft_id, :to_draft_id, :reason, :findings_json)
                """
            ),
            {
                "id": uuid4(),
                "job_id": job_id,
                "from_draft_id": from_draft_id,
                "to_draft_id": to_draft_id,
                "reason": reason,
                "findings_json": json.dumps(findings),
            },
        )


def get_pending_publication(job_id: str, platform: str = "blogger") -> Optional[dict]:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM app.publications WHERE job_id = :job_id AND platform = :platform"),
            {"job_id": job_id, "platform": platform},
        ).mappings().first()
        return dict(row) if row else None


def insert_publication_intent(job_id: str, draft_id: str, blog_id: str, request_id: str,
                               platform: str = "blogger") -> str:
    """Commit a PENDING row BEFORE calling the remote API — the idempotency anchor."""
    engine = get_engine()
    pub_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.publications (id, job_id, draft_id, platform, blog_id, request_id, state)
                VALUES (:id, :job_id, :draft_id, :platform, :blog_id, :request_id, 'PENDING')
                ON CONFLICT (job_id, platform) DO NOTHING
                """
            ),
            {
                "id": pub_id,
                "job_id": job_id,
                "draft_id": draft_id,
                "platform": platform,
                "blog_id": blog_id,
                "request_id": request_id,
            },
        )
    return str(pub_id)


def update_publication(job_id: str, *, state: str, remote_post_id: Optional[str] = None,
                        remote_url: Optional[str] = None, is_draft: bool = True,
                        api_response: Optional[dict] = None, error_message: Optional[str] = None,
                        platform: str = "blogger") -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE app.publications SET state = :state, remote_post_id = :remote_post_id,
                       remote_url = :remote_url, is_draft = :is_draft,
                       api_response = :api_response, error_message = :error_message,
                       confirmed_at = CASE WHEN :state = 'DRAFT' OR :state = 'LIVE' THEN now() ELSE confirmed_at END
                WHERE job_id = :job_id AND platform = :platform
                """
            ),
            {
                "state": state,
                "remote_post_id": remote_post_id,
                "remote_url": remote_url,
                "is_draft": is_draft,
                "api_response": json.dumps(api_response) if api_response else None,
                "error_message": error_message,
                "job_id": job_id,
                "platform": platform,
            },
        )


def get_job(job_id: str) -> Optional[dict]:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT j.*, t.raw_text AS topic_text
                FROM app.jobs j JOIN app.topics t ON t.id = j.topic_id
                WHERE j.id = :id
                """
            ),
            {"id": job_id},
        ).mappings().first()
        return dict(row) if row else None


def list_jobs(states: list[str]) -> list[dict]:
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT j.id, j.state, j.pipeline, j.attempt, j.cost_usd, j.created_at, t.raw_text AS topic_text
                FROM app.jobs j JOIN app.topics t ON t.id = j.topic_id
                WHERE j.state = ANY(:states)
                ORDER BY j.created_at DESC LIMIT 50
                """
            ),
            {"states": states},
        ).mappings().all()
        return [dict(r) for r in rows]


def request_cancel(job_id: str) -> Optional[str]:
    """Cancel a QUEUED job immediately; flag a RUNNING one for cooperative stop.
    Returns the resulting state, or None if job not found."""
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT state FROM app.jobs WHERE id = :id"), {"id": job_id}
        ).mappings().first()
        if not row:
            return None
        if row["state"] == "QUEUED":
            conn.execute(
                text("UPDATE app.jobs SET state = 'CANCELLED', updated_at = now() WHERE id = :id"),
                {"id": job_id},
            )
            return "CANCELLED"
        conn.execute(
            text("UPDATE app.jobs SET cancel_requested = true, updated_at = now() WHERE id = :id"),
            {"id": job_id},
        )
        return row["state"]


def retry_job(job_id: str) -> Optional[str]:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE app.jobs SET state = 'QUEUED', cancel_requested = false, updated_at = now() "
                "WHERE id = :id AND state IN ('FAILED', 'BLOCKED') RETURNING id"
            ),
            {"id": job_id},
        ).mappings().first()
        return str(row["id"]) if row else None


def cost_since(since: datetime) -> Decimal:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM app.jobs WHERE created_at >= :since"),
            {"since": since},
        ).mappings().first()
        return row["total"]


def get_totp_state(user_id: str) -> Optional[dict]:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM app.telegram_totp_state WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).mappings().first()
        return dict(row) if row else None


def record_totp_success(user_id: str, step: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.telegram_totp_state (user_id, last_step, failed_attempts, locked_until, updated_at)
                VALUES (:user_id, :step, 0, NULL, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    last_step = :step, failed_attempts = 0, locked_until = NULL, updated_at = now()
                """
            ),
            {"user_id": user_id, "step": step},
        )


def record_totp_failure(user_id: str, max_attempts: int, lockout_minutes: int) -> None:
    """Increment failed_attempts; lock out once max_attempts is reached."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.telegram_totp_state (user_id, failed_attempts, updated_at)
                VALUES (:user_id, 1, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    failed_attempts = app.telegram_totp_state.failed_attempts + 1,
                    locked_until = CASE
                        WHEN app.telegram_totp_state.failed_attempts + 1 >= :max_attempts
                        THEN now() + make_interval(mins => :lockout_minutes)
                        ELSE app.telegram_totp_state.locked_until
                    END,
                    updated_at = now()
                """
            ),
            {"user_id": user_id, "max_attempts": max_attempts, "lockout_minutes": lockout_minutes},
        )
