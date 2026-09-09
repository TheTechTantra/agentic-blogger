#!/usr/bin/env python3
"""
Seed the MLflow Prompt Registry with every prompt the pipeline needs.

This is the one-time bootstrap that moves prompt text out of Python and into
the registry. After it runs, the registry is the source of truth: edit prompts
in the MLflow UI, not here. Re-running this script pushes these texts as NEW
versions, which would revert any UI edits — so it is a bootstrap and a
disaster-recovery tool, not part of the normal edit loop.

The texts below are the verbatim prompts as they existed in the nodes before
the migration, with f-string interpolations rewritten as {{variables}}.

Usage:
    python -m scripts.register_prompts            # register + move alias
    python -m scripts.register_prompts --dry-run  # print what would happen
"""

import argparse
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from agentic_blogger.config.loader import prompts_spec
from agentic_blogger.nodes.schemas import (
    FactCheckReport,
    Outline,
    ResearchBrief,
    SeoMeta,
)
from agentic_blogger.prompts import specs as S

# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM = """You are a staff-level practitioner in the topic's field doing the reading behind a technical article — not a summarizer. You have shipped this technology, and you write for four distinct readers at once:
  - beginner: meeting the concepts for the first time; needs the mental model and the smallest thing that runs.
  - intermediate: has used it on a real task; needs the API surface, the idiomatic shape, and the mistakes that cost a day.
  - advanced: builds non-trivial systems with it; needs internals, performance characteristics, failure modes, and trade-offs against alternatives.
  - pro: peer expert; needs edge cases, version-specific behaviour, unresolved issues, spec/source-level detail, and where the received wisdom is wrong.

Evidence rules — these are hard constraints, not preferences:
  1. Prefer primary sources: official docs, specifications and RFCs, the project's own source repository, release notes and changelogs, maintainer posts, peer-reviewed papers. Treat SEO listicles, scraped tutorial farms, and undated blog spam as unusable.
  2. Every code example you report must come from a source you actually opened, and you must record the URL it came from. Never invent an example and never present a reconstructed one as quoted — mark it adapted.
  3. Record the version and date a behaviour was true for. Anything version-sensitive without a version is an unusable claim.
  4. Where sources disagree, or where you could not verify something, say so plainly. An honest gap is worth more than a confident guess."""

RESEARCH_URL_SYSTEM = """This job is built from a single source document supplied by the user. You are writing an explainer *about* that work for readers who have not read it — not a rewrite of it, not a summary that substitutes for it, and not a competing claim to its ideas.

Attribution rules — these are ethical constraints, not style preferences:
  1. Ideas, results, terminology, and framings that originate in the source belong to its authors. Record them as theirs, by name, so the downstream writer can attribute them inline. Never record one of their contributions as general knowledge.
  2. Capture the full citation: title, authors as printed, venue, date, and URL. An explainer that cannot name whose work it explains must not be written.
  3. Quote sparingly and exactly. Record verbatim passages only when the authors' own wording matters, keep them under 25 words, and never silently paraphrase a quote into your own prose.
  4. Do not reproduce the source's structure section by section, and do not reproduce its figures, tables, or full derivations. A reader must still have reason to read the original.
  5. Separate three things at all times: what the source claims, what is independently established, and what is your own commentary. Never imply the authors endorse your commentary.
  6. The value we add is explanatory — worked examples the source does not give, plainer statements of its concepts, trade-offs against alternatives, failure modes, and what it would take to use or reproduce the work. Gather that material actively; it is the reason the post exists."""

RESEARCH_USER = """Research the topic: "{{topic}}".

Run separate searches for each reader tier rather than one broad query — the beginner material and the pro material almost never live on the same page. At minimum search for: the official documentation and its getting-started path; the project's repository, release notes and open issues; known pitfalls and failure modes; performance or internals discussion; and whatever changed most recently.

Then write a research brief (600-900 words) synthesizing what you found, and follow it with these labelled sections:

LANDSCAPE: current stable version(s), release dates, what changed recently, and what is deprecated.

TIERS: one block per tier ({{tiers}}). For each — what the reader already knows and must not be re-taught, what this topic requires them to learn, the specific pitfalls at that level, and the URLs backing it.

CODE: concrete examples, at least one per tier, escalating in depth (beginner: minimal runnable; pro: the edge case or internals-level usage). For each example give the language, what it demonstrates, the code itself, the exact URL it came from, the version it was valid for, whether it is quoted or adapted, and how a reader verifies it runs. Do not include an example you cannot attribute.

UNRESOLVED: claims sources disagree on, or that you could not verify.

FORMAT: whether this should be one post covering all four tiers with clearly signposted sections, or a series with one post per tier — and why. Recommend a series only when the depth spread genuinely cannot be signposted inside one post; if you do, give a working title per part."""

RESEARCH_URL_ANGLE = """The reader-facing angle requested for the explainer is: "{{angle}}"."""

RESEARCH_URL_USER = """Fetch and read this document first: {{url}}

{{angle_line}}We are writing an explainer that helps a reader understand this work and credits it properly. Read it closely enough to teach it.

From the document itself, extract:
  - the full citation: title, authors as printed, venue or publisher, and date;
  - what its authors claim as their contribution, in their framing;
  - the concepts, terms, and notation it introduces that a reader must be taught before the contribution makes sense;
  - its actual method or argument, its results, and the conditions and assumptions those results depend on;
  - the limitations, threats to validity, or open questions its authors themselves state;
  - any code, data, or artifacts it points to.

Then research around it — this is what turns a summary into an explainer. Search for: prerequisite material for each concept it assumes; independent explanations, replications, critiques, or follow-on work; the alternatives it should be compared against and how it trades off against them; real implementations or usage; and whether anything it asserts has since been superseded. Run separate searches per reader tier rather than one broad query.

Then write a research brief (600-900 words) that explains the work and situates it, and follow it with these labelled sections:

SOURCE: the full citation — title, authors, venue, date, URL — plus the authors' stated contribution, the terms the explainer must define, and any short verbatim passages (under 25 words each) worth quoting. Every field the document states must be filled; write 'not stated' rather than guessing an author or a date.

LANDSCAPE: where this work sits — what preceded it, what competes with it, what has happened since, and whether its claims still hold.

TIERS: one block per tier ({{tiers}}). For each — what the reader already knows and must not be re-taught, which of this work's concepts they need explained and in what order, the specific places readers at that level misunderstand it, and the URLs backing it.

CODE: concrete examples that make the work's ideas runnable, at least one per tier. Prefer examples from the authors' own repository or from independent implementations over anything reconstructed. Every example carries the exact URL it came from, the version it was valid for, and quoted-or-adapted. Examples lifted from the source document carry that document's URL and are marked adapted unless quoted verbatim.

TRADEOFFS: where this approach wins, where it loses, what it costs, and what a reader should use instead when it does not fit. Attribute each trade-off to whoever established it — the authors, a critic, or your own reading.

UNRESOLVED: claims the source makes that other sources dispute, limitations its authors acknowledge, and anything you could not verify.

FORMAT: whether this should be one post covering all four tiers with clearly signposted sections, or a series with one post per tier — and why. Recommend a series only when the depth spread genuinely cannot be signposted inside one post; if you do, give a working title per part."""

RESEARCH_SYNTH_RULES_URL = """- primary_source is REQUIRED for this job: fill it from the SOURCE section of the research, with url exactly {{source_url}}. Copy the authors, title, venue and date as the research reports them; leave a field empty rather than inventing one. quotable entries must be verbatim and under 25 words."""

RESEARCH_SYNTH_RULES_NONE = """- primary_source must be null: this job had no source document."""

RESEARCH_SYNTH_USER = """Topic: {{topic}}

Structure the research below into the required schema. Rules:
{{primary_rules}}
- Carry every code example through with its source_url intact. Drop any example whose origin is not stated in the research — do not guess a plausible URL.
- Use only URLs that appear in the research or the source list.
- Assign each tier block and each code example to exactly one of: {{tiers}}.
- recommended_format must be 'single_post' or 'series'; populate series_plan only for 'series'.
- The `brief` field is the prose synthesis, verbatim from the research — do not re-summarize it.

--- SOURCES SEEN ---
{{source_lines}}

--- RESEARCH ---
{{raw}}"""

# ---------------------------------------------------------------------------
# outline / draft / attribution
# ---------------------------------------------------------------------------

OUTLINE_SOURCE_RULES = """This post EXPLAINS someone else's work for readers who have not read it:
  {{citation}}
Structure it accordingly:
- The opening section frames the work and credits its authors by name.
- Organize by what the reader needs to understand, in the order they need it. Do not mirror the original's own section order.
- At least two sections must carry what the original does not give the reader: worked examples, trade-offs against alternatives, failure modes, or how to actually use or reproduce it.
- The closing section points the reader to the original.
- Titles must not imply we produced the work or that its authors endorse this post."""

OUTLINE_USER = """Topic: {{topic}}

Research brief:
{{brief}}
{{source_rules}}
Produce a blog post outline: 3 candidate titles, and 4-7 sections each with a heading and 2-4 key points to cover."""

ATTRIBUTION_QUOTES = """Verbatim passages available to quote (use at most two, each under 25 words, each in a blockquote with the authors named):
{{quotes}}"""

ATTRIBUTION_QUOTES_EMPTY = """No verbatim passages were captured — do not invent a quotation."""

ATTRIBUTION_CONTRACT = """THIS POST EXPLAINS SOMEONE ELSE'S WORK. It is an explainer for readers who have not read the original, not a rewrite of it and not a replacement for it.

The work being explained:
  {{citation}}

Concepts from it the post must actually teach: {{terms}}

{{quote_block}}

Attribution requirements — non-negotiable:
  1. Credit the work within the first two sentences, naming its authors and linking the URL above. A reader must never be able to finish a paragraph believing these ideas are ours.
  2. Attribute inline, in the prose, every claim, result, term, and framing that originates in it — 'the authors show', 'the paper reports', 'X et al. define'. Do not collect attribution into a single reference at the bottom and treat the body as unsourced.
  3. State results as the authors' claims under their stated conditions, not as settled fact. Where they report numbers, say what was measured and under what assumptions.
  4. Never reproduce the work's structure section by section, and never reproduce its figures, tables, or full derivations. Explain in our own arrangement, chosen for the reader rather than for the original's outline.
  5. Keep our own commentary clearly ours and clearly separate. Never imply the authors endorse an opinion, extension, or criticism we add. Disagreement is allowed; misrepresentation is not.
  6. Do not overstate the work, and do not use its authors' or institution's name as an endorsement of this post.
  7. Close with a 'Read the original' line carrying the full citation and link, phrased so the reader understands the original is worth reading in full.

What makes this post worth publishing is what the original does not give the reader: plainer explanations of its concepts, worked examples and runnable code, the trade-offs against alternatives, the failure modes, and what it takes to actually use or reproduce the work. Lead with that value. If the post could be replaced by reading the abstract, it has failed."""

DRAFT_USER = """{{contract}}Write a complete, well-researched blog post about: {{topic}}

Use this title: {{title}}

Follow this outline:
{{outline_block}}

Research brief to draw on:
{{brief}}

Write the full post in Markdown (headings, no title heading itself — the title is set separately). Aim for 1200-1800 words. Be concrete, avoid filler, and don't fabricate specifics not supported by the brief."""

# ---------------------------------------------------------------------------
# factcheck / revise / seo
# ---------------------------------------------------------------------------

FACTCHECK_USER = """Fact-check the following blog post draft against the research brief. List every checkable factual claim with a verdict: 'supported' (backed by the brief), 'unsupported' (not backed by the brief, but not necessarily wrong), or 'contradicted' (conflicts with the brief). For unsupported/contradicted claims, suggest a fix.

--- RESEARCH BRIEF ---
{{brief}}

--- DRAFT ---
{{draft}}"""

REVISE_USER = """Revise the following blog post draft to address these fact-check findings. Fix or soften unsupported/contradicted claims per the suggested fix. Keep everything else intact. Return the full revised Markdown post.

--- FINDINGS ---
{{findings}}

--- DRAFT ---
{{draft}}"""

SEO_USER = """Given this blog post (working title: {{title}}), produce SEO metadata: a refined title (<=70 chars), a meta description (<=160 chars), up to 20 topical labels/tags, and a URL slug.

--- POST ---
{{draft}}"""

# ---------------------------------------------------------------------------
# schema field descriptions (JSON maps: "ClassName.field" -> description)
# ---------------------------------------------------------------------------

SCHEMA_RESEARCH_BRIEF = json.dumps({
    "ResearchBrief.brief": "600-900 word prose synthesis",
    "ResearchBrief.primary_source": "set only for URL-sourced jobs; the work being explained",
    "ResearchBrief.landscape": "current versions, dates, and what recently changed",
    "ResearchBrief.recommended_format": "one of: single_post, series",
    "ResearchBrief.series_plan": "one working title per part, if recommending a series",
    "TierBrief.tier": "one of: {{tiers}}",
    "TierBrief.assumed_knowledge": "what this reader already knows; do not re-explain it",
    "CodeExemplar.__doc__": (
        "A runnable example lifted from a real source. `source_url` is not optional by "
        "accident — an example with no traceable origin is exactly the thing this "
        "pipeline must not publish."
    ),
    "CodeExemplar.tier": "one of: {{tiers}}",
    "CodeExemplar.purpose": "what the reader learns by running this",
    "CodeExemplar.source_url": "the page this example came from or was adapted from",
    "CodeExemplar.version": "library/runtime version the example was valid for",
    "CodeExemplar.adapted": "True if reworked rather than quoted verbatim",
    "CodeExemplar.how_to_verify": "command or check that proves it runs",
    "PrimarySource.__doc__": (
        "The document a URL-sourced job was built from. Carried as structured fields "
        "rather than left inside the prose brief so the draft node can render an "
        "accurate credit line without re-parsing it."
    ),
    "PrimarySource.authors": "as printed on the work; empty if unattributed",
    "PrimarySource.venue": "journal, conference, publisher, or site",
    "PrimarySource.published": "publication or last-updated date, as stated",
    "PrimarySource.contribution": "what this work claims as new, in its authors' own framing",
    "PrimarySource.key_terms": "terms or concepts this work introduces that a reader must be taught",
    "PrimarySource.quotable": "short verbatim passages (<=25 words) worth quoting, with nothing paraphrased",
}, indent=2)

SCHEMA_OUTLINE = json.dumps({
    "OutlineSection.tier": "primary reader tier: one of {{tiers}}",
}, indent=2)

SCHEMA_FACTCHECK = json.dumps({
    "FactCheckFinding.verdict": "one of: supported, unsupported, contradicted",
}, indent=2)

# SeoMeta carries no descriptions today. Registered empty so they can be added
# in the UI later without a code change.
SCHEMA_SEO = json.dumps({}, indent=2)


PROMPTS: dict[str, str] = {
    S.RESEARCH_SYSTEM: RESEARCH_SYSTEM,
    S.RESEARCH_URL_SYSTEM: RESEARCH_URL_SYSTEM,
    S.RESEARCH_USER: RESEARCH_USER,
    S.RESEARCH_URL_USER: RESEARCH_URL_USER,
    S.RESEARCH_URL_ANGLE: RESEARCH_URL_ANGLE,
    S.RESEARCH_SYNTH_USER: RESEARCH_SYNTH_USER,
    S.RESEARCH_SYNTH_RULES_URL: RESEARCH_SYNTH_RULES_URL,
    S.RESEARCH_SYNTH_RULES_NONE: RESEARCH_SYNTH_RULES_NONE,
    S.OUTLINE_USER: OUTLINE_USER,
    S.OUTLINE_SOURCE_RULES: OUTLINE_SOURCE_RULES,
    S.DRAFT_USER: DRAFT_USER,
    S.ATTRIBUTION_CONTRACT: ATTRIBUTION_CONTRACT,
    S.ATTRIBUTION_QUOTES: ATTRIBUTION_QUOTES,
    S.ATTRIBUTION_QUOTES_EMPTY: ATTRIBUTION_QUOTES_EMPTY,
    S.FACTCHECK_USER: FACTCHECK_USER,
    S.REVISE_USER: REVISE_USER,
    S.SEO_USER: SEO_USER,
    "schema_research_brief_descriptions": SCHEMA_RESEARCH_BRIEF,
    "schema_outline_descriptions": SCHEMA_OUTLINE,
    "schema_factcheck_descriptions": SCHEMA_FACTCHECK,
    "schema_seo_descriptions": SCHEMA_SEO,
}

RESPONSE_FORMATS = {
    S.RESEARCH_SYNTH_USER: ResearchBrief,
    S.OUTLINE_USER: Outline,
    S.FACTCHECK_USER: FactCheckReport,
    S.SEO_USER: SeoMeta,
}


def _validate() -> list[str]:
    """Every manifest prompt must have text here, and every template variable
    must be one the manifest declares. Catches drift between specs.py and this
    file before anything is pushed."""
    import re

    pattern = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
    problems = []

    missing = set(S.SPECS) - set(PROMPTS)
    if missing:
        problems.append(f"no text for manifest prompts: {sorted(missing)}")
    extra = set(PROMPTS) - set(S.SPECS)
    if extra:
        problems.append(f"text for prompts not in the manifest: {sorted(extra)}")

    for name, template in PROMPTS.items():
        if name not in S.SPECS:
            continue
        declared = set(S.SPECS[name].variables)
        used = set(pattern.findall(template))
        undeclared = used - declared
        if undeclared:
            problems.append(f"{name}: uses undeclared variables {sorted(undeclared)}")
    return problems


def main(dry_run: bool) -> int:
    problems = _validate()
    if problems:
        for p in problems:
            logger.error("VALIDATION: %s", p)
        return 1
    logger.info("✓ %d prompts validated against the manifest", len(PROMPTS))

    alias = prompts_spec()["alias"]
    if dry_run:
        for name, template in sorted(PROMPTS.items()):
            rf = RESPONSE_FORMATS.get(name)
            logger.info("would register %-38s %5d chars  response_format=%s  alias=%s",
                        name, len(template), rf.__name__ if rf else "-", alias)
        return 0

    import mlflow.genai

    for name, template in sorted(PROMPTS.items()):
        kwargs = {}
        if name in RESPONSE_FORMATS:
            kwargs["response_format"] = RESPONSE_FORMATS[name]
        version = mlflow.genai.register_prompt(
            name=name,
            template=template,
            commit_message="seeded from scripts/register_prompts.py",
            tags={"pipeline": "blog", "managed_by": "register_prompts.py"},
            **kwargs,
        )
        mlflow.genai.set_prompt_alias(name, alias, version.version)
        logger.info("✓ %-38s v%-3d -> @%s", name, version.version, alias)

    logger.info("\nRegistered %d prompts. The registry is now the source of truth — "
                "edit in the MLflow UI, not in this script.", len(PROMPTS))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(main(ap.parse_args().dry_run))
