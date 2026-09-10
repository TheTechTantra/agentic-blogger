"""Orchestrator worker — claim jobs and run the graph.

One replica in Phase 1 (plan §8, secrets concurrent-write constraint —
scaling later is safe once the advisory-lock serialization is added).
"""

import logging
import os
import time

from langgraph.checkpoint.postgres import PostgresSaver

from agentic_blogger.db.engine import psycopg_url
from agentic_blogger.graph.builder import build_graph
from agentic_blogger.graph.runner import JobRunner
from agentic_blogger.observability.log_setup import setup_logging

setup_logging("orchestrator")
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "5"))
WORKER_ID = os.getenv("HOSTNAME", "orchestrator-1")

# An idle worker logs nothing, which is indistinguishable from a dead one —
# and that ambiguity cost real time during the last incident. One line every
# ~5 minutes proves the loop is turning without burying job logs under a
# poll line every 5 seconds.
IDLE_LOG_EVERY_S = 300


def main():
    logger.info("Orchestrator worker starting (worker_id=%s)", WORKER_ID)
    graph = build_graph()

    with PostgresSaver.from_conn_string(psycopg_url()) as checkpointer:
        checkpointer.setup()  # idempotent
        compiled = graph.compile(checkpointer=checkpointer)
        runner = JobRunner(compiled, WORKER_ID)

        logger.info("Ready — polling for jobs every %ds (idle heartbeat every %ds)",
                    POLL_INTERVAL_S, IDLE_LOG_EVERY_S)
        idle_since = time.monotonic()
        last_idle_log = idle_since
        jobs_run = 0
        while True:
            try:
                claimed = runner.run_one()
            except Exception:
                logger.exception("Unexpected error in run_one() — continuing loop")
                claimed = False

            if claimed:
                jobs_run += 1
                logger.info("worker idle again after job %d (worker_id=%s)", jobs_run, WORKER_ID)
                idle_since = last_idle_log = time.monotonic()
                continue

            now = time.monotonic()
            if now - last_idle_log >= IDLE_LOG_EVERY_S:
                logger.info("idle %dm — queue empty, jobs_run=%d",
                            int((now - idle_since) // 60), jobs_run)
                last_idle_log = now
            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
