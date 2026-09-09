"""Job-start verification that the registry can actually serve this pipeline.

Runs before the graph is invoked. The point is spend: without it, a prompt
edited in the MLflow UI to rename a variable would sail through research on
Opus and only fail at the draft node, after the expensive calls. Failing here
costs one round-trip per prompt and nothing else.

Three checks per prompt:
  - it resolves at the configured alias;
  - every {{variable}} in the template is one the node code supplies
    (declared in specs.py). Extra declared-but-unused variables are fine —
    the reverse is what breaks at render time;
  - a prompt that backs structured output carries a response_format whose
    top-level properties match the Pydantic model the node passes.
"""

import logging

from agentic_blogger.prompts.registry import PromptRegistryError, drain, load
from agentic_blogger.prompts.specs import SPECS, PromptSpec

logger = logging.getLogger(__name__)


def _check_response_format(prompt, spec: PromptSpec) -> list[str]:
    if spec.response_format is None:
        return []

    registered = prompt.response_format
    if not registered:
        return [f"{spec.name}: expected a response_format for "
                f"{spec.response_format.__name__}, registry has none"]

    expected = set(spec.response_format.model_json_schema().get("properties", {}))
    # MLflow stores the response format as a JSON schema dict, but nests it
    # under "schema"/"json_schema" depending on how it was registered.
    props = registered.get("properties")
    if props is None:
        for key in ("schema", "json_schema"):
            nested = registered.get(key)
            if isinstance(nested, dict) and "properties" in nested:
                props = nested["properties"]
                break
    if props is None:
        return [f"{spec.name}: registered response_format has no properties block"]

    actual = set(props)
    if actual != expected:
        return [f"{spec.name}: response_format fields {sorted(actual)} != "
                f"{spec.response_format.__name__} fields {sorted(expected)}"]
    return []


def check_all(names: list[str] | None = None) -> dict[str, int]:
    """Verify every manifest prompt. Returns {prompt_name: version}.

    Raises PromptRegistryError listing every problem found, rather than
    stopping at the first — one restart should surface the whole mess.
    """
    targets = names or list(SPECS)
    problems: list[str] = []
    versions: dict[str, int] = {}

    for name in targets:
        spec = SPECS[name]
        try:
            prompt = load(name)
        except PromptRegistryError as e:
            # A load failure means the registry is unreachable, and the
            # remaining lookups would fail identically after the same retry
            # budget each. Stop here: 21 serial timeouts turn a fast failure
            # into a multi-minute hang. Validation problems below still
            # accumulate, so one restart surfaces all of those at once.
            problems.append(str(e))
            problems.append(f"aborted after {name}: registry unreachable, "
                            f"{len(targets) - targets.index(name) - 1} prompt(s) unchecked")
            break

        versions[name] = prompt.version

        undeclared = set(prompt.variables) - set(spec.variables)
        if undeclared:
            problems.append(
                f"{name}: template uses {sorted(undeclared)}, which the node does "
                f"not supply (declared: {sorted(spec.variables)})"
            )

        problems.extend(_check_response_format(prompt, spec))

    if problems:
        raise PromptRegistryError(
            f"prompt preflight failed ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems)
        )

    # Preflight touches every prompt; leaving them in the accumulator would
    # attribute all of them to whichever node writes the first node_run.
    drain()
    logger.info("prompt preflight OK — %d prompts resolved", len(versions))
    return versions
