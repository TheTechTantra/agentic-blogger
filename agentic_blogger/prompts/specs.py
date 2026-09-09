"""The manifest of every prompt the pipeline requires.

One place listing what must exist in the registry, what variables each
template is allowed to reference, and which structured-output schema (if any)
it belongs to. Preflight checks the registry against this; register_prompts.py
seeds the registry to match it.

`content` marks variables carrying model- or user-generated text. Those are
substituted last (see registry.render) so generated text that happens to look
like template syntax is never itself rewritten.

Conditional prompt text is registered as a separate named prompt rather than
as a variable, because the two branches are different English, not the same
sentence with a hole in it. Code picks the branch; the registry owns the words.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

from agentic_blogger.nodes.schemas import (
    SCHEMA_DESCRIPTION_PROMPTS,
    FactCheckReport,
    Outline,
    ResearchBrief,
    SeoMeta,
)


@dataclass(frozen=True)
class PromptSpec:
    name: str
    variables: frozenset[str] = frozenset()
    content: frozenset[str] = frozenset()
    response_format: Optional[type[BaseModel]] = None
    note: str = ""

    def __post_init__(self) -> None:
        unknown = self.content - self.variables
        if unknown:
            raise ValueError(
                f"{self.name}: content vars not declared in variables: {sorted(unknown)}"
            )


def _spec(name, variables=(), content=(), response_format=None, note="") -> PromptSpec:
    return PromptSpec(
        name=name,
        variables=frozenset(variables),
        content=frozenset(content),
        response_format=response_format,
        note=note,
    )


# --- research -------------------------------------------------------------

RESEARCH_SYSTEM = "research_system"
RESEARCH_URL_SYSTEM = "research_url_system"
RESEARCH_USER = "research_user"
RESEARCH_URL_USER = "research_url_user"
RESEARCH_URL_ANGLE = "research_url_angle"
RESEARCH_SYNTH_USER = "research_synth_user"
RESEARCH_SYNTH_RULES_URL = "research_synth_primary_rules_url"
RESEARCH_SYNTH_RULES_NONE = "research_synth_primary_rules_none"

# --- outline / draft ------------------------------------------------------

OUTLINE_USER = "outline_user"
OUTLINE_SOURCE_RULES = "outline_source_rules"
DRAFT_USER = "draft_user"
ATTRIBUTION_CONTRACT = "attribution_explainer_contract"
ATTRIBUTION_QUOTES = "attribution_quote_block"
ATTRIBUTION_QUOTES_EMPTY = "attribution_quote_block_empty"

# --- review / publish prep ------------------------------------------------

FACTCHECK_USER = "factcheck_user"
REVISE_USER = "revise_user"
SEO_USER = "seo_user"


_MESSAGE_SPECS = [
    _spec(RESEARCH_SYSTEM,
          note="system prompt for both topic and URL research jobs"),
    _spec(RESEARCH_URL_SYSTEM,
          note="appended to research_system for URL jobs only"),
    _spec(RESEARCH_USER,
          variables=("topic", "tiers"), content=("topic",)),
    _spec(RESEARCH_URL_USER,
          variables=("url", "angle_line", "tiers"), content=("url", "angle_line")),
    _spec(RESEARCH_URL_ANGLE,
          variables=("angle",), content=("angle",),
          note="omitted entirely when the angle is absent or equals the URL"),
    _spec(RESEARCH_SYNTH_USER,
          variables=("topic", "primary_rules", "tiers", "source_lines", "raw"),
          content=("topic", "primary_rules", "source_lines", "raw"),
          response_format=ResearchBrief),
    _spec(RESEARCH_SYNTH_RULES_URL,
          variables=("source_url",), content=("source_url",)),
    _spec(RESEARCH_SYNTH_RULES_NONE),

    _spec(OUTLINE_USER,
          variables=("topic", "brief", "source_rules"),
          content=("topic", "brief", "source_rules"),
          response_format=Outline),
    _spec(OUTLINE_SOURCE_RULES,
          variables=("citation",), content=("citation",)),

    _spec(DRAFT_USER,
          variables=("contract", "topic", "title", "outline_block", "brief"),
          content=("contract", "topic", "title", "outline_block", "brief")),
    _spec(ATTRIBUTION_CONTRACT,
          variables=("citation", "terms", "quote_block"),
          content=("citation", "terms", "quote_block")),
    _spec(ATTRIBUTION_QUOTES,
          variables=("quotes",), content=("quotes",)),
    _spec(ATTRIBUTION_QUOTES_EMPTY),

    _spec(FACTCHECK_USER,
          variables=("brief", "draft"), content=("brief", "draft"),
          response_format=FactCheckReport),
    _spec(REVISE_USER,
          variables=("findings", "draft"), content=("findings", "draft")),
    _spec(SEO_USER,
          variables=("title", "draft"), content=("title", "draft"),
          response_format=SeoMeta),
]

# Schema-description prompts: JSON maps of "ClassName.field" -> description.
# `tiers` is offered to all of them; templates that ignore it are fine.
_SCHEMA_SPECS = [
    _spec(name, variables=("tiers",), note="JSON map of schema field descriptions")
    for name in SCHEMA_DESCRIPTION_PROMPTS
]

SPECS: dict[str, PromptSpec] = {s.name: s for s in (*_MESSAGE_SPECS, *_SCHEMA_SPECS)}


def spec(name: str) -> PromptSpec:
    try:
        return SPECS[name]
    except KeyError:
        raise KeyError(f"Unknown prompt {name!r} — add it to prompts/specs.py") from None
