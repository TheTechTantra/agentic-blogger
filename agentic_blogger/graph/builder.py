"""Graph topology. Returns an uncompiled StateGraph — the caller compiles it
with a checkpointer (see runner.py), matching the pattern proven in
scripts/smoke_graph.py where the PostgresSaver connection must stay open for
the life of the graph's use."""

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


def build_graph() -> StateGraph:
    g = StateGraph(BlogState)

    for name, fn in NODES.items():
        g.add_node(name, fn, retry=RetryPolicy(max_attempts=3))

    g.set_entry_point("research")
    g.add_edge("research", "outline")
    g.add_edge("outline", "draft")
    g.add_edge("draft", "factcheck")
    g.add_conditional_edges("factcheck", needs_revision, {"revise": "revise", "continue": "seo"})
    g.add_edge("revise", "factcheck")
    g.add_edge("seo", "format")
    g.add_edge("format", "publish")
    g.add_edge("publish", END)

    return g
