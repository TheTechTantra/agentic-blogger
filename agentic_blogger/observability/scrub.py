"""Redact secrets before anything reaches an MLflow artifact or trace.

autolog() captures full prompts and responses by default — an OAuth refresh
token or API key leaking into a trace is a realistic and bad failure.
"""

import re

_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|authorization|refresh_token|client_secret|bearer)"
)
_REDACTED = "[REDACTED]"


def scrub_text(text: str) -> str:
    """Redact 'key: value'-shaped substrings whose key looks secret.
    Best-effort — not a substitute for never logging real secrets."""
    def _redact_line(match: re.Match) -> str:
        return f"{match.group(1)}{_REDACTED}"

    pattern = re.compile(
        r"(" + _SECRET_KEY_PATTERN.pattern + r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"
    )
    return pattern.sub(lambda m: m.group(1) + _REDACTED, text)


def scrub_dict(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if _SECRET_KEY_PATTERN.search(str(k)):
            out[k] = _REDACTED
        elif isinstance(v, dict):
            out[k] = scrub_dict(v)
        elif isinstance(v, str):
            out[k] = scrub_text(v)
        else:
            out[k] = v
    return out
