"""Graph topology. Returns an uncompiled StateGraph — the caller compiles it
with a checkpointer (see runner.py), matching the pattern proven in
scripts/smoke_graph.py where the PostgresSaver connection must stay open for
the life of the graph's use."""

import logging
import time

from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from agentic_blogger.graph.state import BlogState
from agentic_blogger.nodes.draft import draft_node
from agentic_blogger.nodes.factcheck import factcheck_node, needs_revision
from agentic_blogger.nodes.format import format_node
from agentic_blogger.nodes.outline import outline_node
from agentic_blogger.nodes.publish import publish_node
from agentic_blogger.nodes.research import research_node
from agentic_blogger.nodes.revise import revise_node
from agentic_blogger.nodes.seo import seo_node
from agentic_blogger.observability.log_setup import bind_job, bind_node, summarize_state

logger = logging.getLogger(__name__)

NODES = {
    "research": research_node,
    "outline": outline_node,
    "draft": draft_node,
    "factcheck": factcheck_node,
    "revise": revise_node,
    "seo": seo_node,
    "format": format_node,
    "publish": publish_node,
}

# What each node reads. Logged on entry so a run's inputs are visible without
# dumping the checkpoint: if draft starts with outline_plan=none, the failure
# is upstream and the log says so on the line before the traceback.
_INPUTS = {
    "research": ("topic", "source_url"),
    "outline": ("topic", "research_brief"),
    "draft": ("topic", "outline_plan", "research_brief"),
    "factcheck": ("draft_markdown", "research_brief"),
    "revise": ("draft_markdown", "factcheck_report", "revision_count"),
    "seo": ("draft_title", "draft_markdown"),
    "format": ("draft_markdown", "factcheck_report"),
    "publish": ("draft_id", "draft_title", "seo_meta", "html"),
}


def _traced(name: str, fn):
    """Wrap a node with entry/exit/failure logging.

    Central rather than per-node so every node is covered identically and a
    node added later cannot forget it. The job binding is re-applied here
    because LangGraph may run the node on an executor thread that did not
    inherit the runner's context.
    """
    def wrapper(state: dict):
        job_id = state.get("job_id", "")
        with bind_job(job_id), bind_node(name):
            started = time.monotonic()
            logger.info("start in: %s", summarize_state(state, _INPUTS.get(name, ())))
            try:
                result = fn(state)
            except Exception as e:
                logger.error("failed after %.1fs: %s: %s",
                             time.monotonic() - started, type(e).__name__, e, exc_info=True)
                raise
            elapsed = time.monotonic() - started
            logger.info("done in %.1fs out: %s", elapsed, summarize_state(result or {}))
            return result

    wrapper.__name__ = f"{name}_traced"
    return wrapper


def _traced_router(fn):
    """Log which branch a conditional edge chose.

    Separate from _traced because a router returns a branch name, not a state
    delta — and "which way did factcheck send it" is the single most useful
    line for explaining why a run has two factcheck entries or none.
    """
    def wrapper(state: dict):
        with bind_job(state.get("job_id", "")), bind_node("route"):
            choice = fn(state)
            findings = (state.get("factcheck_report") or {}).get("findings", [])
            flagged = [f for f in findings
                       if f.get("verdict") in ("unsupported", "contradicted")]
            logger.info("factcheck -> %s (findings=%d flagged=%d revision_count=%d)",
                        choice, len(findings), len(flagged), state.get("revision_count", 0))
            return choice

    wrapper.__name__ = f"{fn.__name__}_traced"
    return wrapper


def build_graph() -> StateGraph:
    g = StateGraph(BlogState)

    for name, fn in NODES.items():
        g.add_node(name, _traced(name, fn), retry=RetryPolicy(max_attempts=3))

    g.set_entry_point("research")
    g.add_edge("research", "outline")
    g.add_edge("outline", "draft")
    g.add_edge("draft", "factcheck")
    g.add_conditional_edges("factcheck", _traced_router(needs_revision),
                             {"revise": "revise", "continue": "seo"})
    g.add_edge("revise", "factcheck")
    g.add_edge("seo", "format")
    g.add_edge("format", "publish")
    g.add_edge("publish", END)

    return g
