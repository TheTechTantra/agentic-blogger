"""USD cost estimation from usage_metadata. Never presented as billing truth."""

from decimal import Decimal
from typing import Optional

from agentic_blogger.config.loader import load_pricing


def _rates(provider: str, model: str) -> Optional[dict]:
    table = load_pricing()
    return table.get(provider, {}).get(model)


def cost_from_usage(provider: str, model: str, usage_metadata: dict) -> Decimal:
    """Compute an estimated USD cost from a LangChain usage_metadata dict.

    A missing cache-write count is treated as unknown (0 cost contribution),
    never silently assumed to be zero usage — see plan §2 note on cache
    accounting varying by provider.
    """
    rates = _rates(provider, model)
    if not rates:
        return Decimal("0")

    input_tokens = Decimal(usage_metadata.get("input_tokens", 0) or 0)
    output_tokens = Decimal(usage_metadata.get("output_tokens", 0) or 0)
    details = usage_metadata.get("input_token_details", {}) or {}
    cache_read = Decimal(details.get("cache_read", 0) or 0)
    cache_write = Decimal(details.get("cache_creation", 0) or 0)

    # input_tokens from Anthropic already excludes cache read/write tokens
    # in langchain's normalized usage_metadata, so we price each bucket
    # independently rather than subtracting.
    cost = (
        (input_tokens / Decimal(1_000_000)) * Decimal(str(rates["input_per_1m"]))
        + (output_tokens / Decimal(1_000_000)) * Decimal(str(rates["output_per_1m"]))
        + (cache_read / Decimal(1_000_000)) * Decimal(str(rates.get("cache_read_per_1m", 0)))
        + (cache_write / Decimal(1_000_000)) * Decimal(str(rates.get("cache_write_per_1m", 0)))
    )
    return cost
