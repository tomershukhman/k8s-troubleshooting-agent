"""Shared fixtures for acceptance tests. These run against the real cluster in your kubeconfig
(`AGENT_KUBE_CONTEXT`, else the current context)."""

import time
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from k8s_agent.cli import app
from k8s_agent.config import get_settings
from k8s_agent.kube import AdminClients, admin_clients
from k8s_agent.scenarios.spec import get_scenario

runner = CliRunner()


def agent(*args: str, env: dict[str, str] | None = None):
    """Run an `agent` CLI command in-process; fail the test if it exits non-zero. `env`
    overrides settings for this one command."""
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, list(args), env=env)
    finally:
        get_settings.cache_clear()
    print(result.stderr)
    assert result.exit_code == 0, f"agent {' '.join(args)} failed:\n{result.stderr}"
    return result


@pytest.fixture(scope="session")
def kube() -> AdminClients:
    c = admin_clients(get_settings())
    print(f"\nacceptance tests on kube context: {c.context}")
    return c


def eventually(check: Callable[[], Any]) -> Any:
    """Retry `check` (which raises AssertionError until satisfied) for up to
    AGENT_ACCEPT_ASSERT_TIMEOUT_SECONDS, polling every AGENT_SCENARIO_POLL_SECONDS."""
    s = get_settings()
    deadline = time.monotonic() + s.accept_assert_timeout_seconds
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(s.scenario_poll_seconds)


def scenario_namespace(folder: str) -> str:
    """The neutral namespace a scenario folder deploys into (set in its scenario.yaml)."""
    return get_scenario(get_settings().scenarios_dir, folder).spec.namespace
