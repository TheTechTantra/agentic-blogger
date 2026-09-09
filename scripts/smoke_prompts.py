#!/usr/bin/env python3
"""
Smoke test: MLflow Prompt Registry.

Verifies that:
  - every prompt in the manifest resolves at the configured alias
  - each template's {{variables}} are ones the nodes actually supply
  - prompts backing structured output carry a matching response_format
  - schema field descriptions bind onto the Pydantic models
  - every prompt renders with placeholder values (catches a template that
    references a variable the node does not pass)

Read-only against the registry. Makes no LLM calls and costs nothing.
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from agentic_blogger.config.loader import prompts_spec
from agentic_blogger.nodes.schemas import CodeExemplar, ResearchBrief, bind_descriptions
from agentic_blogger.prompts import PromptRegistryError, render
from agentic_blogger.prompts.preflight import check_all
from agentic_blogger.prompts.specs import SPECS


def main() -> int:
    cfg = prompts_spec()
    logger.info("=== Smoke Test: Prompt Registry ===")
    logger.info("alias=%s cache_ttl_seconds=%s", cfg["alias"], cfg["cache_ttl_seconds"])

    logger.info("\n--- preflight ---")
    try:
        versions = check_all()
    except PromptRegistryError as e:
        logger.error("%s", e)
        return 1
    for name, version in sorted(versions.items()):
        logger.info("  ✓ %-38s v%s", name, version)

    logger.info("\n--- render with placeholders ---")
    failures = 0
    for name, spec in sorted(SPECS.items()):
        try:
            render(name, {v: f"<{v}>" for v in spec.variables})
            logger.info("  ✓ %-38s %d var(s)", name, len(spec.variables))
        except PromptRegistryError as e:
            failures += 1
            logger.error("  ✗ %-38s %s", name, e)

    logger.info("\n--- schema descriptions ---")
    try:
        bind_descriptions()
    except Exception as e:
        logger.error("  ✗ bind_descriptions failed: %s", e)
        return 1

    tier_desc = CodeExemplar.model_fields["tier"].description
    logger.info("  CodeExemplar.tier -> %r", tier_desc)
    if not tier_desc:
        logger.error("  ✗ descriptions did not bind onto the model")
        failures += 1
    elif "{{" in tier_desc:
        logger.error("  ✗ unsubstituted template variable left in a description")
        failures += 1

    props = ResearchBrief.model_json_schema().get("properties", {})
    described = sum(1 for v in props.values() if v.get("description"))
    logger.info("  ResearchBrief: %d/%d top-level fields described", described, len(props))

    if failures:
        logger.error("\n=== SMOKE TEST FAILED (%d) ===", failures)
        return 1
    logger.info("\n=== SMOKE TEST PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
