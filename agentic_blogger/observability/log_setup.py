"""Logging setup shared by the orchestrator and the bot.

Two problems this solves, both of which made the logs untraceable.

First, correlation. A job touches eight nodes, two LLM roles per node in
places, the prompt registry, Postgres and Blogger, and every one of those
logged from its own module with no shared key. Reconstructing a run meant
guessing from timestamps. `bind_job`/`bind_node` put the job id and the
current node into a ContextVar, and a filter stamps them onto every record
that passes through the root handler — including records from libraries that
know nothing about this application.

ContextVars and not thread-locals, but bound *inside* the node function
rather than around the graph invocation: LangGraph runs sync nodes on an
executor, and a context set on the calling thread is not guaranteed to reach
the worker that actually runs the node. Binding in the node body always runs
on the thread doing the work, so it is correct either way.

Second, secrets. The bot's HTTP client logs full request URLs, and for the
Telegram API the bot token is a path segment — every poll printed it. That is
why httpx was silenced entirely, which also cost us the request-level
heartbeat that made "in flight" distinguishable from "hung". Redaction
restores the useful half: the filter rewrites the token out of the message
before it is emitted, so httpx can log at INFO again.

The redaction is a backstop, not a licence — never pass a secret to a logger
on purpose.
"""

import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from agentic_blogger.observability.scrub import scrub_text

_job_id: ContextVar[str] = ContextVar("job_id", default="")
_node: ContextVar[str] = ContextVar("node", default="")

# Telegram puts the bot token in the URL path: /bot<id>:<hash>/getUpdates.
# Keep the numeric id — it identifies which bot without being the credential —
# and drop the secret half.
_TELEGRAM_TOKEN_RE = re.compile(r"(/bot\d{5,}):[A-Za-z0-9_-]{20,}")
# Google/OAuth style bearer values that scrub_text's key=value shape misses.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}")

# Cheap prefilter: skip the regex work on the overwhelming majority of lines
# that contain nothing secret-shaped.
_SUSPECT = re.compile(r"(?i)/bot\d|bearer |token|secret|api[_-]?key|authorization")


def _redact(message: str) -> str:
    if not _SUSPECT.search(message):
        return message
    message = _TELEGRAM_TOKEN_RE.sub(r"\1:[REDACTED]", message)
    message = _BEARER_RE.sub(r"\1[REDACTED]", message)
    return scrub_text(message)


class RedactingFilter(logging.Filter):
    """Rewrite credential-shaped substrings out of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            original = record.getMessage()
        except Exception:
            return True
        redacted = _redact(original)
        if redacted != original:
            # Collapse args into the message: they have already been
            # interpolated, and leaving them would re-interpolate a string
            # that no longer has placeholders.
            record.msg = redacted
            record.args = ()
        return True


# Long polling means one httpx line per getUpdates round trip — roughly every
# 10 seconds, ~8.6k lines a day, all identical and all saying nothing beyond
# "the bot is still polling". Startup (getMe, deleteWebhook) already proves
# connectivity, and any poll that does NOT come back 2xx is still printed.
_POLL_LINE_RE = re.compile(r"/getUpdates .*HTTP/[\d.]+ 2\d\d")
# Not silence: a bot that has logged nothing for hours is the same
# dead-or-idle ambiguity the worker's idle heartbeat exists to remove. One
# summary line per interval, produced by rewriting a poll record in place
# rather than emitting a new one — logging from inside a filter reenters the
# handler.
_POLL_SUMMARY_EVERY_S = 1800


class PollNoiseFilter(logging.Filter):
    """Collapse successful Telegram long-poll lines into a periodic summary."""

    def __init__(self) -> None:
        super().__init__()
        self._suppressed = 0
        self._last_summary = time.monotonic()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "httpx" or record.levelno > logging.INFO:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if not _POLL_LINE_RE.search(message):
            return True

        self._suppressed += 1
        now = time.monotonic()
        if now - self._last_summary < _POLL_SUMMARY_EVERY_S:
            return False

        elapsed = int((now - self._last_summary) / 60)
        record.msg = (f"telegram long-poll alive: {self._suppressed} successful "
                      f"getUpdates in the last {elapsed}m (individual lines suppressed)")
        record.args = ()
        self._suppressed = 0
        self._last_summary = now
        return True


class ContextFilter(logging.Filter):
    """Stamp the current job/node onto the record as a `ctx` field."""

    def filter(self, record: logging.LogRecord) -> bool:
        parts = []
        job = _job_id.get()
        if job:
            parts.append(f"job={job}")
        node = _node.get()
        if node:
            parts.append(f"node={node}")
        # Rendered as a bracketed group so an unbound record (startup, the
        # poll loop) reads cleanly instead of carrying empty placeholders.
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return True


_FORMAT = "%(asctime)s [%(levelname)s] %(name)s%(ctx)s: %(message)s"

# Libraries that are useful at INFO for tracing but unusable at DEBUG.
# httpx is deliberately INFO now that RedactingFilter runs: one line per
# outbound request is the cheapest possible proof that a call left the
# container, which is exactly what was missing while it was silenced.
_LIBRARY_LEVELS = {
    "httpx": logging.INFO,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "telegram": logging.INFO,
    "telegram.ext": logging.INFO,
    # LangChain/LangGraph DEBUG is a firehose of serialized state.
    "langchain": logging.INFO,
    "langgraph": logging.INFO,
    "anthropic": logging.INFO,
    "openai": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "googleapiclient.discovery_cache": logging.ERROR,
    "mlflow": logging.WARNING,
}


def setup_logging(service: str) -> None:
    """Configure root logging for a service entrypoint. Idempotent.

    LOG_LEVEL sets the application level (default INFO); set it to DEBUG to
    get per-call payload sizes and prompt resolution without a code change.
    """
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactingFilter())
    handler.addFilter(PollNoiseFilter())

    root = logging.getLogger()
    # force=True so a library that called basicConfig() first (or a re-import
    # of this module) cannot leave a second, unfiltered handler attached —
    # that handler would print the unredacted message.
    logging.basicConfig(level=level, handlers=[handler], force=True)
    root.setLevel(level)

    for name, lib_level in _LIBRARY_LEVELS.items():
        # Never make a library noisier than the application level asked for.
        logging.getLogger(name).setLevel(max(lib_level, level))

    logging.getLogger(__name__).info(
        "logging configured service=%s level=%s", service, logging.getLevelName(level)
    )


@contextmanager
def bind_job(job_id: str):
    """Tag every log record emitted in this block with the job id."""
    token = _job_id.set(job_id)
    try:
        yield
    finally:
        _job_id.reset(token)


@contextmanager
def bind_node(node: str):
    """Tag every log record emitted in this block with the node name."""
    token = _node.set(node)
    try:
        yield
    finally:
        _node.reset(token)


def current_job() -> str:
    return _job_id.get()


def summarize(value) -> str:
    """One-token-ish description of a state value, for entry/exit logs.

    Sizes rather than contents: a draft is 30 KB of markdown and the useful
    signal is that it exists and roughly how big it got, not what it says.
    """
    if value is None:
        return "none"
    if isinstance(value, str):
        return f"{len(value)}ch"
    if isinstance(value, (list, tuple, set)):
        return f"[{len(value)}]"
    if isinstance(value, dict):
        # Keys, not values: the dicts here are briefs and reports whose
        # contents are kilobytes, but whose *shape* is the thing that
        # silently degrades (a ResearchBrief that fell back to prose-only
        # has one key instead of seven).
        return "{" + ",".join(sorted(value)[:8]) + ("...}" if len(value) > 8 else "}")
    if isinstance(value, bool):
        return str(value)
    return str(value)


def summarize_state(state: dict, keys: tuple[str, ...] = ()) -> str:
    """Render selected state keys as `key=summary` pairs, skipping absent ones."""
    items = []
    for key in (keys or tuple(state)):
        if key in state:
            items.append(f"{key}={summarize(state[key])}")
    return " ".join(items) or "(empty)"
