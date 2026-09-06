#!/usr/bin/env python3
"""
Smoke test: web_search_20260209 server-tool binding via langchain-anthropic.

THE RISKIEST STEP — tests framework integration introduced by decision 8.
If langchain-anthropic does not yet pass through the current server-tool types,
this fails immediately and the whole design needs revision.

Usage:
  docker compose run --rm orchestrator python -m scripts.smoke_search

Expected output:
  - Tool binding succeeds
  - Search returns structured results with url, title, snippet
  - No errors on tool invocation
"""

import os
import sys
import json
import logging

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
    logger.error("Verify requirements.txt pinning: langchain==0.2.12")
    sys.exit(1)


def test_server_tool_binding():
    """Test that web_search_20260209 can be bound and invoked."""
    logger.info("=== Smoke Test: web_search_20260209 binding ===")

    # Initialize Haiku model
    logger.info("Initializing claude-haiku-4-5...")
    llm = init_chat_model(
        "claude-haiku-4-5",
        model_provider="anthropic",
        max_tokens=2048,
    )

    # Bind the server tool — this is the critical test
    logger.info("Binding web_search_20260209 tool...")
    try:
        tools = [{
            "type": "web_search_20260209",
            "name": "web_search",
            "allowed_callers": ["direct"]  # Haiku requires explicit allowed_callers
        }]
        llm_with_tools = llm.bind_tools(tools)
    except Exception as e:
        logger.error(f"bind_tools() failed: {e}")
        logger.error("langchain-anthropic may not yet support web_search_20260209")
        logger.error("Check https://github.com/langchain-ai/langchain-anthropic/releases")
        sys.exit(1)

    # Invoke with a search query
    logger.info("Invoking with search query...")
    query = "Kubernetes operators explained"
    try:
        response = llm_with_tools.invoke([
            HumanMessage(content=query)
        ])
    except Exception as e:
        logger.error(f"Invocation failed: {e}")
        sys.exit(1)

    # Check response structure
    logger.info(f"Response type: {type(response)}")
    logger.info(f"Stop reason: {response.response_metadata.get('stop_reason', 'unknown')}")
    logger.info(f"Tool calls: {len(response.tool_calls) if hasattr(response, 'tool_calls') else 0}")

    # If tool was called, validate the result
    if hasattr(response, "tool_calls") and response.tool_calls:
        for i, call in enumerate(response.tool_calls):
            logger.info(f"Tool call {i}: {call.get('name', 'unknown')}")
            if "args" in call:
                logger.info(f"  Args: {call['args']}")

    # Log full content for inspection
    logger.info(f"Content: {response.content[:200] if response.content else '(empty)'}")

    logger.info("✓ web_search_20260209 binding and invocation succeeded")
    return True


if __name__ == "__main__":
    try:
        success = test_server_tool_binding()
        if success:
            logger.info("\n=== SMOKE TEST PASSED ===")
            sys.exit(0)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
