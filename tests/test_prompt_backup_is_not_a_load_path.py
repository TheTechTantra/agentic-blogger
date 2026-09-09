"""The exported prompt backup must never become a second source of truth.

The registry is authoritative and has no fallback by design: a job that cannot
reach MLflow is supposed to fail. If runtime code ever started reading the
backup files, an outage would silently serve stale prompts instead — exactly
the failure this design rejects. These tests fail if that line is crossed.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "agentic_blogger"


def _runtime_sources():
    return [p for p in PACKAGE.rglob("*.py")]


def test_runtime_never_imports_the_export_script():
    offenders = [
        p.relative_to(ROOT)
        for p in _runtime_sources()
        if "export_prompts" in p.read_text()
    ]
    assert not offenders, f"runtime code references the backup exporter: {offenders}"


def test_runtime_never_reads_the_backup_directory():
    offenders = [
        p.relative_to(ROOT)
        for p in _runtime_sources()
        if "prompts_backup" in p.read_text()
    ]
    assert not offenders, f"runtime code reads the prompt backup: {offenders}"


def test_no_prompt_text_left_in_the_nodes():
    """Nodes may build strings, but must not carry model-facing instructions.

    Heuristic, deliberately narrow: flags the imperative phrasings that the
    migrated prompts used, so a prompt quietly reintroduced into a node is
    caught. Formatting helpers and placeholders are not prompt text.
    """
    markers = (
        "Write a complete",
        "Fact-check the following",
        "Revise the following",
        "produce SEO metadata",
        "Research the topic",
        "You are a staff-level",
        "Attribution requirements",
    )
    offenders = []
    for p in (PACKAGE / "nodes").rglob("*.py"):
        text = p.read_text()
        for marker in markers:
            if marker in text:
                offenders.append(f"{p.relative_to(ROOT)}: {marker!r}")
    assert not offenders, "prompt text found in node code: " + "; ".join(offenders)
