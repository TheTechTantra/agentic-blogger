"""Extract plain text from an AIMessage.content that may be a string or a
list of content blocks (thinking/redacted_thinking/text/tool_use/...).

Adaptive thinking (effort='high') turns response.content into a block list
— naively str()-ing it stringifies the thinking block (including its
signature) right into the output. Always go through this helper instead."""


def extract_text(content) -> str:
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)
