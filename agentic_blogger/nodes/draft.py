"""Draft node — writes the full markdown post from the outline + research brief."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.db import repo
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.llm.text import extract_text

logger = logging.getLogger(__name__)


def _format_outline(outline: dict) -> str:
    lines = []
    for section in outline.get("sections", []):
        lines.append(f"- {section['heading']}: {', '.join(section.get('key_points', []))}")
    return "\n".join(lines)


def draft_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    brief = (state.get("research_brief") or {}).get("brief", "")
    outline = state.get("outline_plan") or {}
    spec = role_spec("draft")

    llm = with_resilience(build_llm("draft"), "draft")

    title = (outline.get("title_options") or [topic])[0]
    prompt = (
        f"Write a complete, well-researched blog post about: {topic}\n\n"
        f"Use this title: {title}\n\n"
        f"Follow this outline:\n{_format_outline(outline)}\n\n"
        f"Research brief to draw on:\n{brief}\n\n"
        "Write the full post in Markdown (headings, no title heading itself — "
        "the title is set separately). Aim for 1200-1800 words. Be concrete, "
        "avoid filler, and don't fabricate specifics not supported by the brief."
    )

    with track_llm_call(job_id, "draft", "draft", spec["model"]) as record:
        response = llm.invoke([HumanMessage(content=prompt)])
        record(response)

    markdown = extract_text(response.content)
    word_count = len(markdown.split())

    draft_id = repo.insert_draft(
        job_id, version=0, title=title, markdown=markdown, word_count=word_count,
        outline_json=outline, produced_by=spec["model"],
    )

    return {"draft_markdown": markdown, "draft_title": title, "draft_id": draft_id}
