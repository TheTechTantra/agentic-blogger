"""JobRunner — claims a job, invokes the compiled graph, updates the job row.

Job claiming and durable execution are deliberately separate concerns
(plan §4): LangGraph's checkpointer resumes a node sequence; it says nothing
about which job to pick up next. That's still our claim loop (db/repo.py).
"""

import logging

from agentic_blogger.db import repo
from agentic_blogger.nodes.publish import JobBlocked
from agentic_blogger.observability.mlflow_setup import log_job_metrics, start_job_run
from agentic_blogger.prompts import PromptRegistryError
from agentic_blogger.prompts.preflight import check_all

logger = logging.getLogger(__name__)


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

        logger.info("job=%s claimed attempt=%d resumed=%s topic=%r", job_id, attempt, resumed, topic)

        with start_job_run(job_id, resumed=resumed):
            try:
                # Before any LLM spend: prove the registry can serve every
                # prompt this pipeline needs, with the variables the nodes
                # supply. A prompt edited to rename a variable would otherwise
                # sail through research on Opus and die at draft.
                check_all()

                if resumed:
                    final_state = self.compiled_graph.invoke(None, config=config)
                else:
                    initial_state = {
                        "job_id": job_id,
                        "topic": topic,
                        # Present only for /url jobs; research branches on it.
                        "source_url": (claimed.get("config_snapshot") or {}).get("source_url"),
                        "sources": [],
                        "revision_count": 0,
                    }
                    final_state = self.compiled_graph.invoke(initial_state, config=config)
            except PromptRegistryError as e:
                # Registry is the only source of prompt text and has no
                # fallback by design: a job that cannot read its prompts is
                # terminal, not retryable. Fix the registry, re-create the job.
                logger.error("job=%s FAILED on prompt registry: %s", job_id, e)
                repo.mark_job_state(job_id, "FAILED", failure_class="PromptRegistryError",
                                     failure_reason=str(e))
                return True
            except JobBlocked as e:
                logger.error("job=%s BLOCKED: %s", job_id, e)
                repo.mark_job_state(job_id, "BLOCKED", failure_class="JobBlocked", failure_reason=str(e))
                return True
            except Exception as e:
                logger.exception("job=%s node raised — marking FAILED", job_id)
                if claimed["attempt"] >= claimed["max_attempts"]:
                    repo.mark_job_state(job_id, "FAILED", failure_class=type(e).__name__,
                                         failure_reason=str(e))
                else:
                    # Leave RUNNING; lease will expire and another claim
                    # attempt resumes it from the last good checkpoint.
                    logger.info("job=%s will be retried on lease expiry (attempt %d/%d)",
                                job_id, claimed["attempt"], claimed["max_attempts"])
                return True

        published = final_state.get("published") or {}
        job_row = repo.get_job(job_id)
        log_job_metrics(job_row["cost_usd"], job_row["tokens_in"], job_row["tokens_out"])
        repo.mark_job_state(job_id, "PUBLISHED")
        logger.info("job=%s PUBLISHED remote_url=%s", job_id, published.get("remote_url"))
        return True
