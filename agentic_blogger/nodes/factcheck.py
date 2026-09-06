"""Fact-check node — surfaces findings, never blocks the pipeline (decision 4)."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.schemas import FactCheckReport

logger = logging.getLogger(__name__)

MAX_REVISIONS = 1


def factcheck_node(state: dict) -> dict:
    job_id = state["job_id"]
    draft = state.get("draft_markdown", "")
    brief = (state.get("research_brief") or {}).get("brief", "")
    spec = role_spec("factcheck")

    llm = build_llm("factcheck").with_structured_output(FactCheckReport, include_raw=True)
    llm = with_resilience(llm, "factcheck")

    prompt = (
        "Fact-check the following blog post draft against the research brief. "
        "List every checkable factual claim with a verdict: 'supported' (backed "
        "by the brief), 'unsupported' (not backed by the brief, but not "
        "necessarily wrong), or 'contradicted' (conflicts with the brief). For "
        "unsupported/contradicted claims, suggest a fix.\n\n"
        f"--- RESEARCH BRIEF ---\n{brief}\n\n--- DRAFT ---\n{draft}"
    )

    with track_llm_call(job_id, "factcheck", "factcheck", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    report: FactCheckReport = result["parsed"]
    findings = [f.model_dump() for f in report.findings]

    logger.info("job=%s factcheck findings=%d", job_id, len(findings))
    return {"factcheck_report": {"findings": findings}}


def needs_revision(state: dict) -> str:
    findings = (state.get("factcheck_report") or {}).get("findings", [])
    flagged = [f for f in findings if f.get("verdict") in ("unsupported", "contradicted")]
    if flagged and state.get("revision_count", 0) < MAX_REVISIONS:
        return "revise"
    return "continue"
