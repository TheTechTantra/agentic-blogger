"""Per-call USD cost, read back from the MLflow AI Gateway.

Nothing in this repo prices a token. The gateway is the only component that
computes cost, and this module reads its number — it does not recompute,
cross-check, or fall back to a local table. There is one place spend is
decided, and it is the same place the budget is enforced, so `node_runs.cost_usd`
and the gateway's own usage and budget figures cannot disagree.

How the number gets here:

  1. The gateway prices every call server-side and writes `total_cost` into
     MLflow's `span_metrics` table, against a span named
     `provider/<provider>/<model>`.
  2. `MLFLOW_ENABLE_ASYNC_TRACE_LOGGING=false` on the mlflow service makes that
     write happen before the gateway answers the caller, so the row exists by
     the time we look. With the default async exporter it lands up to
     `MLFLOW_ASYNC_TRACE_LOGGING_MAX_INTERVAL_MILLIS` (5s) later — measured at
     4.7s, which is why it is switched off there.
  3. This module sums that metric over the wall-clock window of one call, via
     `POST /api/3.0/mlflow/traces/metrics`.

The span-name filter is what keeps the number honest. MLflow's server prices
*every* trace that carries a model and token usage, so the client-side
`mlflow.langchain.autolog()` spans (named `ChatAnthropic`) carry a cost of
their own for the very same call. Summing without the filter double-counts.
Only `provider/...` spans are the gateway's.

Attribution is by time window, not by request id — the gateway does not return
a trace id or a cost header, and its trace records the provider's message id
too deep to filter on. That is exact here because the orchestrator is a
single-replica worker running one node at a time, so nothing else is calling
the gateway during the window. It would stop being exact under concurrent
workers; see docs/AI_GATEWAY.md before adding one.

Retries and fallbacks are handled by asking for every model the call could
have used, not just the primary. A haiku call that fell back to Ollama spent
money under both names, and both belong on the node's ledger row.
"""

import logging
import os
import time
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_EXPERIMENT_NAME = "agentic-blogger/blog"

# How long to keep asking after a call returns. Synchronous trace export puts
# the row in place within ~20ms, so this is a guard rather than a wait we
# expect to spend.
#
# It is deliberately short. A model the gateway cannot price — Ollama, which is
# in no price catalog — produces a span with no cost row at all, and that is
# indistinguishable from a row that has not been written yet. So every locally
# served call pays this timeout in full. One second is long enough to cover a
# slow write and short enough not to matter on a fallback path.
_POLL_TIMEOUT_S = 1.0
_POLL_INTERVAL_S = 0.2

# Widen the window slightly at both ends: the gateway timestamps the trace when
# it starts handling the request, which is marginally before our clock reading.
_WINDOW_MARGIN_MS = 2000

_experiment_id: Optional[str] = None
_warned: set[str] = set()


def _tracking_uri() -> str:
    return os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000").rstrip("/")


def _get_experiment_id() -> Optional[str]:
    global _experiment_id
    if _experiment_id is not None:
        return _experiment_id
    try:
        r = httpx.get(f"{_tracking_uri()}/api/2.0/mlflow/experiments/get-by-name",
                      params={"experiment_name": _EXPERIMENT_NAME}, timeout=10.0)
        r.raise_for_status()
        _experiment_id = r.json()["experiment"]["experiment_id"]
    except Exception:
        logger.warning("Could not resolve MLflow experiment %r — gateway cost "
                       "cannot be read", _EXPERIMENT_NAME, exc_info=True)
        return None
    return _experiment_id


def _span_name(model_key: str) -> str:
    """The gateway names its provider span `provider/<provider>/<model>`.

    Built from the config key, so it uses the real model tag ('qwen3.5:9b'),
    not the sanitized endpoint name — the span records what was sent upstream.
    """
    provider, model = model_key.split(":", 1)
    return f"provider/{provider}/{model}"


def _query(experiment_id: str, span_name: str, start_ms: int, end_ms: int) -> Optional[Decimal]:
    body = {
        "experiment_ids": [experiment_id],
        "view_type": "SPANS",
        "metric_name": "total_cost",
        "aggregations": [{"aggregation_type": "SUM"}],
        # Only '=' is supported here, which is why one query per model is
        # needed rather than a prefix match on 'provider/'.
        "filters": [f"span.name = '{span_name}'"],
        "start_time_ms": start_ms,
        "end_time_ms": end_ms,
    }
    r = httpx.post(f"{_tracking_uri()}/api/3.0/mlflow/traces/metrics", json=body, timeout=15.0)
    r.raise_for_status()
    points = r.json().get("data_points") or []
    if not points:
        return None
    total = sum(Decimal(str(p["values"].get("SUM", 0) or 0)) for p in points)
    return total or None


def cost_for_call(model_keys: list[str], started_ms: int, ended_ms: int) -> Decimal:
    """Gateway-recorded USD spend across `model_keys` during the window.

    Returns Decimal("0") rather than raising on any failure. A cost read must
    never fail a call that already succeeded: the money is spent either way,
    and losing the figure is strictly better than losing the work.

    Zero is a legitimate answer, not only a failure signal — Ollama is not in
    any price catalog, so a call served locally genuinely records nothing.
    """
    experiment_id = _get_experiment_id()
    if not experiment_id:
        return Decimal("0")

    start = started_ms - _WINDOW_MARGIN_MS
    end = ended_ms + _WINDOW_MARGIN_MS
    span_names = [_span_name(k) for k in model_keys]

    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while True:
        total = Decimal("0")
        try:
            for name in span_names:
                if (value := _query(experiment_id, name, start, end)) is not None:
                    total += value
        except Exception:
            logger.warning("Gateway cost query failed for %s — recording $0 for this call",
                           ",".join(model_keys), exc_info=True)
            return Decimal("0")

        if total:
            return total
        if time.monotonic() >= deadline:
            # Logged once per model set: a research job makes enough calls to
            # bury everything else in the log otherwise.
            key = ",".join(model_keys)
            if key not in _warned:
                _warned.add(key)
                logger.info(
                    "Gateway reported no cost for %s. Expected for locally served "
                    "models; otherwise check that the model is priced by the "
                    "gateway and that MLFLOW_ENABLE_ASYNC_TRACE_LOGGING is false.", key)
            return Decimal("0")
        time.sleep(_POLL_INTERVAL_S)
