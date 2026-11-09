"""Shared fixtures for acceptance tests, against the real cluster in your kubeconfig
(`AGENT_KUBE_CONTEXT`, else the current context). Every step registers its cleanup first, so a
failure anywhere still removes what it created."""

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client as FastMCPClient
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_core._api import suppress_langchain_beta_warning
from pydantic import PositiveFloat
from pydantic_settings import BaseSettings, SettingsConfigDict
from typer.testing import CliRunner

from k8s_agent.cli import app
from k8s_agent.config import get_settings
from k8s_agent.kube import AdminClients, admin_clients, apply, read_or_none
from k8s_agent.scenarios import OWNER_LABEL, OWNER_VALUE, fault_live, get_scenario
from tests.fakes import diagnosis

with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter


class AcceptSettings(BaseSettings):
    """The acceptance tests' own settings, read like the app's (AGENT_*, from env vars or .env)."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # How long an assertion may keep retrying for state that flips between polls (e.g.
    # CrashLoopBackOff).
    accept_assert_timeout_seconds: PositiveFloat = 30
    # Setup (all but the scenarios suite) keeps scenarios already up (ours, manifests re-applied
    # cleanly, fault live) and leaves them up afterwards; a test that changes one (the remediate
    # suite's, on the selector Service) rebuilds it. False = fresh, removed after.
    accept_reuse_scenarios: bool = True
    # Test runs trace here, so the experiment users open (MLFLOW_EXPERIMENT_NAME) shows real runs.
    accept_mlflow_experiment_name: str = "k8s-agent-tests"


ACCEPT = AcceptSettings()
os.environ["MLFLOW_EXPERIMENT_NAME"] = ACCEPT.accept_mlflow_experiment_name
get_settings.cache_clear()

ROOT = Path(__file__).resolve().parents[2]
FOLDERS = ["crashloop", "selector", "readiness", "healthy"]
SECRET, SECRET_VALUE = "orders-db-password", "canary-value-must-not-leak"

# The crashloop scenario's right answer, without evidence (each test cites its own), for the
# critic and the judges to check.
ORDERS = {"namespace": "shop-orders"}
CONFIGMAP = {"apiVersion": "v1", "kind": "ConfigMap", "name": "orders-config", **ORDERS}
MISSPELT = "The ConfigMap key is misspelled: DATABSE_URL instead of DATABASE_URL."
CRASH = diagnosis(
    fault_category="bad_config",
    affected_resource={"kind": "ConfigMap", "name": "orders-config", **ORDERS},
    root_cause=MISSPELT,
    recommended_action="Rename the key to DATABASE_URL in ConfigMap orders-config.",
)


def agent(*args: str):
    """Run an `agent` CLI command in-process; fail the test if it exits non-zero."""
    get_settings.cache_clear()
    result = CliRunner().invoke(app, list(args))
    print(result.stderr)
    assert result.exit_code == 0, f"agent {' '.join(args)} failed:\n{result.stderr}"
    return result


def compose(*args: str) -> str:
    out = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return out.stdout


def namespace(folder: str) -> str:
    """The neutral namespace a scenario folder deploys into."""
    return get_scenario(get_settings().scenarios_dir, folder).namespace


def eventually(check: Callable[[], Any]) -> Any:
    """Retry `check` (which raises AssertionError until satisfied) for up to
    AGENT_ACCEPT_ASSERT_TIMEOUT_SECONDS, polling every AGENT_SCENARIO_POLL_SECONDS."""
    deadline = time.monotonic() + ACCEPT.accept_assert_timeout_seconds
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(get_settings().scenario_poll_seconds)


def mcp_call(tool: str, args: dict) -> tuple[bool, str]:
    """(is_error, text) of one MCP tool call, made the way the agent makes it."""

    async def go():
        s = get_settings()
        async with MCPAdapter(FastMCPClient(s.mcp_endpoint, timeout=s.mcp_timeout_seconds)) as a:
            tools = {t.name: t for t in await a.list_tools()}
            msg = await tools[tool].ainvoke(
                {"type": "tool_call", "id": "x", "name": tool, "args": args}
            )
            return msg.status == "error", msg.text

    return asyncio.run(go())


def crash_pod(kube: AdminClients) -> str:
    [pod] = [p.metadata.name for p in kube.core.list_namespaced_pod("shop-orders").items
             if not p.metadata.deletion_timestamp]  # fmt: skip
    return pod


def crash_log(read: Callable[..., str]) -> list[str]:
    """The crashing container's log, previous and current: on some runtimes the previous
    container's log is gone after a few restarts, and the current one has the same line."""
    logs = []
    for previous in (True, False):
        try:
            logs.append(read(previous=previous))
        except ApiException as e:
            logs.append(f"<{e.status} {e.reason}>")
    return logs


def reusable(kube: AdminClients, folder: str) -> bool:
    """A scenario already up that can be kept: ours, its manifests applied again without error
    (a changed RoleBinding roleRef can't be, so that one is recreated), and its fault live."""
    scn = get_scenario(get_settings().scenarios_dir, folder)
    ns = read_or_none(lambda: kube.core.read_namespace(scn.namespace))
    if ns is None or ns.metadata.deletion_timestamp is not None:
        return False
    if (ns.metadata.labels or {}).get(OWNER_LABEL) != OWNER_VALUE:
        return False  # `scenario up` refuses it with a clear error
    try:
        apply(kube, scn.manifests, lambda _: None)
    except ApiException:
        return False
    return fault_live(scn, kube)


@pytest.fixture(scope="session")
def kube() -> AdminClients:
    c = admin_clients(get_settings())
    print(f"\nacceptance tests on kube context: {c.context}")
    return c


@pytest.fixture(scope="session")
def cluster(kube: AdminClients, request):
    """The identities, all four scenarios (finding a namespace means choosing among four), a
    planted Secret to try to read, and the k8s-mcp server. With AGENT_ACCEPT_REUSE_SCENARIOS,
    scenarios already up are kept, and all are left up afterwards."""
    agent("setup")
    reuse = ACCEPT.accept_reuse_scenarios
    for folder in FOLDERS:
        if reuse and reusable(kube, folder):
            print(f"reusing scenario {folder}")
            continue
        if not reuse:
            request.addfinalizer(lambda f=folder: agent("scenario", "down", f))
        agent("scenario", "up", folder)
    # Refusal must come from policy, not "not found". The Secret goes with its namespace.
    body = {"metadata": {"name": SECRET}, "stringData": {"v": SECRET_VALUE}}
    try:
        kube.core.create_namespaced_secret("shop-orders", body)
    except ApiException as e:
        if e.status != 409:  # already planted in a reused scenario
            raise
    if not compose("ps", "-q", "--status", "running", "k8s-mcp").strip():
        request.addfinalizer(lambda: compose("rm", "-sf", "k8s-mcp"))
        print("starting k8s-mcp (stopped again afterwards)")
    compose("up", "-d", "--wait", "k8s-mcp")
    return kube


def investigate_cli(question: str, *args: str) -> tuple[dict[str, Any], str]:
    """`agent investigate --json` as a user runs it: (result, stderr). No terminal on stdin, so
    a fix it requests (selector) is rejected instead of waiting for y/N."""
    t0 = time.monotonic()
    done = subprocess.run(
        ["uv", "run", "agent", "investigate", question, "--json", *args],
        cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    seconds = time.monotonic() - t0
    print(f"\n$ agent investigate {question!r} {' '.join(args)}  ({seconds:.1f} s)\n{done.stderr}")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def selector_question() -> str:
    return get_scenario(get_settings().scenarios_dir, "selector").spec.question


@dataclass(frozen=True)
class SelectorRun:
    """The selector scenario's question through `investigate_cli` as alice (no namespace
    given), and its Service as it was just before and just after the run."""

    result: dict[str, Any]
    stderr: str
    before: client.V1Service
    after: client.V1Service


def run_selector(kube: AdminClients) -> SelectorRun:
    def checkout() -> client.V1Service:
        return kube.core.read_namespaced_service("checkout", namespace("selector"))

    before = checkout()
    result, stderr = investigate_cli(selector_question(), "--user", "alice")
    return SelectorRun(result, stderr, before, checkout())


@pytest.fixture(scope="session")
def selector_run(cluster: AdminClients) -> SelectorRun:
    """One real selector run, shared: test_tracing.py checks its trace, test_remediate.py its
    rejected fix request. Each makes a run of its own only if this one misses what it checks."""
    return run_selector(cluster)


def stored(trace_id: str) -> Any:
    """The trace as the tracking server holds it (an mlflow Trace). Spans are sent one by one,
    so wait until the root span is there and the count stops changing."""
    import mlflow

    seen = None
    for _ in range(30):
        trace = mlflow.get_trace(trace_id)
        spans = len(trace.data.spans) if trace else 0
        if trace and spans == seen and any(s.parent_id is None for s in trace.data.spans):
            return trace
        seen = spans
        time.sleep(1)
    pytest.fail(f"trace {trace_id} not complete on the server ({seen} spans)")
