"""What every tool result goes through before the model sees it (SPEC §3.3): strip noise
fields, apply the safety cap, prefix the evidence ID. And how to read a failed call: the MCP
server gives no error code, only the Kubernetes API server's message (kubernetes-mcp-server
v0.1.2; tests/acceptance checks these against the real server)."""

import re
from typing import Any

import yaml
from yaml.composer import ComposerError

PROTECTED = frozenset({"spec", "status"})  # never stripped, never looked inside

# The reader was refused: groups are the verb, the resource and the namespace (none when
# cluster-wide), e.g. `cannot list resource "pods" in API group "" in the namespace "default"`.
REFUSED = re.compile(r'cannot (\w+) resource "([^"]+)".*?(?:in the namespace "([^"]+)"|$)')
# Errors a retry can't fix, so they aren't retried: refused (by Kubernetes, or by the MCP
# server's own denied_resources: `resource not allowed`), the object or kind doesn't exist
# (`pods "x" not found`, `no matches for kind "Foo"`), a required argument is missing.
PERMANENT = re.compile(
    r'is forbidden|resource not allowed|" not found|no matches for kind|parameter required'
)


class _NoAliases(yaml.SafeLoader):
    """SafeLoader without aliases. Object YAML from the MCP server has none, while nested
    aliases (a "billion laughs") would make walking and dumping the data blow up."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            mark = self.peek_event().start_mark
            raise ComposerError(None, None, "aliases are not allowed", mark)
        return super().compose_node(parent, index)


def _remove(node: Any, fields: frozenset[str]) -> Any:
    if isinstance(node, list):
        return [_remove(v, fields) for v in node]
    if not isinstance(node, dict):
        return node
    return {
        k: v if k in PROTECTED else _remove(v, fields)
        for k, v in node.items()
        if k in PROTECTED or k not in fields
    }


def strip_fields(text: str, fields: list[str]) -> str:
    """Remove keys named in `fields` from Kubernetes object YAML (a mapping with kind or
    apiVersion, or a list of mappings such as events), outside `spec` and `status`. Anything
    else, logs, tables and YAML with aliases included, comes back unchanged."""
    if not any(f in text for f in fields):
        return text
    try:
        data = yaml.load(text, Loader=_NoAliases)
    except yaml.YAMLError:
        return text
    is_object = isinstance(data, dict) and ("kind" in data or "apiVersion" in data)
    is_list = isinstance(data, list) and bool(data) and all(isinstance(d, dict) for d in data)
    if not (is_object or is_list):
        return text
    cleaned = _remove(data, frozenset(fields))
    if cleaned == data:
        return text
    return yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True, width=1000)


def cap(text: str, max_chars: int) -> str:
    """Unchanged up to `max_chars`; beyond it, a plain cut with a marker."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[… {len(text) - max_chars} characters cut …]"


def tag(ref: str, text: str) -> str:
    return f"[{ref}] {text}"


def untag(ref: str, text: str) -> str:
    return text.removeprefix(f"[{ref}] ")
