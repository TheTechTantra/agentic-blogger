#!/usr/bin/env python3
"""
Export every registered prompt to files, for backup only.

This is a one-way mirror of the MLflow Prompt Registry into the repo so the
prompts survive losing the MLflow database. Nothing in agentic_blogger/ reads
these files, and nothing ever should — the registry is the source of truth and
a second load path would silently mask a registry outage.

Run it when you want a snapshot; committing and pushing the result is manual.
Nothing runs this on a schedule, so the backup is only as fresh as the last
time someone ran it.

Restoring from a backup is a deliberate, manual act too: adapt the texts back
into scripts/register_prompts.py and re-seed.

Usage:
    python -m scripts.export_prompts                  # -> prompts_backup/
    python -m scripts.export_prompts --out-dir DIR
    python -m scripts.export_prompts --all-versions   # not just the alias
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from agentic_blogger.config.loader import prompts_spec
from agentic_blogger.prompts.specs import SPECS


def _write(out_dir: Path, name: str, prompt) -> dict:
    template = prompt.template
    body = template if isinstance(template, str) else json.dumps(template, indent=2)
    (out_dir / f"{name}.txt").write_text(body)
    return {
        "name": name,
        "version": prompt.version,
        "variables": sorted(prompt.variables),
        "has_response_format": bool(prompt.response_format),
        "commit_message": prompt.commit_message,
    }


def main(out_dir: Path, all_versions: bool) -> int:
    import mlflow.genai

    from agentic_blogger.prompts.registry import _configure

    _configure()
    alias = prompts_spec()["alias"]
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "alias": alias,
        "note": "BACKUP ONLY — the MLflow Prompt Registry is the source of truth. "
                "Nothing at runtime reads these files.",
        "prompts": [],
    }

    failures = 0
    for name in sorted(SPECS):
        try:
            if all_versions:
                versions = mlflow.genai.search_prompt_versions(name)
                for pv in versions:
                    entry = _write(out_dir, f"{name}.v{pv.version}", pv)
                    manifest["prompts"].append(entry)
                    logger.info("✓ %s v%s", name, pv.version)
            else:
                # cache_ttl_seconds=0: a backup must reflect the registry right
                # now, not whatever a warm cache is still serving.
                pv = mlflow.genai.load_prompt(
                    f"prompts:/{name}@{alias}", cache_ttl_seconds=0, link_to_model=False
                )
                manifest["prompts"].append(_write(out_dir, name, pv))
                logger.info("✓ %-38s v%s", name, pv.version)
        except Exception as e:
            failures += 1
            logger.error("✗ %-38s %s", name, e)

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("\nExported %d prompt(s) to %s (%d failure(s))",
                len(manifest["prompts"]), out_dir, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=Path("prompts_backup"))
    ap.add_argument("--all-versions", action="store_true")
    sys.exit(main(ap.parse_args().out_dir, ap.parse_args().all_versions))
