"""Research node — search_tool role bound to web_search_20260209, forced to
actually search (tool_choice), then structured extraction of citable sources.
"""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.db import repo
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience

logger = logging.getLogger(__name__)

_SEARCH_TOOLS = [{
    "type": "web_search_20260209",
    "name": "web_search",
    "allowed_callers": ["direct"],
}]


def _extract_text_and_sources(content) -> tuple[str, list[dict]]:
    """Anthropic server-tool responses come back as a list of content
    blocks: text, server_tool_use, and web_search_tool_result. Parse
    defensively — an unexpected shape degrades to an empty source list
    (fail-open) rather than crashing the node."""
    if isinstance(content, str):
        return content, []

    text_parts: list[str] = []
    sources: list[dict] = []
    try:
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "web_search_tool_result":
                results = block.get("content", [])
                if isinstance(results, list):
                    for r in results:
                        if isinstance(r, dict) and r.get("url"):
                            sources.append({
                                "url": r.get("url", ""),
                                "title": r.get("title", ""),
                                "snippet": "",
                            })
    except Exception:
        logger.exception("Failed parsing search response content — continuing with what we have")

    return "\n".join(text_parts), sources


def research_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    spec = role_spec("search_tool")

    llm = build_llm("search_tool").bind_tools(
        _SEARCH_TOOLS, tool_choice={"type": "tool", "name": "web_search"}
    )
    llm = with_resilience(llm, "search_tool")

    prompt = (
        f"Research the topic: \"{topic}\".\n"
        "Use web search to find current, credible information. Then write a "
        "thorough research brief (600-900 words) synthesizing what you found, "
        "covering key facts, context, and any recent developments. Be specific "
        "and cite claims to what you read."
    )

    with track_llm_call(job_id, "research", "search_tool", spec["model"]) as record:
        response = llm.invoke([HumanMessage(content=prompt)])
        record(response)

    brief, sources = _extract_text_and_sources(response.content)

    if not sources:
        logger.warning("job=%s research produced no extractable sources", job_id)

    repo.insert_research_sources(job_id, sources, search_provider="anthropic_server_tools",
                                  search_query=topic)

    return {
        "research_brief": {"brief": brief},
        "sources": sources,
    }
