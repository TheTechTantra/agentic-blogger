"""Build LangChain-compatible LLM Runnables from role config.

Model construction is provider-pluggable per decision 1: swapping a role's
provider is a one-line change in config/models.yaml, never a code change.

Every model built here is addressed to the MLflow AI Gateway, never to a
provider directly (config/models.yaml `gateway:`). The consequence worth
knowing when reading the rest of this file: this process holds no provider API
key at all. The gateway holds them, encrypted, and the dummy key handed to each
client exists only because the client libraries refuse to construct without
one. If the gateway is unreachable, jobs fail — that is the contract, and the
reason is that a bypass would mean spend no budget sees and calls no usage
table records.
"""

import logging
import os

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable

from agentic_blogger.config.loader import (
    fallback_specs,
    gateway_spec,
    load_models,
    role_spec,
    timeout_s,
)
from agentic_blogger.llm.gateway import endpoint_name
from agentic_blogger.resilience import runnable_retry_kwargs
from agentic_blogger.secrets.store import SecretStore

logger = logging.getLogger(__name__)

_SECRET_KEY_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_ensured_keys: set[str] = set()


# Handed to every client as its API key. The real credential lives in the
# gateway's encrypted store and is attached server-side; the clients only need
# a non-empty string to pass their own constructor validation.
_GATEWAY_PLACEHOLDER_KEY = "mlflow-gateway"


def _ensure_api_key(provider: str) -> None:
    """Fetch the provider's API key from TinyDB and set it as an env var —
    the underlying LangChain chat model classes read it from there.

    A no-op when the gateway is enabled. Not merely unnecessary then, but
    unwanted: putting a provider key in this process's environment is exactly
    the thing routing through the gateway is meant to stop, and a key sitting
    in os.environ is one accidental direct base_url away from being used.
    _SECRET_KEY_BY_PROVIDER is still the provider->secret-name mapping that
    scripts/register_gateway.py reads.
    """
    if gateway_spec()["enabled"]:
        return
    secret_name = _SECRET_KEY_BY_PROVIDER.get(provider)
    if not secret_name or secret_name in _ensured_keys:
        return
    if os.getenv(secret_name):
        _ensured_keys.add(secret_name)
        return
    value = SecretStore().get_optional(secret_name)
    if value:
        os.environ[secret_name] = value
        _ensured_keys.add(secret_name)
    else:
        logger.warning("Secret %s not found in TinyDB (provider=%s)", secret_name, provider)


def _provider_kwargs(provider: str, spec: dict) -> dict:
    """Effort/thinking is the one thing that never normalized across
    providers (plan §2, point 1). Logged as requested vs effective so the
    mapping stays auditable."""
    effort = spec.get("effort", "low")
    kwargs: dict = {}
    if provider == "anthropic" and effort == "high":
        # Adaptive thinking on Claude 5-family models. NEVER pass
        # budget_tokens — it 400s on Opus 5 (see claude-api skill drift note).
        kwargs["thinking"] = {"type": "adaptive"}
    logger.debug("effort_requested=%s effort_effective=%s provider=%s",
                 effort, bool(kwargs), provider)
    return kwargs


def _gateway_route(provider: str, model: str) -> tuple[str, dict]:
    """Where a (provider, model) pair is addressed, and with what kwargs.

    Returns (client_provider, kwargs). `client_provider` is which LangChain
    integration constructs the client, which is NOT always the config provider:
    everything on the unified surface is spoken to by the OpenAI client
    regardless of who actually serves the model.

    Two surfaces, chosen per provider in config/models.yaml:

    - `passthrough` -> {base}/{provider}. The provider's native request body is
      forwarded to it untouched. Anthropic needs this: the web_search/web_fetch
      server tools, adaptive-thinking blocks, and the prompt-cache token buckets
      the gateway prices separately only exist in that body shape, and a
      translation layer would quietly drop all three.
    - `openai` -> {base}/mlflow/v1. One OpenAI-compatible surface for everyone
      else, addressed by endpoint name.

    In both cases the request's `model` field is read by the gateway as the
    *endpoint* name, which is why endpoints are registered under the bare model
    id — the client keeps sending what it always sent.
    """
    gw = gateway_spec()
    base = gw["base_url"]
    surface = (gw["surfaces"] or {}).get(provider, "openai")

    if surface == "passthrough":
        return provider, {"base_url": f"{base}/{provider}"}
    return "openai", {"base_url": f"{base}/mlflow/v1"}


def _construct(model: str, provider: str, max_tokens: int, extra: dict) -> Runnable:
    provider_init_kwargs = dict(extra)
    # Client-side ceiling on a call that now crosses an extra hop. Unset, a
    # gateway that accepts the connection and then stalls hangs the worker for
    # as long as the process lives; the pipeline's longest legitimate call
    # (draft, factcheck) is minutes, not unbounded.
    provider_init_kwargs.setdefault("timeout", timeout_s())
    # Retry is with_resilience()'s job, and only its job. The provider SDKs
    # retry internally by default (the Anthropic client, 2 by default), and
    # that count MULTIPLIES with the `resilience.max_attempts` retry wrapped
    # around it -- 5 x 3 = 15 upstream calls for one node. On a gateway
    # timeout each of those is a full provider generation that is billed and
    # then abandoned, and the gateway records no usage row for a call it
    # never got a response from, so the spend is invisible to the budget too.
    # Same stacking problem, same fix, as MLFLOW_HTTP_REQUEST_MAX_RETRIES on
    # the prompt path in docker-compose.yml.
    provider_init_kwargs.setdefault("max_retries", 0)

    client_provider = provider
    wire_model = model
    if gateway_spec()["enabled"]:
        client_provider, gw_kwargs = _gateway_route(provider, model)
        provider_init_kwargs.update(gw_kwargs)
        # The gateway resolves the request's `model` field to an endpoint, so
        # what goes on the wire is the endpoint name, not necessarily the tag.
        # Identical for every model except Ollama's — see llm/gateway.py.
        wire_model = endpoint_name(model)
        # The provider's real key never enters this process; see the note on
        # _GATEWAY_PLACEHOLDER_KEY.
        provider_init_kwargs["api_key"] = _GATEWAY_PLACEHOLDER_KEY
        logger.debug("gateway route model=%s endpoint=%s provider=%s client=%s base_url=%s",
                     model, wire_model, provider, client_provider, gw_kwargs["base_url"])
    elif provider == "ollama":
        base_url = load_models()["providers"]["ollama"].get("base_url")
        if base_url:
            provider_init_kwargs["base_url"] = base_url

    try:
        return init_chat_model(
            wire_model, model_provider=client_provider, max_tokens=max_tokens,
            **provider_init_kwargs
        )
    except TypeError as e:
        if extra:
            logger.warning("Provider rejected kwargs %s (%s) — retrying without them", extra, e)
            provider_init_kwargs = {k: v for k, v in provider_init_kwargs.items()
                                    if k not in extra}
            return init_chat_model(
                wire_model, model_provider=client_provider, max_tokens=max_tokens,
                **provider_init_kwargs
            )
        raise


def _build_raw(model_key: str, max_tokens: int, effort: str) -> Runnable:
    """Build an LLM from a bare 'provider:model' string (used for fallbacks,
    which aren't roles). str.split(':', 1) — never bare split(':') — Ollama
    tags legitimately contain colons (e.g. 'ollama:qwen3.5:9b')."""
    provider, model = model_key.split(":", 1)
    _ensure_api_key(provider)
    kwargs = _provider_kwargs(provider, {"effort": effort})
    return _construct(model, provider, max_tokens, kwargs)


def build_llm(role: str) -> Runnable:
    """Build the raw model Runnable for a config role — NOT wrapped in
    retry/fallbacks yet. Call .bind_tools()/.with_structured_output() on
    this first if the node needs it, then pass the result to
    with_resilience(). Wrapping order matters: RunnableRetry and
    RunnableWithFallbacks don't forward bind_tools()/with_structured_output()
    — those only exist on the underlying chat model."""
    spec = role_spec(role)
    # Which model actually served a node is the first question asked of any
    # bad output, and role->model is config, so it cannot be read off the code.
    logger.info("build_llm role=%s model=%s max_tokens=%s effort=%s",
                role, spec["model"], spec.get("max_tokens", 4096), spec.get("effort", "low"))
    return _build_raw(spec["model"], spec.get("max_tokens", 4096), spec.get("effort", "low"))


def with_resilience(llm: Runnable, role: str) -> Runnable:
    """Apply retry + fallbacks. Call this LAST, after any bind_tools()/
    with_structured_output() on the object returned by build_llm()."""
    spec = role_spec(role)
    max_tokens = spec.get("max_tokens", 4096)
    effort = spec.get("effort", "low")

    # Shared policy from config/models.yaml `resilience:`. LangChain's
    # .with_retry() exposes only an attempt count and a jitter flag, so the
    # backoff bounds in that block apply on the tenacity call sites and not
    # here — see agentic_blogger/resilience.py.
    llm = llm.with_retry(**runnable_retry_kwargs())

    fallback_llms = []
    for fb in fallback_specs(spec["model"]):
        try:
            fallback_llms.append(_build_raw(fb, max_tokens, effort))
        except Exception:
            # A fallback that can't even be constructed (missing package,
            # unset key, unreachable host) must never take down the primary
            # path — log and skip it instead.
            logger.warning("Skipping unavailable fallback %r for role=%s", fb, role, exc_info=True)
    if fallback_llms:
        llm = llm.with_fallbacks(fallback_llms)
    logger.debug("resilience role=%s fallbacks=%d", role, len(fallback_llms))

    return llm
