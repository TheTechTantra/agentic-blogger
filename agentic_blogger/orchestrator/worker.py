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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "5"))
WORKER_ID = os.getenv("HOSTNAME", "orchestrator-1")


def main():
    logger.info("Orchestrator worker starting (worker_id=%s)", WORKER_ID)
    graph = build_graph()

    with PostgresSaver.from_conn_string(psycopg_url()) as checkpointer:
        checkpointer.setup()  # idempotent
        compiled = graph.compile(checkpointer=checkpointer)
        runner = JobRunner(compiled, WORKER_ID)

        logger.info("Ready — polling for jobs every %ds", POLL_INTERVAL_S)
        while True:
            try:
                claimed = runner.run_one()
            except Exception:
                logger.exception("Unexpected error in run_one() — continuing loop")
                claimed = False
            if not claimed:
                time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
