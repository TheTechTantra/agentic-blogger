#!/usr/bin/env python3
"""
Register every model in config/models.yaml as an MLflow AI Gateway endpoint.

The gateway is the only path from this pipeline to an LLM provider, and this
script is what makes that path exist. It pushes provider credentials from
TinyDB into the gateway's encrypted store, creates one model definition and one
endpoint per model, and applies the spend budget.

Unlike scripts/register_prompts.py -- which deliberately creates a new prompt
version on every run -- this one is idempotent. Objects are matched by name and
updated in place, so re-running after an API-key rotation swaps the credential
without touching the endpoints built on it, and re-running after no change at
all is a no-op. That difference matters: prompts are content people edit in the
UI, gateway wiring is infrastructure that must converge on the config file.

Endpoint names are the model ids verbatim ("claude-opus-5"), with the single
exception of characters the gateway rejects in a name -- see llm/gateway.py.
That is not cosmetic. The Anthropic passthrough route reads the request's
`model` field as the *endpoint* name, so naming them this way lets the chat
model clients keep sending the model id they already send, and lets
`provider:model` keys and the per-node cost attribution in llm/callbacks.py keep
working with base_url as the only thing that changed.

Usage:
    python -m scripts.register_gateway            # converge the gateway
    python -m scripts.register_gateway --dry-run  # print what would change
"""

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

import httpx

from agentic_blogger.config.loader import load_models
from agentic_blogger.llm.gateway import endpoint_name
from agentic_blogger.llm.registry import _SECRET_KEY_BY_PROVIDER
from agentic_blogger.secrets.store import SecretStore

# One budget policy, global scope, so no combination of endpoints can spend
# more than this in a month no matter which model a role is pointed at. REJECT
# rather than ALERT: an unattended pipeline that keeps spending after an email
# nobody reads is the failure this is meant to prevent. Per-endpoint rate
# limits are the tool for runaway loops; this is the dollar backstop.
BUDGET_AMOUNT_USD = float(os.getenv("GATEWAY_BUDGET_USD", "50"))
BUDGET_DURATION_UNIT = "MONTHS"
BUDGET_DURATION_VALUE = 1
BUDGET_ACTION = "REJECT"

CREATED_BY = "register_gateway.py"


class Gateway:
    """Thin REST client for the gateway management API.

    Plain httpx against the documented 3.0 paths rather than MlflowClient: the
    request shapes and, more importantly, the error bodies stay visible. A
    failure here is a misconfigured credential store, and that is exactly when
    a wrapped exception costs the most.
    """

    def __init__(self, tracking_uri: str, timeout: float = 30.0):
        self.base = tracking_uri.rstrip("/") + "/api/3.0/mlflow/gateway"
        self.client = httpx.Client(timeout=timeout)

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, f"{self.base}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() or {}

    def get(self, path, params=None):
        return self._call("GET", path, params=params)

    def post(self, path, json):
        return self._call("POST", path, json=json)


# ---------------------------------------------------------------------------
# What to register
# ---------------------------------------------------------------------------

def _model_keys() -> list[str]:
    """Every 'provider:model' the pipeline can reach.

    Fallback targets are included on purpose. A fallback that exists in
    config/models.yaml but not in the gateway is worse than no fallback: it
    only fails when the primary is already failing.
    """
    cfg = load_models()
    keys = {spec["model"] for spec in cfg["roles"].values()}
    for primary, fbs in (cfg.get("fallbacks") or {}).items():
        keys.add(primary)
        keys.update(fbs)
    return sorted(keys)


def _split(model_key: str) -> tuple[str, str]:
    """'anthropic:claude-opus-5' -> ('anthropic', 'claude-opus-5').

    split(':', 1), never bare split(':') -- Ollama tags legitimately contain
    colons ('ollama:qwen3.5:9b').
    """
    return model_key.split(":", 1)


def _secret_name(provider: str) -> str:
    return f"agentic-blogger-{provider}"


def _provider_secret_payload(provider: str) -> tuple[dict, dict] | None:
    """(secret_value, auth_config) for a provider, or None to skip it.

    secret_value holds the encrypted fields ({"api_key": ...}); auth_config
    holds the non-secret ones ({"api_base": ...}). Both shapes come from the
    gateway's own /provider-config route.
    """
    if provider == "ollama":
        # Ollama has no credential, but the gateway marks api_key required for
        # every provider, so a placeholder goes in. api_base moves here from
        # config/models.yaml `providers.ollama.base_url`, because it is now the
        # gateway -- not this process -- that opens the connection to Ollama.
        base_url = (load_models()["providers"]["ollama"] or {}).get("base_url")
        if base_url:
            # The gateway's Ollama adapter is an OpenAI-compatible client, so
            # api_base must point at Ollama's /v1 surface, not its native root
            # (its own default is "http://localhost:11434/v1"). Without the
            # suffix the call lands on Ollama's Go router and comes back as a
            # bare "404 page not found" with nothing naming the cause.
            # config/models.yaml keeps the native root, because that is what
            # ChatOllama wants on the non-gateway path.
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
        return {"api_key": "unused"}, ({"api_base": base_url} if base_url else {})

    env_name = _SECRET_KEY_BY_PROVIDER.get(provider)
    if not env_name:
        logger.warning("No secret mapping for provider %r — skipping", provider)
        return None

    value = os.getenv(env_name) or SecretStore().get_optional(env_name)
    if not value:
        return None
    return {"api_key": value}, {}


# ---------------------------------------------------------------------------
# Convergence steps
# ---------------------------------------------------------------------------

def _sync_secrets(gw: Gateway, providers: set[str], dry_run: bool) -> dict[str, str]:
    """Push each provider credential into the gateway. Returns provider -> secret_id."""
    existing = {s["secret_name"]: s for s in gw.get("/secrets/list").get("secrets", [])}
    secret_ids: dict[str, str] = {}

    for provider in sorted(providers):
        name = _secret_name(provider)
        payload = _provider_secret_payload(provider)
        if payload is None:
            logger.warning("✗ %-10s no credential in TinyDB — endpoints for it will not be created",
                           provider)
            continue
        secret_value, auth_config = payload
        current = existing.get(name)

        if dry_run:
            logger.info("would %-6s secret %-28s provider=%s",
                        "update" if current else "create", name, provider)
            secret_ids[provider] = current["secret_id"] if current else f"<new:{name}>"
            continue

        if current:
            # Rotation path: the secret_id is stable, so every model definition
            # and endpoint already pointing at it keeps working untouched.
            gw.post("/secrets/update", {
                "secret_id": current["secret_id"],
                "secret_value": secret_value,
                "auth_config": auth_config,
                "last_updated_by": CREATED_BY,
            })
            secret_ids[provider] = current["secret_id"]
            logger.info("✓ secret %-28s updated  (provider=%s)", name, provider)
        else:
            resp = gw.post("/secrets/create", {
                "secret_name": name,
                "secret_value": secret_value,
                "provider": provider,
                "auth_config": auth_config,
                "created_by": CREATED_BY,
            })
            secret_ids[provider] = resp["secret"]["secret_id"]
            logger.info("✓ secret %-28s created  (provider=%s)", name, provider)

    return secret_ids


def _sync_model_definitions(gw: Gateway, model_keys: list[str],
                            secret_ids: dict[str, str], dry_run: bool) -> dict[str, str]:
    """One model definition per model. Returns model_key -> model_definition_id."""
    existing = {m["name"]: m for m in gw.get("/model-definitions/list").get("model_definitions", [])}
    ids: dict[str, str] = {}

    for key in model_keys:
        provider, model = _split(key)
        if provider not in secret_ids:
            continue
        # `name` is the gateway-side identifier and must obey its charset;
        # `model_name` is what gets sent upstream to the provider and must stay
        # the real tag. For every model but Ollama's these are the same string.
        name = endpoint_name(model)
        current = existing.get(name)

        if dry_run:
            logger.info("would %-6s model-definition %-22s provider=%s model_name=%s",
                        "update" if current else "create", name, provider, model)
            ids[key] = current["model_definition_id"] if current else f"<new:{name}>"
            continue

        if current:
            gw.post("/model-definitions/update", {
                "model_definition_id": current["model_definition_id"],
                "secret_id": secret_ids[provider],
                "model_name": model,
                "last_updated_by": CREATED_BY,
            })
            ids[key] = current["model_definition_id"]
            logger.info("✓ model-definition %-22s updated", name)
        else:
            resp = gw.post("/model-definitions/create", {
                "name": name,
                "secret_id": secret_ids[provider],
                "provider": provider,
                "model_name": model,
                "created_by": CREATED_BY,
            })
            ids[key] = resp["model_definition"]["model_definition_id"]
            logger.info("✓ model-definition %-22s created", name)

    return ids


def _sync_endpoints(gw: Gateway, model_keys: list[str],
                    md_ids: dict[str, str], experiment_id: str | None, dry_run: bool) -> None:
    """One endpoint per model, named exactly after the model id.

    Fallbacks are NOT declared here even though the gateway supports them.
    config/models.yaml `fallbacks:` stays the single place a fallback chain is
    written down, and llm/registry.with_resilience() stays the single place it
    is applied -- one endpoint per model keeps the gateway's usage table a
    clean per-model breakdown instead of attributing a fallback's tokens to the
    primary's endpoint.
    """
    existing = {e["name"]: e for e in gw.get("/endpoints/list").get("endpoints", [])}

    for key in model_keys:
        _, model = _split(key)
        name = endpoint_name(model)
        if key not in md_ids:
            logger.warning("✗ endpoint %-22s skipped — no model definition", name)
            continue
        current = existing.get(name)

        if dry_run:
            logger.info("would %-6s endpoint %-22s -> %s",
                        "update" if current else "create", name, key)
            continue

        if current:
            gw.post("/endpoints/update", {
                "endpoint_id": current["endpoint_id"],
                "usage_tracking": True,
                "last_updated_by": CREATED_BY,
            })
            logger.info("✓ endpoint %-22s updated", name)
        else:
            body = {
                "name": name,
                "model_configs": [{
                    "model_definition_id": md_ids[key],
                    "linkage_type": "PRIMARY",
                    "weight": 1.0,
                }],
                # Server-side token/cost/latency recording. This is what makes
                # the AI Gateway usage table populate; without it the endpoint
                # still serves traffic but reports nothing.
                "usage_tracking": True,
                "created_by": CREATED_BY,
            }
            if experiment_id:
                # Puts gateway spans in the same experiment as the job runs, so
                # a trace links to the job that caused it.
                body["experiment_id"] = experiment_id
            gw.post("/endpoints/create", body)
            logger.info("✓ endpoint %-22s created", name)


def _sync_budget(gw: Gateway, dry_run: bool) -> None:
    """A single global monthly USD budget with REJECT as the action.

    Global scope, not per-endpoint: the question worth answering is what the
    whole pipeline spends, and a per-endpoint budget would silently allow N
    times the intended total once a role is repointed at another model.
    """
    policies = gw.get("/budgets/list").get("budget_policies", [])
    current = next((p for p in policies if p.get("target_scope") == "GLOBAL"), None)

    desired = {
        "budget_unit": "USD",
        "budget_amount": BUDGET_AMOUNT_USD,
        "duration": {"unit": BUDGET_DURATION_UNIT, "value": BUDGET_DURATION_VALUE},
        "target_scope": "GLOBAL",
        "budget_action": BUDGET_ACTION,
    }

    if dry_run:
        logger.info("would %-6s budget  $%s / %d %s  action=%s (GLOBAL)",
                    "update" if current else "create", BUDGET_AMOUNT_USD,
                    BUDGET_DURATION_VALUE, BUDGET_DURATION_UNIT, BUDGET_ACTION)
        return

    if current:
        gw.post("/budgets/update", {
            "budget_policy_id": current["budget_policy_id"],
            **desired,
            "last_updated_by": CREATED_BY,
        })
        logger.info("✓ budget   $%s / %d %s  action=%s  updated",
                    BUDGET_AMOUNT_USD, BUDGET_DURATION_VALUE, BUDGET_DURATION_UNIT, BUDGET_ACTION)
    else:
        gw.post("/budgets/create", {**desired, "created_by": CREATED_BY})
        logger.info("✓ budget   $%s / %d %s  action=%s  created",
                    BUDGET_AMOUNT_USD, BUDGET_DURATION_VALUE, BUDGET_DURATION_UNIT, BUDGET_ACTION)


def _experiment_id() -> str | None:
    """Resolve the pipeline's experiment so gateway traces land beside job runs."""
    try:
        import mlflow

        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000"))
        exp = mlflow.get_experiment_by_name("agentic-blogger/blog")
        return exp.experiment_id if exp else None
    except Exception:
        logger.warning("Could not resolve experiment 'agentic-blogger/blog' — "
                       "endpoints will be created without one", exc_info=True)
        return None


def main(dry_run: bool) -> int:
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    gw = Gateway(tracking_uri)

    model_keys = _model_keys()
    providers = {_split(k)[0] for k in model_keys}
    logger.info("Gateway %s", gw.base)
    logger.info("Models  %s", ", ".join(model_keys))
    logger.info("")

    try:
        secret_ids = _sync_secrets(gw, providers, dry_run)
        if not secret_ids:
            logger.error("No provider credentials available — nothing to register. "
                         "Seed them with ./scripts/seed_secrets.sh")
            return 1
        md_ids = _sync_model_definitions(gw, model_keys, secret_ids, dry_run)
        _sync_endpoints(gw, model_keys, md_ids, _experiment_id(), dry_run)
        _sync_budget(gw, dry_run)
    except RuntimeError as e:
        logger.error("Gateway API call failed: %s", e)
        return 1

    logger.info("")
    if dry_run:
        logger.info("Dry run — nothing was changed.")
    else:
        logger.info("Gateway converged. Every LLM call now routes through %s.", tracking_uri)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(main(ap.parse_args().dry_run))
