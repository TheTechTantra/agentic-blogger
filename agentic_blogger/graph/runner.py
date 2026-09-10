"""JobRunner — claims a job, invokes the compiled graph, updates the job row.

Job claiming and durable execution are deliberately separate concerns
(plan §4): LangGraph's checkpointer resumes a node sequence; it says nothing
about which job to pick up next. That's still our claim loop (db/repo.py).
"""

import contextlib
import logging
import threading
import time

from agentic_blogger.db import repo
from agentic_blogger.nodes.publish import JobBlocked
from agentic_blogger.observability.log_setup import bind_job, summarize_state
from agentic_blogger.observability.mlflow_setup import log_job_metrics, start_job_run
from agentic_blogger.prompts import PromptRegistryError
from agentic_blogger.prompts.preflight import check_all

logger = logging.getLogger(__name__)

# claim_job stamps a 15-minute lease and nothing renewed it, so any job whose
# nodes together outran that window became claimable while still running. The
# single-threaded worker loop was the only thing hiding it: run_one() blocks
# until the graph returns, so one worker never polled while busy. A second
# replica would have double-run the job — duplicate LLM spend, and a publish
# node racing itself against Blogger.
#
# The renewal spans the whole invoke() rather than firing between nodes,
# because a single node can outlast the lease on its own: the research node
# runs 12 minutes against web_search before it yields anything.
_LEASE_MINUTES = 15
_HEARTBEAT_SECONDS = 300


@contextlib.contextmanager
def _lease_heartbeat(job_id: str, worker_id: str):
    """Keep a job's lease alive for as long as the body is running."""
    stop = threading.Event()

    def beat():
        # The heartbeat runs on its own thread, so it needs its own binding —
        # ContextVars are per-thread and this one starts empty.
        with bind_job(job_id):
            _beat_loop()

    def _beat_loop():
        beats = 0
        while not stop.wait(_HEARTBEAT_SECONDS):
            beats += 1
            try:
                if not repo.heartbeat_lease(job_id, worker_id, _LEASE_MINUTES):
                    # Someone else owns the job now. Nothing safe left to do
                    # from here — the graph keeps running to its checkpoint,
                    # but we stop asserting a claim we no longer hold.
                    logger.error("lease lost by worker=%s — stopping heartbeat", worker_id)
                    return
                # Proof of life for a job that is otherwise silent for the
                # 12 minutes the research node spends inside one API call.
                logger.info("lease renewed (beat=%d, +%dmin, elapsed=%dmin)",
                            beats, _LEASE_MINUTES, beats * _HEARTBEAT_SECONDS // 60)
            except Exception:
                # A transient DB blip must not kill the job that is running
                # fine; the next tick retries well inside the lease window.
                logger.warning("lease heartbeat failed — will retry", exc_info=True)

    thread = threading.Thread(target=beat, name=f"lease-{job_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


class JobRunner:
    def __init__(self, compiled_graph, worker_id: str):
        self.compiled_graph = compiled_graph
        self.worker_id = worker_id

    def run_one(self) -> bool:
        """Claim and run one job. Returns True if a job was claimed (whether
        it succeeded or failed), False if the queue was empty."""
        claimed = repo.claim_job(self.worker_id)
        if not claimed:
            return False

        job_id = claimed["job_id"]
        thread_id = claimed["thread_id"]
        topic = claimed["topic"]
        attempt = claimed["attempt"]
        resumed = attempt > 1
        config = {"configurable": {"thread_id": thread_id}}
        source_url = (claimed.get("config_snapshot") or {}).get("source_url")

        with bind_job(job_id):
            return self._run_claimed(claimed, job_id, thread_id, topic, attempt,
                                     resumed, config, source_url)

    def _run_claimed(self, claimed, job_id, thread_id, topic, attempt, resumed,
                     config, source_url) -> bool:
        started = time.monotonic()
        logger.info(
            "claimed attempt=%d/%d resumed=%s worker=%s thread=%s source_url=%s topic=%r",
            attempt, claimed["max_attempts"], resumed, self.worker_id, thread_id,
            source_url or "-", topic,
        )

        with start_job_run(job_id, resumed=resumed), _lease_heartbeat(job_id, self.worker_id):
            try:
                # Before any LLM spend: prove the registry can serve every
                # prompt this pipeline needs, with the variables the nodes
                # supply. A prompt edited to rename a variable would otherwise
                # sail through research on Opus and die at draft.
                logger.info("prompt preflight starting")
                check_all()
                logger.info("prompt preflight ok")

                if resumed:
                    logger.info("invoking graph (resuming from checkpoint)")
                    final_state = self.compiled_graph.invoke(None, config=config)
                else:
                    initial_state = {
                        "job_id": job_id,
                        "topic": topic,
                        # Present only for /url jobs; research branches on it.
                        "source_url": source_url,
                        "sources": [],
                        "revision_count": 0,
                    }
                    logger.info("invoking graph (fresh run)")
                    final_state = self.compiled_graph.invoke(initial_state, config=config)
            except PromptRegistryError as e:
                # Registry is the only source of prompt text and has no
                # fallback by design: a job that cannot read its prompts is
                # terminal, not retryable. Fix the registry, re-create the job.
                logger.error("FAILED on prompt registry after %.1fs: %s",
                             time.monotonic() - started, e)
                repo.mark_job_state(job_id, "FAILED", failure_class="PromptRegistryError",
                                     failure_reason=str(e))
                return True
            except JobBlocked as e:
                logger.error("BLOCKED after %.1fs: %s", time.monotonic() - started, e)
                repo.mark_job_state(job_id, "BLOCKED", failure_class="JobBlocked",
                                     failure_reason=str(e))
                return True
            except Exception as e:
                logger.exception("node raised after %.1fs (%s) — attempt %d/%d",
                                 time.monotonic() - started, type(e).__name__,
                                 claimed["attempt"], claimed["max_attempts"])
                if claimed["attempt"] >= claimed["max_attempts"]:
                    logger.error("attempts exhausted — marking FAILED")
                    repo.mark_job_state(job_id, "FAILED", failure_class=type(e).__name__,
                                         failure_reason=str(e))
                else:
                    # Leave RUNNING; lease will expire and another claim
                    # attempt resumes it from the last good checkpoint.
                    logger.info("will be retried on lease expiry (attempt %d/%d, "
                                "lease expires in ~%dmin)",
                                claimed["attempt"], claimed["max_attempts"], _LEASE_MINUTES)
                return True

        elapsed = time.monotonic() - started
        published = final_state.get("published") or {}
        job_row = repo.get_job(job_id)
        logger.info("graph complete in %.1fs final_state: %s", elapsed,
                    summarize_state(final_state))
        log_job_metrics(job_row["cost_usd"], job_row["tokens_in"], job_row["tokens_out"])
        repo.mark_job_state(job_id, "PUBLISHED")
        logger.info("PUBLISHED in %.1fs cost_usd=%s tokens_in=%s tokens_out=%s "
                    "post_id=%s remote_url=%s",
                    elapsed, job_row["cost_usd"], job_row["tokens_in"], job_row["tokens_out"],
                    published.get("remote_post_id"), published.get("remote_url"))
        return True
