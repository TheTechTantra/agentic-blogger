#!/usr/bin/env python3
"""
Smoke test: every LLM role reaches its model through the MLflow AI Gateway.

Complements scripts/smoke_llm.py rather than replacing it: that one proves a
model answers, this one proves the *route* is the gateway and nothing was lost
in the hop. The three things checked beyond "it answered" are the three that a
proxy silently breaks:

  1. usage_metadata survives, including the prompt-cache buckets that
     the gateway prices separately. Lose them and cost silently under-reports.
  2. Anthropic server tools still execute server-side and their result blocks
     come back, which is the whole research node.
  3. The ollama fallback is reachable, so a haiku outage degrades instead of
     failing.

Every assertion also fails if a call quietly went direct to a provider: the
worker holds no provider credential, so a direct call cannot succeed at all.

Usage:
    python -m scripts.smoke_gateway
    python -m scripts.smoke_gateway --skip-ollama   # no local Ollama running
"""

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from agentic_blogger.config.loader import gateway_spec, load_models
from agentic_blogger.llm.gateway import endpoint_name
from agentic_blogger.llm.registry import _build_raw, build_llm

failures: list[str] = []


def check(condition: bool, message: str) -> bool:
    if condition:
        logger.info("  ✓ %s", message)
    else:
        logger.error("  ✗ %s", message)
        failures.append(message)
    return condition


def _base_url(llm) -> str:
    """Where this client will actually send. Differs by integration:
    ChatAnthropic exposes anthropic_api_url, ChatOpenAI openai_api_base."""
    for attr in ("anthropic_api_url", "openai_api_base", "base_url"):
        val = getattr(llm, attr, None)
        if val:
            return str(val)
    return "<unknown>"


def smoke_roles() -> None:
    gw = gateway_spec()
    for role in sorted(load_models()["roles"]):
        logger.info("\nrole=%s", role)
        llm = build_llm(role)
        url = _base_url(llm)
        check(url.startswith(gw["base_url"]),
              f"addressed to the gateway ({url})")

        resp = llm.invoke([{"role": "user", "content": "Reply with exactly: OK"}])
        usage = resp.usage_metadata or {}
        check(bool(usage.get("input_tokens")) and bool(usage.get("output_tokens")),
              f"usage_metadata in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
        # The cache buckets are the fragile part -- a translating proxy drops
        # them while leaving the top-level counts intact, so cost reporting
        # degrades without anything looking broken.
        check("input_token_details" in usage,
              "input_token_details present (prompt-cache buckets intact)")


def smoke_server_tools() -> None:
    from agentic_blogger.nodes.research import _SEARCH_TOOL

    logger.info("\nserver tools (search_tool role)")
    llm = build_llm("search_tool").bind_tools([_SEARCH_TOOL])
    resp = llm.invoke([{
        "role": "user",
        "content": "Search the web for the current stable Python version. "
                   "Use the web_search tool. Answer in one sentence.",
    }])
    blocks = resp.content if isinstance(resp.content, list) else []
    kinds = {b.get("type") for b in blocks if isinstance(b, dict)}
    check("web_search_tool_result" in kinds,
          f"web_search executed server-side (blocks: {sorted(kinds)})")
    check(isinstance(resp.content, list),
          "response content is a block list (llm/text.py extract_text depends on it)")


def smoke_fallback() -> None:
    logger.info("\nfallback (ollama:qwen3.5:9b)")
    llm = _build_raw("ollama:qwen3.5:9b", 256, "low")
    check(_base_url(llm).endswith("/mlflow/v1"),
          "fallback uses the unified surface, not a direct Ollama connection")
    resp = llm.invoke([{"role": "user", "content": "Reply with exactly: OK"}])
    check(bool(resp.content), f"answered through the gateway ({str(resp.content)[:40]!r})")


def smoke_naming() -> None:
    """The convention the whole design rests on: what the client puts in the
    request's `model` field is the gateway endpoint name."""
    logger.info("\nendpoint naming")
    cfg = load_models()
    keys = {s["model"] for s in cfg["roles"].values()}
    for fbs in (cfg.get("fallbacks") or {}).values():
        keys.update(fbs)
    for key in sorted(keys):
        _, model = key.split(":", 1)
        check(bool(endpoint_name(model)), f"{key} -> endpoint {endpoint_name(model)!r}")


def main(skip_ollama: bool) -> int:
    gw = gateway_spec()
    if not gw["enabled"]:
        logger.error("gateway.enabled is false in config/models.yaml — nothing to smoke")
        return 1
    logger.info("Gateway %s", gw["base_url"])
    logger.info("Surfaces %s", gw["surfaces"])

    smoke_naming()
    smoke_roles()
    smoke_server_tools()
    if skip_ollama:
        logger.info("\nfallback (ollama) — skipped")
    else:
        smoke_fallback()

    logger.info("")
    if failures:
        logger.error("SMOKE FAILED — %d check(s):", len(failures))
        for f in failures:
            logger.error("  - %s", f)
        return 1
    logger.info("SMOKE PASSED — every role routes through the gateway with usage intact.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ollama", action="store_true",
                    help="skip the fallback check when no local Ollama is running")
    sys.exit(main(ap.parse_args().skip_ollama))
