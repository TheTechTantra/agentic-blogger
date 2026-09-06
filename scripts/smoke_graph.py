#!/usr/bin/env python3
"""
Smoke test: PostgresSaver durability and resumption.

Tests that LangGraph checkpointer correctly resumes from a checkpoint
without re-running completed nodes. This is the core reason for decision 8.

Usage:
  docker compose run --rm orchestrator python -m scripts.smoke_graph

Expected behavior:
  1. First invocation: node_1 runs, node_2 fails mid-way
  2. Second invocation (resume): node_1 is SKIPPED (already completed),
     node_2 resumes and completes
"""

import os
import sys
import json
import logging
from typing_extensions import TypedDict
from uuid import uuid4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.postgres import PostgresSaver
except ImportError as e:
    logger.error(f"LangGraph import failed: {e}")
    logger.error("Verify requirements.txt: langgraph-checkpoint-postgres installed")
    sys.exit(1)


class SimpleState(TypedDict):
    """Minimal state for 2-node graph."""
    value: int
    node_1_ran: bool
    node_2_ran: bool
    error_on_node_2: bool


# Global to track what ran (for demo purposes)
execution_log = []


def node_1(state: SimpleState) -> dict:
    """First node: increment value and mark as ran."""
    logger.info("NODE_1: Starting")
    execution_log.append("node_1_executed")
    logger.info("NODE_1: Marking node_1_ran=True")
    return {"value": state["value"] + 1, "node_1_ran": True}


def node_2(state: SimpleState) -> dict:
    """Second node: increment value and optionally fail."""
    logger.info("NODE_2: Starting")
    logger.info(f"NODE_2: Current state.node_1_ran = {state['node_1_ran']}")

    if state["error_on_node_2"]:
        logger.info("NODE_2: Raising exception as requested")
        execution_log.append("node_2_failed")
        raise RuntimeError("Simulated failure in node_2 (crash)")

    execution_log.append("node_2_executed")
    logger.info("NODE_2: Completing successfully")
    return {"value": state["value"] + 1, "node_2_ran": True}


def _psycopg_url() -> str:
    """Build a plain postgresql:// URL — PostgresSaver uses psycopg v3,
    which doesn't take the '+psycopg2' SQLAlchemy driver suffix."""
    user = os.getenv("POSTGRES_USER", "agentic_blogger")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD not set")
        sys.exit(1)
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "25432")
    db = os.getenv("POSTGRES_DB", "blogger")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def build_test_graph():
    """Build a simple 2-node graph (uncompiled — caller compiles with a checkpointer)."""
    logger.info("Building StateGraph...")

    graph = StateGraph(SimpleState)
    graph.add_node("node_1", node_1)
    graph.add_node("node_2", node_2)

    graph.set_entry_point("node_1")
    graph.add_edge("node_1", "node_2")
    graph.add_edge("node_2", END)

    return graph


def run_test():
    """
    Run the smoke test: execute, fail, resume, verify no re-run.

    Both invocations share one PostgresSaver connection (one process, one
    `with` block) — that's fine. What's under test is checkpoint durability
    across separate `.invoke()` calls, not process survival; a real crash
    is simulated by node_2 raising instead of the process exiting.
    """
    logger.info("=== Smoke Test: PostgresSaver Durability ===")

    postgres_url = _psycopg_url()
    logger.info(f"Using database: {postgres_url.split('@')[1] if '@' in postgres_url else postgres_url}")

    graph = build_test_graph()

    with PostgresSaver.from_conn_string(postgres_url) as checkpointer:
        checkpointer.setup()  # idempotent — creates tables on first run only
        compiled = graph.compile(checkpointer=checkpointer)
        logger.info("Graph compiled successfully")

        # Generate a thread_id for resumption
        thread_id = f"smoke-test-{uuid4().hex[:8]}"
        logger.info(f"Thread ID: {thread_id}")

        # ===== PHASE 1: Initial run (will fail in node_2) =====
        logger.info("\n--- PHASE 1: Initial invocation (expect failure in node_2) ---")
        execution_log.clear()

        initial_state = {
            "value": 0,
            "node_1_ran": False,
            "node_2_ran": False,
            "error_on_node_2": True,  # Trigger failure
        }

        logger.info(f"Initial state: {initial_state}")

        try:
            logger.info("Invoking graph with config={configurable={thread_id=...}}...")
            final = compiled.invoke(
                initial_state,
                config={"configurable": {"thread_id": thread_id}}
            )
            logger.warning("Graph completed without error (unexpected!)")
        except RuntimeError as e:
            logger.info(f"✓ Expected failure caught: {e}")
            logger.info(f"Execution log after phase 1: {execution_log}")

        # Verify phase 1 results
        if "node_1_executed" not in execution_log:
            logger.error("ERROR: node_1 did not execute in phase 1")
            return False
        if "node_2_failed" not in execution_log:
            logger.error("ERROR: node_2 did not fail in phase 1")
            return False

        logger.info("✓ Phase 1 completed as expected")

        # ===== PHASE 2: Resume from checkpoint =====
        logger.info("\n--- PHASE 2: Resume from checkpoint (should skip node_1) ---")
        execution_log.clear()

        # error_on_node_2 is part of checkpointed state, so it would still be
        # True and node_2 would fail again forever. A real retry fixes the
        # underlying cause first — simulate that with update_state(), the
        # same mechanism a production retry path would use.
        config = {"configurable": {"thread_id": thread_id}}
        compiled.update_state(config, {"error_on_node_2": False})
        logger.info("Cleared error_on_node_2 via update_state() (simulates a fixed retry)")

        # Invoke with None input — this resumes from the last checkpoint
        # node_1 should be skipped because it already completed
        logger.info("Invoking graph with input=None (resume mode)...")
        logger.info("Expected: node_1 SKIPPED, node_2 runs and completes")

        try:
            final = compiled.invoke(
                None,  # Resume from checkpoint
                config={"configurable": {"thread_id": thread_id}}
            )
            logger.info(f"✓ Graph completed")
            logger.info(f"Final state: {json.dumps(final, default=str, indent=2)}")
        except Exception as e:
            logger.error(f"ERROR on resume: {e}")
            return False

        logger.info(f"Execution log after phase 2: {execution_log}")

        # ===== VERIFICATION =====
        logger.info("\n--- VERIFICATION ---")

        # node_1 should NOT appear in execution_log for phase 2
        if "node_1_executed" in execution_log:
            logger.error("ERROR: node_1 re-ran on resume (it should have been skipped!)")
            logger.error("This indicates PostgresSaver is not working correctly.")
            return False
        else:
            logger.info("✓ node_1 was correctly skipped on resume")

        # node_2 should appear in phase 2 execution_log
        if "node_2_executed" not in execution_log:
            logger.error("ERROR: node_2 did not complete on resume")
            return False
        else:
            logger.info("✓ node_2 completed on resume")

        # Final state should have both nodes marked as ran
        if not final.get("node_1_ran"):
            logger.error("ERROR: node_1_ran is False in final state")
            return False
        if not final.get("node_2_ran"):
            logger.error("ERROR: node_2_ran is False in final state")
            return False

        logger.info("✓ Final state shows both nodes completed")
        logger.info(f"✓ Final value: {final.get('value')} (expected 2)")

        return True


if __name__ == "__main__":
    try:
        success = run_test()
        if success:
            logger.info("\n=== SMOKE TEST PASSED ===")
            logger.info("PostgresSaver durability confirmed:")
            logger.info("  - Checkpoint created after node_1 completion")
            logger.info("  - Resume skipped node_1 (no re-run)")
            logger.info("  - node_2 resumed and completed from checkpoint")
            sys.exit(0)
        else:
            logger.error("\n=== SMOKE TEST FAILED ===")
            sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
