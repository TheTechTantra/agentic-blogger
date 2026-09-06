"""Build LangChain-compatible LLM Runnables from role config.

Model construction is provider-pluggable per decision 1: swapping a role's
provider is a one-line change in config/models.yaml, never a code change.
"""

import logging
import os

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable

from agentic_blogger.config.loader import fallback_specs, load_models, role_spec
from agentic_blogger.secrets.store import SecretStore

logger = logging.getLogger(__name__)

_SECRET_KEY_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_ensured_keys: set[str] = set()


def _ensure_api_key(provider: str) -> None:
    """Fetch the provider's API key from TinyDB and set it as an env var —
    the underlying LangChain chat model classes read it from there."""
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
    logger.info("effort_requested=%s effort_effective=%s provider=%s", effort, bool(kwargs), provider)
    return kwargs


def _construct(model: str, provider: str, max_tokens: int, extra: dict) -> Runnable:
    provider_init_kwargs = dict(extra)
    if provider == "ollama":
        base_url = load_models()["providers"]["ollama"].get("base_url")
        if base_url:
            provider_init_kwargs["base_url"] = base_url

    try:
        return init_chat_model(
            model, model_provider=provider, max_tokens=max_tokens, **provider_init_kwargs
        )
    except TypeError as e:
        if extra:
            logger.warning("Provider rejected kwargs %s (%s) — retrying without them", extra, e)
            return init_chat_model(model, model_provider=provider, max_tokens=max_tokens)
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
    return _build_raw(spec["model"], spec.get("max_tokens", 4096), spec.get("effort", "low"))


def with_resilience(llm: Runnable, role: str) -> Runnable:
    """Apply retry + fallbacks. Call this LAST, after any bind_tools()/
    with_structured_output() on the object returned by build_llm()."""
    spec = role_spec(role)
    max_tokens = spec.get("max_tokens", 4096)
    effort = spec.get("effort", "low")

    defaults = load_models().get("defaults", {})
    llm = llm.with_retry(
        stop_after_attempt=defaults.get("max_retries", 3),
        wait_exponential_jitter=True,
    )

    fallback_llms = []
    for fb in fallback_specs(spec["model"]):
        try:
            fallback_llms.append(_build_raw(fb, max_tokens, effort))
        except Exception:
            # A fallback that can't even be constructed (missing package,
            # unset key, unreachable host) must never take down the primary
            # path — log and skip it instead.
            logger.warning("Skipping unavailable fallback %r for role", fb, exc_info=True)
    if fallback_llms:
        llm = llm.with_fallbacks(fallback_llms)

    return llm
