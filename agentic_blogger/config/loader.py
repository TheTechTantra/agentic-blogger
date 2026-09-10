"""Load config/models.yaml."""

import os
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/app/config"))
if not CONFIG_DIR.exists():
    # host / test fallback
    CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@lru_cache(maxsize=1)
def load_models() -> dict:
    with open(CONFIG_DIR / "models.yaml") as f:
        return yaml.safe_load(f)


def resilience_spec() -> dict:
    """Retry/backoff numbers shared by every outbound service call.

    Defaults match config/models.yaml so a missing block degrades to the
    documented policy rather than to whatever each call site used to hardcode.
    """
    cfg = load_models().get("resilience") or {}
    return {
        "max_attempts": cfg.get("max_attempts", 5),
        "initial_backoff_s": cfg.get("initial_backoff_s", 1),
        "max_backoff_s": cfg.get("max_backoff_s", 30),
        "jitter": cfg.get("jitter", True),
    }


def prompts_spec() -> dict:
    """Prompt registry settings. `alias` is resolved per node at call time."""
    cfg = load_models().get("prompts") or {}
    return {
        "alias": cfg.get("alias", "production"),
        "cache_ttl_seconds": cfg.get("cache_ttl_seconds", 300),
    }


def gateway_spec() -> dict:
    """MLflow AI Gateway routing settings.

    `base_url` is left empty in config/models.yaml on purpose: the MLflow host
    is declared once, in docker-compose.yml, and derived from there. A literal
    value in the config file wins if one is set.

    Defaults here are the disabled ones. A models.yaml with no `gateway:` block
    at all therefore behaves exactly as it did before the gateway existed,
    which is what makes this safe to load from a fallback-construction path.
    """
    cfg = load_models().get("gateway") or {}
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        tracking = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000").rstrip("/")
        base_url = f"{tracking}/gateway"
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "base_url": base_url,
        "surfaces": cfg.get("surfaces") or {},
    }


def timeout_s() -> int:
    """Client-side HTTP timeout for LLM calls (config/models.yaml `defaults:`)."""
    return (load_models().get("defaults") or {}).get("timeout_s", 600)


def role_spec(role: str) -> dict:
    roles = load_models()["roles"]
    if role not in roles:
        raise KeyError(f"Unknown role '{role}' — check config/models.yaml")
    return roles[role]


def fallback_specs(model_key: str) -> list[str]:
    return load_models().get("fallbacks", {}).get(model_key, [])
