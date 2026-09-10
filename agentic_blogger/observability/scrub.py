"""Redact secrets before anything reaches an MLflow artifact, trace, or log.

autolog() captures full prompts and responses by default — an OAuth refresh
token or API key leaking into a trace is a realistic and bad failure. The log
handler in log_setup.py uses scrub_text for the same reason.
"""

import re

# The inline (?i) used to live inside this pattern, which made it unusable as
# a fragment: Python 3.11+ rejects a global flag that is not at the start of
# the *composed* expression, so scrub_text raised re.PatternError on every
# call. It went unnoticed because nothing imported this module until the log
# filter did. Flags are passed to compile() now and the fragment stays plain.
_SECRET_KEY_SOURCE = (
    r"api[_-]?key|token|secret|authorization|refresh_token|client_secret|bearer"
)
_SECRET_KEY_PATTERN = re.compile(_SECRET_KEY_SOURCE, re.IGNORECASE)

# Compiled once: this runs on every log record.
_KV_PATTERN = re.compile(
    r"((?:" + _SECRET_KEY_SOURCE + r")[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def scrub_text(text: str) -> str:
    """Redact 'key: value'-shaped substrings whose key looks secret.
    Best-effort — not a substitute for never logging real secrets."""
    return _KV_PATTERN.sub(lambda m: m.group(1) + _REDACTED, text)


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
