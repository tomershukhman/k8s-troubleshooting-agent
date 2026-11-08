"""Settings parsing. No cluster."""

import pytest
from pydantic import ValidationError

from k8s_agent.config import get_settings


def test_lists_are_comma_separated(env):
    env(remediation_namespaces=" shop-a, shop-b ,,", tool_output_strip_fields="managedFields")
    assert get_settings().remediation_namespaces == ["shop-a", "shop-b"]


def test_mcp_endpoint_follows_port_unless_set(env):
    env(mcp_port="9090")
    assert get_settings().mcp_endpoint == "http://localhost:9090/mcp"
    env(mcp_url="http://localhost:1/mcp")
    assert get_settings().mcp_endpoint == "http://localhost:1/mcp"


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
