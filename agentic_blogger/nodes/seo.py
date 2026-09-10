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
    proposed = seo_dict["labels"]
    seo_dict["labels"] = proposed[:20]  # Blogger caps at 20
    if len(proposed) > 20:
        logger.warning("seo returned %d labels — dropped %d over Blogger's count cap: %s",
                       len(proposed), len(proposed) - 20,
                       ", ".join(repr(x) for x in proposed[20:]))
    # The character cap is enforced at the API boundary (blogger_client
    # _fit_labels), but log the joined length here so an over-length set is
    # attributable to the seo call that produced it.
    logger.info("seo title=%r (%dch) labels=%d/%dch slug=%r meta_description=%dch",
                seo_dict.get("title"), len(seo_dict.get("title") or ""),
                len(seo_dict["labels"]), len(",".join(seo_dict["labels"])),
                seo_dict.get("slug"), len(seo_dict.get("meta_description") or ""))
    return {"seo_meta": seo_dict}
