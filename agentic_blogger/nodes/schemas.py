"""Pydantic schemas used with .with_structured_output() by the LLM-backed nodes.

Field descriptions are prompt surface: .with_structured_output() compiles them
into the tool JSON schema the model reads, so they steer output exactly like
prompt text does. They therefore live in the MLflow Prompt Registry with every
other prompt, not in this file — the classes below carry structure (names,
types, defaults, requiredness) and nothing the model reads as instruction.

Descriptions are applied by bind_descriptions() on first use and cached for
the life of the process. Consequence, accepted deliberately: a description
edit needs a worker restart to take effect, unlike the message prompts, which
honour the alias within the configured cache TTL.

Call bound(SomeModel) rather than passing a model class straight to
.with_structured_output(), or the model will see an undescribed schema.
"""

import json
import logging
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel, Field

from agentic_blogger.prompts import render

logger = logging.getLogger(__name__)

TIERS = ("beginner", "intermediate", "advanced", "pro")


class SourceItem(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""


class SourceList(BaseModel):
    sources: list[SourceItem] = Field(default_factory=list)


class CodeExemplar(BaseModel):
    """A runnable example lifted from a real source.

    `source_url` is required, not optional by oversight: an example with no
    traceable origin is exactly what this pipeline must not publish. The
    model-facing description of this class comes from the prompt registry.
    """

    tier: str
    language: str
    purpose: str
    code: str
    source_url: str
    source_title: str = ""
    version: str = ""
    adapted: bool = False
    how_to_verify: str = ""


class TierBrief(BaseModel):
    tier: str
    assumed_knowledge: str
    covers: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)


class PrimarySource(BaseModel):
    """The document a URL-sourced job was built from.

    Carried as structured fields rather than left inside the prose brief so
    the draft node can render an accurate credit line without re-parsing it —
    attribution is not something to leave to a second model's paraphrase. The
    model-facing description of this class comes from the prompt registry.
    """

    url: str = ""
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    venue: str = ""
    published: str = ""
    contribution: str = ""
    key_terms: list[str] = Field(default_factory=list)
    quotable: list[str] = Field(default_factory=list)


class ResearchBrief(BaseModel):
    brief: str
    primary_source: Optional[PrimarySource] = None
    landscape: str = ""
    tiers: list[TierBrief] = Field(default_factory=list)
    code_exemplars: list[CodeExemplar] = Field(default_factory=list)
    contested_or_unknown: list[str] = Field(default_factory=list)
    recommended_format: str = "single_post"
    series_plan: list[str] = Field(default_factory=list)


class OutlineSection(BaseModel):
    heading: str
    key_points: list[str] = Field(default_factory=list)
    tier: str = ""


class Outline(BaseModel):
    title_options: list[str]
    sections: list[OutlineSection]


class FactCheckFinding(BaseModel):
    claim: str
    verdict: str
    confidence: float
    source_url: Optional[str] = None
    fix: Optional[str] = None


class FactCheckReport(BaseModel):
    findings: list[FactCheckFinding] = Field(default_factory=list)


class SeoMeta(BaseModel):
    title: str
    meta_description: str
    labels: list[str] = Field(default_factory=list)
    slug: str


# Prompt name -> the classes whose descriptions that prompt carries. Each
# prompt's template is a JSON object keyed "ClassName.field_name", plus
# "ClassName.__doc__" for the class-level description.
SCHEMA_DESCRIPTION_PROMPTS: dict[str, tuple[type[BaseModel], ...]] = {
    "schema_research_brief_descriptions": (
        ResearchBrief, TierBrief, CodeExemplar, PrimarySource,
    ),
    "schema_outline_descriptions": (Outline, OutlineSection),
    "schema_factcheck_descriptions": (FactCheckReport, FactCheckFinding),
    "schema_seo_descriptions": (SeoMeta,),
}


def _apply(mapping: dict, models: tuple[type[BaseModel], ...], prompt_name: str) -> None:
    by_name = {m.__name__: m for m in models}
    for key, text in mapping.items():
        cls_name, _, attr = key.partition(".")
        model = by_name.get(cls_name)
        if model is None:
            logger.warning("prompt %s describes unknown class %r — ignoring",
                           prompt_name, cls_name)
            continue
        if attr == "__doc__":
            model.__doc__ = text
            continue
        field = model.model_fields.get(attr)
        if field is None:
            logger.warning("prompt %s describes unknown field %r on %s — ignoring",
                           prompt_name, attr, cls_name)
            continue
        field.description = text


@lru_cache(maxsize=1)
def bind_descriptions() -> None:
    """Pull every schema description from the registry and apply it.

    Cached: runs once per process. Raises PromptRegistryError if the registry
    is unreachable — the same hard-fail contract as every other prompt.
    """
    tiers = ", ".join(TIERS)
    for prompt_name, models in SCHEMA_DESCRIPTION_PROMPTS.items():
        # `tiers` is passed to every schema prompt whether or not its template
        # uses it. Extra variables are ignored by format(); only missing ones
        # raise.
        mapping = json.loads(render(prompt_name, {"tiers": tiers}))
        _apply(mapping, models, prompt_name)

    for models in SCHEMA_DESCRIPTION_PROMPTS.values():
        for model in models:
            model.model_rebuild(force=True)
    logger.info("schema descriptions bound from prompt registry")


def bound(model: type[BaseModel]) -> type[BaseModel]:
    """Return `model` with registry descriptions applied. Use this at every
    .with_structured_output() call site."""
    bind_descriptions()
    return model
