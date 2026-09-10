"""Loading prompt text from the MLflow Prompt Registry.

The registry is the single source of truth for every prompt this pipeline
sends to a model. There is deliberately NO fallback: no bundled defaults, no
local cache we maintain, no last-known-good copy. If the registry cannot be
reached the job fails and someone fixes the registry. The repo's exported
prompt files are a backup for disaster recovery and are never read here.

This is a conscious departure from the fail-open rule in
observability/mlflow_setup.py. Tracing still degrades silently; prompts do
not, because a job that ran on the wrong prompt text produces a plausible
wrong answer instead of an obvious error.

Tracking URI setup is duplicated from mlflow_setup rather than shared, so a
tracing init failure (which is swallowed by design) can never leave prompt
loading pointed at the wrong server.
"""

import logging
import os
import threading

from agentic_blogger.config.loader import prompts_spec

logger = logging.getLogger(__name__)

_tracking_uri_set = False

# Which prompts have been resolved but not yet attributed to a node_run.
#
# Drain-based rather than a context manager wrapping the LLM call, because
# nodes render their prompts *before* entering track_llm_call — a collector
# scoped to the invocation would capture nothing. load() always appends here;
# track_llm_call drains at record time, so a node_run row gets exactly the
# prompts resolved since the previous row was written.
#
# Thread-local: LangGraph may run nodes on worker threads, and one job's
# prompts must never land on another's row.
_local = threading.local()


def _accumulator() -> list:
    if not hasattr(_local, "used"):
        _local.used = []
    return _local.used


def drain() -> list:
    """Return the prompts resolved since the last drain, and reset."""
    used = _accumulator()
    out = list(used)
    used.clear()
    return out


class PromptRegistryError(RuntimeError):
    """Prompt text could not be loaded. Terminal — there is no fallback."""


def _configure() -> None:
    global _tracking_uri_set
    if _tracking_uri_set:
        return
    import mlflow

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000"))
    _tracking_uri_set = True


def _fetch(uri: str, ttl: float):
    import mlflow.genai

    # link_to_model=False: the default tries to associate the prompt with an
    # active model context, which this pipeline does not have. The association
    # we care about is prompt-to-node_run, recorded in Postgres by callbacks.
    return mlflow.genai.load_prompt(uri, cache_ttl_seconds=ttl, link_to_model=False)


def load(name: str):
    """Return the PromptVersion registered under `name` at the configured alias.

    Retries are MLflow's own (MLFLOW_HTTP_REQUEST_MAX_RETRIES / _TIMEOUT /
    _BACKOFF_FACTOR, set on the orchestrator and telegram services in
    docker-compose.yml). This call is deliberately NOT wrapped in the shared
    `resilience:` policy: MLflow already retries internally, and stacking the
    two multiplied out to 35 HTTP attempts per prompt — a registry outage took
    over five minutes to fail a single lookup instead of failing fast.

    Raises PromptRegistryError on failure. Callers must not catch this to
    substitute a default — the job is expected to fail.
    """
    cfg = prompts_spec()
    uri = f"prompts:/{name}@{cfg['alias']}"
    _configure()
    try:
        prompt = _fetch(uri, cfg["cache_ttl_seconds"])
    except Exception as e:
        logger.error("prompt load failed uri=%s: %s: %s", uri, type(e).__name__, e)
        raise PromptRegistryError(f"could not load prompt {uri}: {e}") from e

    acc = _accumulator()
    if not any(u["name"] == name for u in acc):
        # First resolution of this prompt for the pending node_run. Logged at
        # INFO because "which version of the prompt ran" is the question a bad
        # output raises, and the alias moves independently of any deploy.
        logger.info("prompt %s@%s resolved to v%s", name, cfg["alias"], prompt.version)
        acc.append({"name": name, "version": prompt.version, "alias": cfg["alias"]})
    return prompt


def render(name: str, variables: dict | None = None, content: dict | None = None) -> str:
    """Load `name` and substitute its {{variables}}.

    `variables` are short, code-controlled values (tier names, a topic, a
    citation line). `content` carries model- or user-generated text (a draft,
    a research brief, fact-check findings).

    The split exists because MLflow's format_prompt runs a sequential re.sub
    over the accumulating string, so text substituted early is still visible
    to later substitutions. Passing content last means a draft that happens to
    contain "{{topic}}" is never itself rewritten. Plain braces are harmless —
    the pattern only matches {{identifier}} — so this guards against generated
    text that mimics template syntax, not against ordinary punctuation.

    Missing variables raise: PromptVersion.format defaults to
    allow_partial=False, so a prompt edited to reference a variable the node
    does not supply fails loudly here rather than reaching a model with an
    unsubstituted placeholder in it.
    """
    prompt = load(name)
    ordered = dict(variables or {})
    ordered.update(content or {})
    try:
        return prompt.format(**ordered)
    except Exception as e:
        raise PromptRegistryError(
            f"could not render prompt {name!r} (expects {sorted(prompt.variables)}, "
            f"given {sorted(ordered)}): {e}"
        ) from e
