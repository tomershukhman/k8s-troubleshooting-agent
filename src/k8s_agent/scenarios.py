"""The broken scenarios (SPEC §2.2, §4.4). Each `scenarios/<folder>/` holds plain Kubernetes
YAML in `manifests/` (its Namespace, the agent's RoleBindings there, and the app) and a
`scenario.yaml` with the question and the expected answers. The folder name describes the
fault, so it never reaches the cluster."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kubernetes import client
from pydantic import Field

from k8s_agent.kube import AdminClients, Progress, apply, load_yaml, read_or_none
from k8s_agent.schemas import DiagnosisStatus, FaultCategory, Strict

# `up` and `down` touch only namespaces with this label (set in each namespace.yaml).
OWNER_LABEL, OWNER_VALUE = "app.kubernetes.io/part-of", "k8s-agent-demo"


class ScenarioError(Exception):
    """A scenario is missing or malformed, or its namespace isn't ours."""


class ResourceName(Strict):
    kind: str
    name: str


class Expected(Strict):
    """Accepted answer sets (SPEC §2.2)."""

    status: DiagnosisStatus
    fault_categories: list[FaultCategory]
    resources: list[ResourceName] = []


class ExpectedFix(Strict):
    """The fix the scenario expects the agent to propose (SPEC §3.4)."""

    type: str
    namespace: str
    service: str
    selector: dict[str, str]


class ToolUse(Strict):
    """A tool call the agent should make: this tool, with at least these arguments."""

    tool: str
    args: dict[str, str] = {}


class ReadyWhen(Strict):
    """Proof the fault is live: at least this many pods Ready, Running but not Ready, and
    restarts of one container."""

    pods_ready: int = 0
    pods_not_ready: int = 0
    restarts: int = 0


class ScenarioSpec(Strict):
    question: str
    ready_when: ReadyWhen
    expected: Expected
    # What the agent must have looked at (evals): each entry is a list of alternatives, any one
    # of which satisfies it.
    required_tools: list[list[ToolUse]] = Field(min_length=1)
    judge_rubric: list[str] = Field(min_length=1)  # the advice judge's guidelines (evals)
    expected_fix: ExpectedFix | None = None


class Scenario(Strict):
    name: str  # the folder: describes the fault, never shown to the agent
    spec: ScenarioSpec
    manifests: list[dict[str, Any]]

    @property
    def namespace(self) -> str:
        return next(d["metadata"]["name"] for d in self.manifests if d["kind"] == "Namespace")


def load_all(scenarios_dir: Path) -> dict[str, Scenario]:
    folders = sorted(p for p in scenarios_dir.glob("*/scenario.yaml"))
    if not folders:
        raise ScenarioError(f"no scenarios in {scenarios_dir.resolve()}")
    return {
        f.parent.name: Scenario(
            name=f.parent.name,
            spec=ScenarioSpec.model_validate(load_yaml(f)[0]),
            # namespace.yaml first: the namespace and the agent's access, then the app
            manifests=load_yaml(
                *sorted(
                    (f.parent / "manifests").glob("*.yaml"),
                    key=lambda p: p.name != "namespace.yaml",
                )
            ),
        )
        for f in folders
    }


def get_scenario(scenarios_dir: Path, name: str) -> Scenario:
    scenarios = load_all(scenarios_dir)
    if name not in scenarios:
        raise ScenarioError(f"unknown scenario {name!r}; known: {', '.join(scenarios)}")
    return scenarios[name]


def live_state(pods: list[client.V1Pod]) -> ReadyWhen:
    """What `ready_when` compares against, from the pods not being deleted."""
    ready = not_ready = restarts = 0
    for pod in pods:
        if pod.metadata.deletion_timestamp is not None:
            continue
        conditions = pod.status.conditions or []
        is_ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
        ready += is_ready
        not_ready += pod.status.phase == "Running" and not is_ready
        for cs in pod.status.container_statuses or []:
            restarts = max(restarts, cs.restart_count)
    return ReadyWhen(pods_ready=ready, pods_not_ready=not_ready, restarts=restarts)


def fault_live(scn: Scenario, c: AdminClients) -> bool:
    """The scenario's `ready_when` holds: its fault can be observed."""
    got = live_state(c.core.list_namespaced_pod(scn.namespace).items)
    want = scn.spec.ready_when
    return all(getattr(got, f) >= getattr(want, f) for f in ReadyWhen.model_fields)


def wait(what: str, done: Callable[[], Any], *, timeout: float, poll: float) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{what} after {timeout:g}s")
        time.sleep(poll)


def down(
    scn: Scenario, c: AdminClients, *, timeout: float, poll: float, progress: Progress
) -> bool:
    """Delete the scenario namespace and wait until it's gone. False if it was absent."""
    ns = read_or_none(lambda: c.core.read_namespace(scn.namespace))
    if ns is None:
        return False
    if (ns.metadata.labels or {}).get(OWNER_LABEL) != OWNER_VALUE:
        raise ScenarioError(
            f"namespace {scn.namespace} exists but wasn't created by `agent scenario` "
            f"(no {OWNER_LABEL}={OWNER_VALUE} label); refusing to touch it"
        )
    if ns.metadata.deletion_timestamp is None:
        c.core.delete_namespace(scn.namespace)
        progress(f"deleting namespace {scn.namespace}")

    def gone() -> bool:
        return read_or_none(lambda: c.core.read_namespace(scn.namespace)) is None

    wait(f"namespace {scn.namespace} still exists", gone, timeout=timeout, poll=poll)
    return True


def up(
    scn: Scenario,
    c: AdminClients,
    *,
    ready_timeout: float,
    delete_timeout: float,
    poll: float,
    progress: Progress,
) -> None:
    """Recreate the scenario from scratch (no state survives a re-run) and wait until its
    fault is live."""
    down(scn, c, timeout=delete_timeout, poll=poll, progress=progress)
    apply(c, [d for d in scn.manifests if d["kind"] == "Namespace"], progress)

    # Pods created before the namespace's default ServiceAccount exists are rejected, which
    # leaves FailedCreate warnings the agent would see.
    def has_default_sa() -> bool:
        read = lambda: c.core.read_namespaced_service_account("default", scn.namespace)  # noqa: E731
        return read_or_none(read) is not None

    wait("no default ServiceAccount", has_default_sa, timeout=ready_timeout, poll=poll)
    apply(c, [d for d in scn.manifests if d["kind"] != "Namespace"], progress)

    want = scn.spec.ready_when
    live = lambda: fault_live(scn, c)  # noqa: E731
    wait(f"{scn.name}: fault not live ({want})", live, timeout=ready_timeout, poll=poll)
    progress(f"ready: {want}")
