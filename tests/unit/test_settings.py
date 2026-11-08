"""Settings parsing for the M2 options. No cluster."""

import pytest
from pydantic import ValidationError

from k8s_agent.config import Settings


def settings(monkeypatch, **env) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(f"AGENT_{key.upper()}", value)
    return Settings()  # conftest.py: no .env in the working directory


def test_defaults(monkeypatch):
    s = settings(monkeypatch)
    assert s.namespaces == ["shop-orders", "shop-checkout", "shop-catalog", "shop-inventory"]
    assert s.remediation_namespaces == ["shop-checkout"]
    assert s.token_ttl_hours == 12
    assert s.mcp_endpoint == "http://localhost:8080/mcp"


def test_namespaces_are_comma_separated(monkeypatch):
    s = settings(monkeypatch, namespaces=" shop-a, shop-b ,,", remediation_namespaces="shop-b")
    assert s.namespaces == ["shop-a", "shop-b"]
    assert s.remediation_namespaces == ["shop-b"]


@pytest.mark.parametrize("var", ["namespaces", "remediation_namespaces"])
@pytest.mark.parametrize("ns", ["kube-system", "k8s-agent"])
def test_system_namespaces_rejected(monkeypatch, var, ns):
    with pytest.raises(ValidationError, match=f"may never be granted {ns}"):
        settings(monkeypatch, **{var: f"shop-a,{ns}"})


def test_mcp_endpoint_follows_port_unless_set(monkeypatch):
    assert settings(monkeypatch, mcp_port="9090").mcp_endpoint == "http://localhost:9090/mcp"
    s = settings(monkeypatch, mcp_port="9090", mcp_url="http://localhost:1/mcp")
    assert s.mcp_endpoint == "http://localhost:1/mcp"


def test_token_ttl_below_the_api_minimum_is_rejected(monkeypatch):
    with pytest.raises(ValidationError, match="token_ttl_hours"):
        settings(monkeypatch, token_ttl_hours="0.1")
    assert settings(monkeypatch, token_ttl_hours="0.25").token_ttl_hours == 0.25
