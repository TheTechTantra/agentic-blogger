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


def role_spec(role: str) -> dict:
    roles = load_models()["roles"]
    if role not in roles:
        raise KeyError(f"Unknown role '{role}' — check config/models.yaml")
    return roles[role]


def fallback_specs(model_key: str) -> list[str]:
    return load_models().get("fallbacks", {}).get(model_key, [])
