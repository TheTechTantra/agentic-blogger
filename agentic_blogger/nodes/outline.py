"""Outline node — structured output over the research brief."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.schemas import Outline

logger = logging.getLogger(__name__)


def outline_node(state: dict) -> dict:
    job_id = state["job_id"]
    topic = state["topic"]
    brief = (state.get("research_brief") or {}).get("brief", "")
    spec = role_spec("outline")

    llm = build_llm("outline").with_structured_output(Outline, include_raw=True)
    llm = with_resilience(llm, "outline")

    prompt = (
        f"Topic: {topic}\n\nResearch brief:\n{brief}\n\n"
        "Produce a blog post outline: 3 candidate titles, and 4-7 sections "
        "each with a heading and 2-4 key points to cover."
    )

    with track_llm_call(job_id, "outline", "outline", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    outline: Outline = result["parsed"]
    return {"outline_plan": outline.model_dump()}
