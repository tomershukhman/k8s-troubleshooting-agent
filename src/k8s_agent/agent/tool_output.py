"""Tool output, and text that may repeat it (DESIGN 3.4):
- what every tool result goes through before the model sees it: the safety cap, then the
  evidence ID as a prefix;
- how to read a failed call: the MCP server gives no error code, only the Kubernetes API
  server's message (kubernetes-mcp-server v0.1.2; tests/acceptance checks these patterns
  against the real server);
- how to quote untrusted text (tool output, or a model's words that may repeat it) to another
  model: inside tags it can't close."""

import re

# The reader was refused: groups are the verb, the resource and the namespace (none when
# cluster-wide), e.g. `cannot list resource "pods" in API group "" in the namespace "default"`.
REFUSED = re.compile(r'cannot (\w+) resource "([^"]+)".*?(?:in the namespace "([^"]+)"|$)')
# Errors a retry can't fix, so they aren't retried: refused (by Kubernetes, or by the MCP
# server's own denied_resources: `resource not allowed`), the object or kind doesn't exist
# (`pods "x" not found`, `no matches for kind "Foo"`), a required argument is missing.
PERMANENT = re.compile(
    r'is forbidden|resource not allowed|" not found|no matches for kind|parameter required'
)


def cap(text: str, max_chars: int) -> str:
    """Unchanged up to `max_chars`; beyond it, a plain cut with a marker."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[… {len(text) - max_chars} characters cut …]"


def tag(ref: str, text: str) -> str:
    return f"[{ref}] {text}"


def untag(ref: str, text: str) -> str:
    return text.removeprefix(f"[{ref}] ")


def inert(text: str, tag: str) -> str:
    """`text` with every `<tag>` or `</tag>` in it (any case or spacing) made inert, so text
    quoted inside a `<tag>` block can't close it early and add text of its own."""
    return re.sub(rf"<(\s*/?\s*{re.escape(tag)}\b)", r"&lt;\1", text, flags=re.IGNORECASE)


def quoted(tag: str, text: str) -> str:
    """`text` as a `<tag>` block it can't break out of."""
    return f"<{tag}>\n{inert(text, tag)}\n</{tag}>"
