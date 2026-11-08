"""Shared fixtures for acceptance tests. These run against the real cluster in your kubeconfig
(`AGENT_KUBE_CONTEXT`, else the current context)."""

import time
from collections.abc import Callable
from typing import Any

import pytest

from k8s_agent.config import get_settings
from k8s_agent.kube import AdminClients, admin_clients


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
