"""Static capability table — nodes branch on capability, never on provider name."""

CAPABILITIES = {
    "anthropic": {
        "supports_effort": True,
        "supports_json_schema": True,
        "reports_cache_tokens": True,
        "max_context": 1_000_000,
    },
    "openai": {
        "supports_effort": True,
        "supports_json_schema": True,
        "reports_cache_tokens": False,  # reads only, no cache-write count
        "max_context": 200_000,
    },
    "gemini": {
        "supports_effort": True,
        "supports_json_schema": True,
        "reports_cache_tokens": True,
        "max_context": 1_000_000,
    },
    "ollama": {
        "supports_effort": False,
        "supports_json_schema": False,
        "reports_cache_tokens": False,
        "max_context": 32_000,
    },
}


def capability(provider: str, key: str, default=None):
    return CAPABILITIES.get(provider, {}).get(key, default)
