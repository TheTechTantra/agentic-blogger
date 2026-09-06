"""Cost tracking — applied per node call after invoke(), not as a global
LangChain callback. `.with_fallbacks()`/`.with_retry()` wrap the Runnable
opaquely enough that a global handler can't reliably attribute usage back
to (job_id, node_name); reading `response.usage_metadata` directly after
each call is simpler and exactly as accurate.
"""

import logging
import time
from contextlib import contextmanager
from decimal import Decimal

from agentic_blogger.db import repo
from agentic_blogger.llm.pricing import cost_from_usage

logger = logging.getLogger(__name__)


@contextmanager
def track_llm_call(job_id: str, node_name: str, role: str, model_key: str):
    """Wrap an LLM invocation; records node_runs + rolls up jobs.cost_usd.

    Usage:
        with track_llm_call(job_id, "research", "research_synth", spec_model) as t:
            response = llm.invoke([...])
            t(response)
    """
    provider, model = model_key.split(":", 1)
    started = time.monotonic()
    started_ts = None
    result_holder: dict = {}

    def _record(response):
        result_holder["response"] = response

    try:
        yield _record
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        repo.record_node_run(
            job_id, node_name, provider=provider, model=model, role=role,
            latency_ms=latency_ms, error_class=type(e).__name__, error_message=str(e),
        )
        raise
    else:
        latency_ms = int((time.monotonic() - started) * 1000)
        response = result_holder.get("response")
        usage = getattr(response, "usage_metadata", None) or {}
        cost = cost_from_usage(provider, model, usage) if usage else Decimal("0")
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
        repo.record_node_run(
            job_id, node_name, provider=provider, model=model, role=role,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, latency_ms=latency_ms,
        )
        if cost:
            repo.add_job_cost(job_id, cost, tokens_in, tokens_out)
        logger.info(
            "node=%s role=%s tokens_in=%s tokens_out=%s cost_usd=%s latency_ms=%s",
            node_name, role, tokens_in, tokens_out, cost, latency_ms,
        )
