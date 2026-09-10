"""Draft node — writes the full markdown post from the outline + research brief."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.db import repo
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.llm.text import extract_text
from agentic_blogger.nodes.attribution import explainer_contract
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)


def _format_outline(outline: dict) -> str:
    lines = []
    for section in outline.get("sections", []):
        lines.append(f"- {section['heading']}: {', '.join(section.get('key_points', []))}")
    return "\n".join(lines)


def draft_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    research = state.get("research_brief") or {}
    brief = research.get("brief", "")
    primary_source = research.get("primary_source") or {}
    outline = state.get("outline_plan") or {}
    spec = role_spec("draft")

    llm = with_resilience(build_llm("draft"), "draft")

    title = (outline.get("title_options") or [topic])[0]

    # Placed before the outline and brief on purpose: the attribution contract
    # governs how everything after it may be used.
    contract = f"{explainer_contract(primary_source)}\n\n" if primary_source else ""

    prompt = render(S.DRAFT_USER, content={
        "contract": contract,
        "topic": topic,
        "title": title,
        "outline_block": _format_outline(outline),
        "brief": brief,
    })

    with track_llm_call(job_id, "draft", "draft", spec["model"]) as record:
        response = llm.invoke([HumanMessage(content=prompt)])
        record(response)

    markdown = extract_text(response.content)
    word_count = len(markdown.split())
    logger.info("draft written words=%d chars=%d title=%r contract=%s",
                word_count, len(markdown), title, "yes" if contract else "no")

    draft_id = repo.insert_draft(
        job_id, version=0, title=title, markdown=markdown, word_count=word_count,
        outline_json=outline, produced_by=spec["model"],
    )

    return {"draft_markdown": markdown, "draft_title": title, "draft_id": draft_id}
