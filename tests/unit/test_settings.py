"""Settings parsing. No cluster."""

import os

import pytest
from pydantic import ValidationError

from k8s_agent.config import get_settings


def test_lists_are_comma_separated(env):
    env(remediation_namespaces=" shop-a, shop-b ,,")
    assert get_settings().remediation_namespaces == ["shop-a", "shop-b"]


def test_mcp_endpoint_follows_port_unless_set(env):
    env(mcp_url="", mcp_port="9090")
    assert get_settings().mcp_endpoint == "http://localhost:9090/mcp"
    env(mcp_url="http://localhost:1/mcp")
    assert get_settings().mcp_endpoint == "http://localhost:1/mcp"


def test_user_id_falls_back_to_the_uid_when_the_os_user_is_unknown(monkeypatch):
    # The image's non-root uid has no passwd entry; a plain `docker run` sets no USER.
    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)

    def no_passwd_entry(uid: int):
        raise KeyError(uid)

    monkeypatch.setattr("pwd.getpwuid", no_passwd_entry)
    assert get_settings().effective_user_id == f"uid-{os.getuid()}"


def test_critic_model_defaults_to_the_agent_model(env):
    env(model="anthropic:claude-x")
    assert get_settings().effective_critic_model == "anthropic:claude-x"
    env(critic_model="anthropic:claude-y")
    assert get_settings().effective_critic_model == "anthropic:claude-y"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"token_ttl_hours": "0.1"}, "token_ttl_hours"),  # below the TokenRequest minimum
        ({"max_tool_calls": "31"}, "can't exceed AGENT_RUN_MAX_TOOL_CALLS"),
        ({"max_model_calls": "41"}, "can't exceed AGENT_RUN_MAX_MODEL_CALLS"),
    ],
)
def test_invalid_settings_are_rejected(env, values, message):
    env(**values)
    with pytest.raises(ValidationError, match=message):
        get_settings()
