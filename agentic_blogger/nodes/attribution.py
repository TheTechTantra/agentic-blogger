"""Attribution shared by the outline and draft nodes.

A URL-sourced post explains someone else's work. The research node captures
who that someone is (ResearchBrief.primary_source); this module is what turns
that into instructions the writing nodes cannot quietly drop. It lives outside
draft.py because outline and draft must agree on the credit line — a post that
credits the authors in the draft but structures itself as a section-by-section
retelling is still a rewrite.
"""

from agentic_blogger.prompts import render
from agentic_blogger.prompts import specs as S


def citation(source: dict) -> str:
    """Render a primary_source dict as a single citation string. Fields the
    research could not establish are omitted rather than filled with
    placeholders — a citation with a guessed author is worse than a short one."""
    if not source:
        return ""
    parts = []
    if source.get("title"):
        parts.append(f'"{source["title"]}"')
    authors = source.get("authors") or []
    if authors:
        if len(authors) > 3:
            parts.append(f"{authors[0]} et al.")
        else:
            parts.append(", ".join(authors))
    if source.get("venue"):
        parts.append(source["venue"])
    if source.get("published"):
        parts.append(source["published"])
    line = " — ".join(parts)
    url = source.get("url", "")
    return f"{line} ({url})" if url else line


def explainer_contract(source: dict) -> str:
    """The rules the draft must follow when the post explains another work.

    Text comes from the prompt registry; this function only decides which
    branch applies and renders the quote list.
    """
    quotes = source.get("quotable") or []
    if quotes:
        quote_block = render(
            S.ATTRIBUTION_QUOTES,
            content={"quotes": "\n".join(f"  - {q}" for q in quotes)},
        )
    else:
        quote_block = render(S.ATTRIBUTION_QUOTES_EMPTY)

    return render(S.ATTRIBUTION_CONTRACT, content={
        "citation": citation(source),
        "terms": ", ".join(source.get("key_terms") or []) or "(none recorded)",
        "quote_block": quote_block,
    })
