#!/usr/bin/env python3
"""
Smoke test: LLM initialization and token usage tracking via init_chat_model.

Tests that:
  - init_chat_model("anthropic:claude-haiku-4-5") succeeds
  - Model returns structured usage_metadata with token counts
  - Cost calculation works
"""

import os
import sys
import logging
from decimal import Decimal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Fetch ANTHROPIC_API_KEY from TinyDB (never from .env — see decision 7 in the plan)
try:
    from agentic_blogger.secrets.store import SecretStore
except ImportError as e:
    logger.error(f"Failed to import SecretStore: {e}")
    sys.exit(1)

try:
    api_key = SecretStore().get("ANTHROPIC_API_KEY")
except (KeyError, Exception) as e:
    logger.error(f"Failed to fetch ANTHROPIC_API_KEY from TinyDB: {e}")
    sys.exit(1)

# langchain-anthropic reads this env var internally
os.environ["ANTHROPIC_API_KEY"] = api_key

try:
    from langchain.chat_models import init_chat_model
    from langchain_core.messages import HumanMessage
except ImportError as e:
    logger.error(f"LangChain import failed: {e}")
    sys.exit(1)


# Pricing table (from config/pricing.yaml)
PRICING = {
    "anthropic": {
        "claude-haiku-4-5": {
            "input_per_1m": 1.00,
            "output_per_1m": 5.00,
        }
    }
}


def calculate_cost(usage_metadata: dict) -> Decimal:
    """Calculate USD cost from token usage and pricing."""
    input_tokens = usage_metadata.get("input_tokens", 0)
    output_tokens = usage_metadata.get("output_tokens", 0)

    # Assume Anthropic + Haiku pricing
    input_cost = Decimal(input_tokens) / Decimal(1_000_000) * Decimal("1.00")
    output_cost = Decimal(output_tokens) / Decimal(1_000_000) * Decimal("5.00")

    return input_cost + output_cost


def test_llm_init():
    """Test LLM initialization and token tracking."""
    logger.info("=== Smoke Test: LLM Initialization ===")

    # Initialize via init_chat_model (no explicit provider/model split)
    logger.info("Initializing claude-haiku-4-5 via init_chat_model...")
    try:
        llm = init_chat_model(
            "claude-haiku-4-5",
            model_provider="anthropic",
            max_tokens=1024,
        )
        logger.info("✓ init_chat_model succeeded")
    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        sys.exit(1)

    # Make a simple call
    query = "What is the capital of France?"
    logger.info(f"Invoking with query: {query}")

    try:
        response = llm.invoke([HumanMessage(content=query)])
    except Exception as e:
        logger.error(f"Invocation failed: {e}")
        sys.exit(1)

    logger.info(f"✓ Invocation succeeded")
    logger.info(f"Response: {response.content[:100]}")

    # Check usage_metadata
    if not hasattr(response, "usage_metadata"):
        logger.error("Response has no usage_metadata attribute")
        sys.exit(1)

    usage = response.usage_metadata
    logger.info(f"Usage metadata: {usage}")

    # Extract token counts
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    if not input_tokens or not output_tokens:
        logger.error(f"Invalid token counts: input={input_tokens}, output={output_tokens}")
        sys.exit(1)

    logger.info(f"Input tokens: {input_tokens}")
    logger.info(f"Output tokens: {output_tokens}")

    # Calculate cost
    cost_usd = calculate_cost(usage)
    logger.info(f"Estimated cost: ${cost_usd:.6f}")

    if cost_usd <= 0:
        logger.error(f"Cost calculation failed: {cost_usd}")
        sys.exit(1)

    logger.info("✓ Cost calculation succeeded")
    logger.info(f"\n=== SMOKE TEST PASSED ===")
    logger.info(f"Input: {input_tokens} tokens")
    logger.info(f"Output: {output_tokens} tokens")
    logger.info(f"Cost: ${cost_usd:.6f}")

    return True


if __name__ == "__main__":
    try:
        success = test_llm_init()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
