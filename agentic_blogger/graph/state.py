"""BlogState — the shape passed between graph nodes.

Every node returns only the keys it changes (LangGraph merges deltas into
this state). Sub-objects are plain dicts, not Pydantic models — simpler for
the checkpointer to serialize and no different in practice, since nodes
validate their own inputs/outputs at the boundary.
"""

import operator
from typing import Annotated, Optional, TypedDict


class BlogState(TypedDict, total=False):
    job_id: str
    topic: str
    # Set only for URL-sourced jobs (/url in Telegram). When present the
    # research node fetches this page server-side (Anthropic web_fetch) and
    # treats it as the primary source instead of searching blind.
    source_url: Optional[str]

    sources: Annotated[list[dict], operator.add]  # [{url, title, snippet}]
    # Note: these can't be named after their node ("research", "outline",
    # "factcheck", "seo") — LangGraph forbids a node name that matches a
    # state key.
    # ResearchBrief.model_dump() — {"brief", "landscape", "tiers",
    # "code_exemplars", "contested_or_unknown", "recommended_format",
    # "series_plan"}. Degrades to just {"brief": str} if structuring failed.
    research_brief: Optional[dict]
    outline_plan: Optional[dict]        # {"title_options": [...], "sections": [...]}
    draft_markdown: Optional[str]
    draft_title: Optional[str]
    draft_id: Optional[str]
    factcheck_report: Optional[dict]    # {"findings": [{claim, verdict, confidence, source_url, fix}]}
    revision_count: int
    seo_meta: Optional[dict]            # {"title", "meta_description", "labels", "slug"}
    html: Optional[str]
    published: Optional[dict]           # {"remote_post_id", "remote_url", "state"}
