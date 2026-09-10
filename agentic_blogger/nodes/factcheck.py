"""Fact-check node — surfaces findings, never blocks the pipeline (decision 4)."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.schemas import FactCheckReport, bound
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)

MAX_REVISIONS = 1


def factcheck_node(state: dict) -> dict:
    job_id = state["job_id"]
    draft = state.get("draft_markdown", "")
    brief = (state.get("research_brief") or {}).get("brief", "")
    spec = role_spec("factcheck")

    llm = build_llm("factcheck").with_structured_output(bound(FactCheckReport), include_raw=True)
    llm = with_resilience(llm, "factcheck")

    prompt = render(S.FACTCHECK_USER, content={"brief": brief, "draft": draft})

    with track_llm_call(job_id, "factcheck", "factcheck", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    report: FactCheckReport = result["parsed"]
    findings = [f.model_dump() for f in report.findings]

    verdicts: dict[str, int] = {}
    for f in findings:
        verdicts[f.get("verdict", "unknown")] = verdicts.get(f.get("verdict", "unknown"), 0) + 1
    logger.info("factcheck findings=%d verdicts=%s", len(findings),
                ",".join(f"{k}={v}" for k, v in sorted(verdicts.items())) or "-")
    for f in findings:
        if f.get("verdict") in ("unsupported", "contradicted"):
            # The flagged claims are what the revise node will act on; without
            # them the log says a revision happened but never says why.
            logger.info("flagged [%s] %r (source=%s)", f.get("verdict"),
                        (f.get("claim") or "")[:160], f.get("source_url") or "-")
    return {"factcheck_report": {"findings": findings}}


def needs_revision(state: dict) -> str:
    findings = (state.get("factcheck_report") or {}).get("findings", [])
    flagged = [f for f in findings if f.get("verdict") in ("unsupported", "contradicted")]
    if flagged and state.get("revision_count", 0) < MAX_REVISIONS:
        return "revise"
    return "continue"
