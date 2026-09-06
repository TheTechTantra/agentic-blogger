"""Format node — markdown to sanitized HTML, plus beacon + fact-check comment
+ disclaimer. No LLM call, no cost."""

import logging
from pathlib import Path

import bleach
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path("/app/assets") if Path("/app/assets").exists() else (
    Path(__file__).resolve().parents[2] / "assets"
)

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


def _load_disclaimer() -> str:
    path = _ASSETS_DIR / "disclaimer.txt"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        logger.warning("assets/disclaimer.txt not found — publishing without disclaimer text")
        return ""


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

    disclaimer_text = _load_disclaimer()
    disclaimer_html = f'<div class="ai-disclaimer"><p>{bleach.clean(disclaimer_text, tags=[], strip=True)}</p></div>' if disclaimer_text else ""

    full_html = f"{beacon}\n{factcheck_comment}\n{safe_html}\n{disclaimer_html}"

    return {"html": full_html}
