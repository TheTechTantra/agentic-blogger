"""Outline node — structured output over the research brief."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.attribution import citation
from agentic_blogger.nodes.schemas import Outline, bound
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)


def outline_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    research = state.get("research_brief") or {}
    brief = research.get("brief", "")
    primary_source = research.get("primary_source") or {}
    spec = role_spec("outline")

    llm = build_llm("outline").with_structured_output(bound(Outline), include_raw=True)
    llm = with_resilience(llm, "outline")

    # The draft node enforces attribution, but structure is decided here: a
    # section-per-section mirror of the original is a rewrite no matter how
    # carefully the prose credits it.
    source_rules = (
        render(S.OUTLINE_SOURCE_RULES, content={"citation": citation(primary_source)})
        if primary_source else ""
    )

    prompt = render(S.OUTLINE_USER, content={
        "topic": topic,
        "brief": brief,
        "source_rules": source_rules,
    })

    with track_llm_call(job_id, "outline", "outline", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    outline: Outline = result["parsed"]
    return {"outline_plan": outline.model_dump()}
