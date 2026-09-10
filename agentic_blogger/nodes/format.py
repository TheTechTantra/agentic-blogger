"""Format node — markdown to sanitized HTML, plus beacon + fact-check comment.
No LLM call, no cost."""

import logging

import bleach
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

_ALLOWED_TAGS = [
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "img", "table", "thead", "tbody", "tr", "th", "td", "div", "span",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}

_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def format_node(state: dict) -> dict:
    job_id = state["job_id"]
    markdown = state.get("draft_markdown", "")
    findings = (state.get("factcheck_report") or {}).get("findings", [])

    raw_html = _md.render(markdown)
    safe_html = bleach.clean(raw_html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)

    unsupported = sum(1 for f in findings if f.get("verdict") == "unsupported")
    contradicted = sum(1 for f in findings if f.get("verdict") == "contradicted")

    beacon = f"<!-- agentic-blogger:job={job_id} v=1 -->"
    factcheck_comment = f"<!-- factcheck: {unsupported} unsupported, {contradicted} contradicted -->"

    full_html = f"{beacon}\n{factcheck_comment}\n{safe_html}"

    # Sanitization is silent by design, so the only way to notice bleach
    # eating content (a tag outside the allowlist) is the before/after delta.
    logger.info("formatted markdown=%dch html=%dch (sanitizer dropped %dch) "
                "unsupported=%d contradicted=%d",
                len(markdown), len(full_html), len(raw_html) - len(safe_html),
                unsupported, contradicted)

    return {"html": full_html}
