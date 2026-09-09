"""Research node — two passes.

1. search_tool role bound to web_search_20260209, forced to actually search
   (tool_choice), gathering tier-stratified material and citable code. For a
   URL-sourced job (state["source_url"]) web_fetch_20260209 is bound too and
   forced first, so the article is read before anything is searched. Both are
   Anthropic server-side tools: the page is fetched and extracted on their
   infrastructure, never by this container.
2. research_synth role structures that raw text into a ResearchBrief so the
   downstream nodes can route material per reader tier instead of
   re-deriving it from prose.

Pass 2 is a separate call because a server-tool response and
.with_structured_output() cannot share one invocation.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.db import repo
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.schemas import TIERS, ResearchBrief, bound
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)

# `allowed_callers: ["direct"]` is deliberately absent from both tools. It
# disables dynamic filtering (the tools' internal code-execution step that
# discards irrelevant content before it reaches the context window), which is
# the only thing keeping a large PDF's token cost bounded — max_content_tokens
# does not apply to binary content. The cost is ZDR eligibility: these tool
# versions are not zero-data-retention eligible with filtering on. Re-add
# `allowed_callers: ["direct"]` to both if this deployment ever needs ZDR, and
# expect research on PDFs to get much more expensive.
_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 12,
}

# web_fetch only retrieves URLs already present in the conversation, so the
# source URL must appear in the prompt text (it does — see _url_prompt).
# Fetches text, HTML and PDF only; PDFs come back as base64 and are processed
# like an attached document. max_content_tokens truncates *text* content only.
_FETCH_TOOL = {
    "type": "web_fetch_20260318",
    "name": "web_fetch",
    "max_uses": 5,
    "max_content_tokens": 120000,
    "citations": {"enabled": True},
}

# These tool versions require Opus 4.6+/Sonnet 4.6+, and without
# allowed_callers they also require a model that supports programmatic tool
# calling (Opus 4.5+/Sonnet 4.5+) — see the search_tool role in
# config/models.yaml before switching it to a smaller model.


def _handle_search_result(block: dict, sources: list[dict]) -> None:
    results = block.get("content", [])
    # A server-tool error arrives as HTTP 200 with content as a dict
    # ({"error_code": ...}) rather than a list — never index it.
    if isinstance(results, dict):
        logger.warning("web_search returned an error block: %s", results.get("error_code"))
        return
    if not isinstance(results, list):
        return
    for r in results:
        if isinstance(r, dict) and r.get("url"):
            sources.append({
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "snippet": "",
                # Freshness matters for version-sensitive claims; repo ignores
                # unknown keys.
                "page_age": r.get("page_age", ""),
            })


def _handle_fetch_result(block: dict, sources: list[dict]) -> None:
    # content is a single web_fetch_result object (not a list), or an error
    # object if the fetch failed.
    result = block.get("content")
    if not isinstance(result, dict):
        return
    if result.get("error_code"):
        logger.warning("web_fetch failed: %s", result["error_code"])
        return
    if not result.get("url"):
        return
    doc = result.get("content") or {}
    sources.append({
        "url": result["url"],
        "title": (doc.get("title") if isinstance(doc, dict) else "") or "",
        "snippet": "",
        "page_age": result.get("retrieved_at", ""),
        # Marks the article the job was built from, as opposed to
        # corroborating material found by search.
        "source_type": "primary_url",
    })


_RESULT_HANDLERS = {
    "web_search_tool_result": _handle_search_result,
    "web_fetch_tool_result": _handle_fetch_result,
}


def _collect_sources(node, sources: list[dict], depth: int = 0) -> None:
    """Walk the response content for tool-result blocks.

    Recursive rather than a flat loop over top-level blocks: with dynamic
    filtering enabled, the web tools run inside a code-execution container and
    their result blocks arrive nested inside that call's blocks. A flat scan
    finds nothing there, which would look identical to a failed fetch.
    """
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _collect_sources(item, sources, depth + 1)
        return
    if not isinstance(node, dict):
        return

    handler = _RESULT_HANDLERS.get(node.get("type"))
    if handler:
        handler(node, sources)
        return

    for value in node.values():
        if isinstance(value, (dict, list)):
            _collect_sources(value, sources, depth + 1)


def _extract_text_and_sources(content) -> tuple[str, list[dict]]:
    """Anthropic server-tool responses come back as a list of content
    blocks: text, server_tool_use, and web_search/web_fetch results. Parse
    defensively — an unexpected shape degrades to an empty source list
    (fail-open) rather than crashing the node."""
    if isinstance(content, str):
        return content, []

    text_parts: list[str] = []
    sources: list[dict] = []
    try:
        # Prose is taken from top-level text blocks only; text nested inside a
        # tool result is fetched material, not the model's synthesis.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        _collect_sources(content, sources)
    except Exception:
        logger.exception("Failed parsing search response content — continuing with what we have")

    # Dynamic filtering can surface the same page from both a search and a
    # fetch; keep the first mention of each URL, which preserves the
    # primary_url marker when the fetch came first.
    deduped: dict[str, dict] = {}
    for s in sources:
        deduped.setdefault(s["url"], s)

    return "\n".join(text_parts), list(deduped.values())


def _search_prompt(topic: str) -> str:
    return render(
        S.RESEARCH_USER,
        variables={"tiers": ", ".join(TIERS)},
        content={"topic": topic},
    )


def _url_prompt(url: str, angle: str) -> str:
    """Prompt for a URL-sourced job. The URL is stated literally because
    web_fetch will only retrieve URLs that already appear in the
    conversation."""
    angle_line = (
        render(S.RESEARCH_URL_ANGLE, content={"angle": angle}) + "\n\n"
        if angle and angle != url else ""
    )
    return render(
        S.RESEARCH_URL_USER,
        variables={"tiers": ", ".join(TIERS)},
        content={"url": url, "angle_line": angle_line},
    )


def _synthesize(job_id: str, topic: str, raw: str, sources: list[dict],
                source_url: str | None = None) -> dict:
    """Pass 2 — structure the raw research text. Falls back to the prose-only
    shape if structured output fails, so a parse problem degrades the brief
    rather than failing the job."""
    spec = role_spec("research_synth")
    llm = build_llm("research_synth").with_structured_output(bound(ResearchBrief), include_raw=True)
    llm = with_resilience(llm, "research_synth")

    source_lines = "\n".join(
        f"- {s.get('title') or '(untitled)'} — {s['url']}"
        + (f" (page_age: {s['page_age']})" if s.get("page_age") else "")
        for s in sources
    ) or "(none captured)"

    primary_rules = (
        render(S.RESEARCH_SYNTH_RULES_URL, content={"source_url": source_url})
        if source_url else render(S.RESEARCH_SYNTH_RULES_NONE)
    )

    prompt = render(
        S.RESEARCH_SYNTH_USER,
        variables={"tiers": ", ".join(TIERS)},
        content={
            "topic": topic,
            "primary_rules": primary_rules,
            "source_lines": source_lines,
            "raw": raw,
        },
    )

    with track_llm_call(job_id, "research_synth", "research_synth", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    parsed: ResearchBrief | None = result.get("parsed")
    if parsed is None:
        logger.warning("job=%s research_synth returned no parsed brief — falling back to prose", job_id)
        return {"brief": raw}
    return parsed.model_dump()


def research_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    source_url = state.get("source_url")
    spec = role_spec("search_tool")

    if source_url:
        # web_fetch is forced first so the article is read before any search;
        # tool_choice only constrains the first call, so the model is free to
        # search afterwards.
        tools = [_FETCH_TOOL, _SEARCH_TOOL]
        tool_choice = {"type": "tool", "name": "web_fetch"}
        prompt = _url_prompt(source_url, topic)
    else:
        tools = [_SEARCH_TOOL]
        tool_choice = {"type": "tool", "name": "web_search"}
        prompt = _search_prompt(topic)

    llm = build_llm("search_tool").bind_tools(tools, tool_choice=tool_choice)
    llm = with_resilience(llm, "search_tool")

    system = render(S.RESEARCH_SYSTEM)
    if source_url:
        system = f"{system}\n\n{render(S.RESEARCH_URL_SYSTEM)}"
    messages = [SystemMessage(content=system), HumanMessage(content=prompt)]

    with track_llm_call(job_id, "research", "search_tool", spec["model"]) as record:
        response = llm.invoke(messages)
        record(response)

    raw, sources = _extract_text_and_sources(response.content)

    if not sources:
        logger.warning("job=%s research produced no extractable sources", job_id)

    if source_url and not any(s.get("source_type") == "primary_url" for s in sources):
        # The whole premise of a /url job is that the page was read. If the
        # fetch failed we have a blind-search brief wearing a URL job's label —
        # fail loudly rather than publish it.
        raise RuntimeError(f"web_fetch did not return content for {source_url}")

    repo.insert_research_sources(job_id, sources, search_provider="anthropic_server_tools",
                                  search_query=source_url or topic)

    brief = _synthesize(job_id, topic, raw, sources, source_url)

    # An exemplar whose source_url did not survive is unattributable — dropping
    # it here is cheaper than trusting the draft node not to publish it.
    exemplars = brief.get("code_exemplars") or []
    attributed = [e for e in exemplars if e.get("source_url")]
    if len(attributed) != len(exemplars):
        logger.warning("job=%s dropped %d unattributed code exemplars",
                       job_id, len(exemplars) - len(attributed))
        brief["code_exemplars"] = attributed

    if source_url and not (brief.get("primary_source") or {}).get("title"):
        # Without a citation the draft node cannot credit the work, and an
        # uncredited explainer of someone else's paper is the one output this
        # pipeline must never produce.
        raise RuntimeError(f"no primary_source citation extracted for {source_url}")

    logger.info("job=%s research tiers=%d exemplars=%d format=%s", job_id,
                len(brief.get("tiers") or []), len(brief.get("code_exemplars") or []),
                brief.get("recommended_format"))

    return {
        "research_brief": brief,
        "sources": sources,
    }
