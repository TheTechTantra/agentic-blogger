"""SEO metadata node — structured output."""

import logging

from langchain_core.messages import HumanMessage

from agentic_blogger.config.loader import role_spec
from agentic_blogger.llm.callbacks import track_llm_call
from agentic_blogger.llm.registry import build_llm, with_resilience
from agentic_blogger.nodes.schemas import SeoMeta, bound
from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S

logger = logging.getLogger(__name__)


def seo_node(state: dict) -> dict:
    job_id = state["job_id"]
    draft = state.get("draft_markdown", "")
    title = state.get("draft_title", "")
    spec = role_spec("seo")

    llm = build_llm("seo").with_structured_output(bound(SeoMeta), include_raw=True)
    llm = with_resilience(llm, "seo")

    prompt = render(S.SEO_USER, content={"title": title, "draft": draft})

    with track_llm_call(job_id, "seo", "seo", spec["model"]) as record:
        result = llm.invoke([HumanMessage(content=prompt)])
        record(result["raw"])

    seo: SeoMeta = result["parsed"]
    seo_dict = seo.model_dump()
    seo_dict["labels"] = seo_dict["labels"][:20]  # Blogger caps at 20
    return {"seo_meta": seo_dict}
