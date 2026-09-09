"""Shared retry policy for outbound service calls we retry ourselves.

Before this module there were two sets of hardcoded numbers: tenacity's
`stop_after_attempt(5)` in the Blogger client and `defaults.max_retries: 3`
read by the LLM registry. Both now come from the `resilience:` block in
config/models.yaml.

MLflow is the deliberate exception. Its REST client retries internally
(MLFLOW_HTTP_REQUEST_MAX_RETRIES, default 7), so wrapping it here multiplied
out to 35 attempts per call and made a registry outage take minutes to
surface. MLflow's own knobs are set on the services in docker-compose.yml
instead, and agentic_blogger/prompts/registry.py calls it unwrapped.

Two adapters, because the call sites use two different retry mechanisms:

  - `tenacity_kwargs()` for plain function calls (the Blogger client).
  - `runnable_retry_kwargs()` for LangChain Runnables, whose `.with_retry()`
    accepts only an attempt count and a jitter flag. The backoff bounds are
    honoured on the tenacity path and silently unavailable on the LangChain
    one — that asymmetry is LangChain's, not ours.

Retry predicates stay with the call site. A uniform "retry everything"
policy would be wrong in at least one place that matters: the Blogger client
must not retry an expired OAuth grant, and its retry deliberately re-runs a
reconcile step to avoid double-posting.
"""

import logging

from tenacity import (
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)

from agentic_blogger.config.loader import resilience_spec

logger = logging.getLogger(__name__)


def _wait(cfg: dict):
    if cfg["jitter"]:
        return wait_exponential_jitter(
            initial=cfg["initial_backoff_s"], max=cfg["max_backoff_s"]
        )
    return wait_exponential(multiplier=cfg["initial_backoff_s"], max=cfg["max_backoff_s"])


def tenacity_kwargs(predicate=None, log: logging.Logger | None = None) -> dict:
    """kwargs for @retry(...) or Retrying(...).

    `predicate` takes an exception and returns True to retry. Omitting it
    retries every exception. `reraise=True` so the original error surfaces
    rather than tenacity's RetryError wrapper — callers upstream match on
    real exception types.
    """
    cfg = resilience_spec()
    kwargs = {
        "stop": stop_after_attempt(cfg["max_attempts"]),
        "wait": _wait(cfg),
        "reraise": True,
        "before_sleep": before_sleep_log(log or logger, logging.WARNING),
    }
    if predicate is not None:
        kwargs["retry"] = retry_if_exception(predicate)
    return kwargs


def runnable_retry_kwargs() -> dict:
    """kwargs for LangChain's Runnable.with_retry().

    Only max_attempts and jitter carry over — `.with_retry()` exposes no
    control over initial/max backoff.
    """
    cfg = resilience_spec()
    return {
        "stop_after_attempt": cfg["max_attempts"],
        "wait_exponential_jitter": cfg["jitter"],
    }
