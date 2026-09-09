"""Revise node — repairs flagged claims. Bounded by MAX_REVISIONS; if the
repair itself fails or still has issues, the pipeline still proceeds to
publish with counts surfaced (decision 4) — this node is not the last word."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.db import repo
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.llm.text import extract_text
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)


def revise_node(state: dict) -> dict:
    job_id = state["job_id"]
    draft = state.get("draft_markdown", "")
    title = state.get("draft_title", "")
    from_draft_id = state.get("draft_id")
    findings = (state.get("factcheck_report") or {}).get("findings", [])
    spec = role_spec("draft")  # revision uses the same model as drafting

    flagged = [f for f in findings if f.get("verdict") in ("unsupported", "contradicted")]

    prompt = render(S.REVISE_USER, content={"findings": flagged, "draft": draft})

    llm = with_resilience(build_llm("draft"), "draft")
    with track_llm_call(job_id, "revise", "draft", spec["model"]) as record:
        response = llm.invoke([HumanMessage(content=prompt)])
        record(response)

    revised = extract_text(response.content)
    word_count = len(revised.split())

    to_draft_id = repo.insert_draft(
        job_id, version=state.get("revision_count", 0) + 1, title=title,
        markdown=revised, word_count=word_count, produced_by=spec["model"],
    )
    repo.insert_revision(job_id, from_draft_id, to_draft_id, reason="factcheck_findings",
                          findings=flagged)

    return {
        "draft_markdown": revised,
        "draft_id": to_draft_id,
        "revision_count": state.get("revision_count", 0) + 1,
    }
