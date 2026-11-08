"""The MCP server's tool allowlist must equal ours, which the agent enforces again when it loads
the tools (SPEC 3.3). Everything else about the server is checked on the running container by
the M2 acceptance tests."""

import tomllib
from pathlib import Path

from k8s_agent.policy import READ_ONLY_TOOLS

CONFIG = Path(__file__).resolve().parents[2] / "mcp" / "config.toml"


def test_server_allowlist_matches_ours():
    enabled = tomllib.loads(CONFIG.read_text())["enabled_tools"]
    assert sorted(enabled) == sorted(READ_ONLY_TOOLS)
