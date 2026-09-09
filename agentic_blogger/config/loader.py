"""Load config/models.yaml and config/pricing.yaml."""

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


@lru_cache(maxsize=1)
def load_pricing() -> dict:
    with open(CONFIG_DIR / "pricing.yaml") as f:
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


def role_spec(role: str) -> dict:
    roles = load_models()["roles"]
    if role not in roles:
        raise KeyError(f"Unknown role '{role}' — check config/models.yaml")
    return roles[role]


def fallback_specs(model_key: str) -> list[str]:
    return load_models().get("fallbacks", {}).get(model_key, [])
