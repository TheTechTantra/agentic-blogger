"""Naming rules shared between the gateway registration script and the clients.

There is exactly one thing in here, and it exists because two places must agree
on it: scripts/register_gateway.py, which names the endpoints, and
llm/registry.py, which addresses them. If those two ever disagree, every call
fails with "endpoint not found" and nothing in either file looks wrong.
"""

import re

# The gateway restricts endpoint names to letters, digits, underscore, hyphen
# and dot ("Invalid endpoint name ... Name can only contain letters, numbers,
# underscores, hyphens, and dots").
_INVALID = re.compile(r"[^A-Za-z0-9._-]")


def endpoint_name(model: str) -> str:
    """Gateway endpoint name for a model id.

    Almost always the model id verbatim -- 'claude-opus-5' stays
    'claude-opus-5' -- which is the property the whole design leans on: the
    chat client keeps sending the model id it always sent, and the gateway
    reads that field as the endpoint name.

    Ollama is the exception. Its tags contain a colon ('qwen3.5:9b') and the
    gateway rejects colons in names, so those get rewritten to a hyphen. Only
    the wire name changes: config/models.yaml still says 'ollama:qwen3.5:9b',
    and llm/callbacks.py still prices it under that key, because cost lookup
    never goes through here.
    """
    return _INVALID.sub("-", model)
