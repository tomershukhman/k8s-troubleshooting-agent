"""M1: each scenario comes up with its fault live, and `down` removes it (PLAN.md M1)."""

import pytest
from kubernetes.client.exceptions import ApiException
from typer.testing import CliRunner

from k8s_agent.cli import app
from k8s_agent.kube import AdminClients
from k8s_agent.scenarios.cluster import pod_is_ready

from .conftest import eventually

runner = CliRunner()


def agent(*args: str):
    result = runner.invoke(app, list(args))
    print(result.stderr)
    assert result.exit_code == 0, f"agent {' '.join(args)} failed:\n{result.stderr}"
    return result


def pods(kube: AdminClients, ns: str):
    return [p for p in kube.core.list_namespaced_pod(ns).items if not p.metadata.deletion_timestamp]


def endpoints(kube: AdminClients, ns: str, service: str) -> tuple[int, int]:
    """(all endpoints, ready endpoints) of a Service, from its EndpointSlices."""
    slices = kube.discovery.list_namespaced_endpoint_slice(
        ns, label_selector=f"kubernetes.io/service-name={service}"
    ).items
    eps = [ep for s in slices for ep in (s.endpoints or [])]
    return len(eps), sum(bool(ep.conditions and ep.conditions.ready) for ep in eps)


def events(kube: AdminClients, ns: str):
    return kube.core.list_namespaced_event(ns).items


# --- per-scenario fault assertions -----------------------------------------------------


def _log(kube: AdminClients, pod: str, ns: str, *, previous: bool) -> str:
    try:
        return kube.core.read_namespaced_pod_log(pod, ns, previous=previous)
    except ApiException as e:
        return f"<{e.status} {e.reason}>"


def assert_crashloop(kube: AdminClients, ns: str) -> None:
    # Assert on durable facts. The current state flips between waiting/CrashLoopBackOff and
    # terminated/Error (on OrbStack it can sit in terminated/Error for a whole back-off).
    [pod] = pods(kube, ns)
    [cs] = pod.status.container_statuses
    assert cs.restart_count >= 2
    assert cs.last_state.terminated is not None, f"last state: {cs.last_state}"
    assert cs.last_state.terminated.exit_code == 1
    assert not cs.ready and not pod_is_ready(pod)

    def crash_looping():  # retried: a restart can briefly show `running`
        [p] = pods(kube, ns)
        state = p.status.container_statuses[0].state
        backoff = state.waiting is not None and state.waiting.reason == "CrashLoopBackOff"
        errored = state.terminated is not None and state.terminated.reason == "Error"
        assert backoff or errored, f"container state: {state}"

    eventually(crash_looping)

    # The previous container's log can be gone after a few restarts ("unable to retrieve
    # container logs"); then the current (terminated) container's log has the same line.
    name = pod.metadata.name
    logs = [_log(kube, name, ns, previous=True), _log(kube, name, ns, previous=False)]
    assert any("DATABASE_URL not set" in log for log in logs), logs


def assert_selector(kube: AdminClients, ns: str) -> None:
    ps = pods(kube, ns)
    assert ps and all(pod_is_ready(p) for p in ps)
    assert ps[0].metadata.labels["app"] == "checkout"
    assert kube.core.read_namespaced_service("checkout", ns).spec.selector == {"app": "check-out"}
    assert endpoints(kube, ns, "checkout") == (0, 0)


def assert_readiness(kube: AdminClients, ns: str) -> None:
    ps = pods(kube, ns)
    assert ps
    for p in ps:
        assert p.status.phase == "Running"
        assert not pod_is_ready(p)
    messages = [e.message or "" for e in events(kube, ns)]
    assert any("Readiness probe failed" in m and "404" in m for m in messages), messages
    assert endpoints(kube, ns, "catalog")[1] == 0


def assert_healthy(kube: AdminClients, ns: str) -> None:
    ps = pods(kube, ns)
    assert ps and all(pod_is_ready(p) for p in ps)
    assert endpoints(kube, ns, "inventory")[1] >= 1
    warnings = [f"{e.reason}: {e.message}" for e in events(kube, ns) if e.type == "Warning"]
    assert warnings == []


FAULTS = {
    "crashloop": assert_crashloop,
    "selector": assert_selector,
    "readiness": assert_readiness,
    "healthy": assert_healthy,
}


@pytest.mark.parametrize("name", list(FAULTS))
def test_scenario_up_fault_down(kube: AdminClients, name: str):
    ns = f"scn-{name}"
    try:
        agent("scenario", "up", name)
        FAULTS[name](kube, ns)
    finally:
        agent("scenario", "down", name)

    with pytest.raises(ApiException) as gone:
        kube.core.read_namespace(ns)
    assert gone.value.status == 404


def test_list_shows_all_scenarios():
    out = agent("scenario", "list").stdout
    assert sorted(line.split()[0] for line in out.splitlines()) == sorted(FAULTS)
