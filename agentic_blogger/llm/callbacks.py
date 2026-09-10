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

from agentic_blogger.config.loader import fallback_specs
from agentic_blogger.db import repo
from agentic_blogger.llm.cost import cost_for_call
from agentic_blogger.prompts import drain

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
    # Wall clock, separately from the monotonic latency clock: the gateway
    # timestamps its traces in epoch milliseconds and cost is attributed by
    # matching that window (see llm/cost.py).
    started_ms = int(time.time() * 1000)
    result_holder: dict = {}

    def _record(response):
        result_holder["response"] = response

    # Printed before the call, not after: a 12-minute research call is
    # otherwise a 12-minute gap in the log with nothing saying what is running.
    logger.info("llm call start node=%s role=%s model=%s", node_name, role, model_key)

    try:
        yield _record
    except Exception as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.error("llm call FAILED node=%s role=%s model=%s after %dms: %s: %s",
                     node_name, role, model_key, latency_ms, type(e).__name__, e)
        repo.record_node_run(
            job_id, node_name, provider=provider, model=model, role=role,
            latency_ms=latency_ms, prompts=drain(),
            error_class=type(e).__name__, error_message=str(e),
        )
        raise
    else:
        latency_ms = int((time.monotonic() - started) * 1000)
        ended_ms = int(time.time() * 1000)
        response = result_holder.get("response")
        usage = getattr(response, "usage_metadata", None) or {}
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
        # Cost comes from the gateway, never from a local rate table. Every
        # model this call could have reached is asked for, not just the
        # primary: a retry or a fallback to another model spent real money and
        # belongs on this node's row. Nothing is recorded if the response
        # carried no usage — that means no provider call was billed.
        cost = (
            cost_for_call([model_key, *fallback_specs(model_key)], started_ms, ended_ms)
            if usage else Decimal("0")
        )
        # drain() attributes every prompt resolved since the last node_run to
        # this one — nodes render before invoking, so this is the whole set the
        # call actually used.
        prompts = drain()
        repo.record_node_run(
            job_id, node_name, provider=provider, model=model, role=role,
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, latency_ms=latency_ms,
            prompts=prompts,
        )
        if cost:
            repo.add_job_cost(job_id, cost, tokens_in, tokens_out)
        logger.info(
            "node=%s role=%s tokens_in=%s tokens_out=%s cost_usd=%s latency_ms=%s prompts=%s",
            node_name, role, tokens_in, tokens_out, cost, latency_ms,
            ",".join(f"{p['name']}@v{p['version']}" for p in prompts) or "-",
        )
