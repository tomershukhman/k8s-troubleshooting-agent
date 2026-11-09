"""Each scenario comes up with its fault live, and `down` removes it."""

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_agent.kube import AdminClients

from .conftest import FOLDERS, agent, crash_log, crash_pod, eventually, namespace


def pods(kube: AdminClients, ns: str):
    return [p for p in kube.core.list_namespaced_pod(ns).items if not p.metadata.deletion_timestamp]


def ready(pod) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or [])


def endpoints(kube: AdminClients, ns: str, service: str) -> tuple[int, int]:
    """(all, ready) endpoints of a Service, from its EndpointSlices."""
    slices = kube.dynamic.resources.get(api_version="discovery.k8s.io/v1", kind="EndpointSlice")
    items = slices.get(namespace=ns, label_selector=f"kubernetes.io/service-name={service}").items
    eps = [ep for s in items for ep in (s.endpoints or [])]
    return len(eps), sum(bool(ep.conditions and ep.conditions.ready) for ep in eps)


def messages(kube: AdminClients, ns: str) -> list[str]:
    return [f"{e.type} {e.reason}: {e.message}" for e in kube.core.list_namespaced_event(ns).items]


def crashloop(kube, ns):
    [p] = pods(kube, ns)
    [cs] = p.status.container_statuses
    # durable facts: the current state flips between CrashLoopBackOff and terminated/Error
    assert cs.restart_count >= 2 and cs.last_state.terminated.exit_code == 1
    assert not ready(p)
    name = crash_pod(kube)
    logs = crash_log(
        lambda previous: kube.core.read_namespaced_pod_log(name, ns, previous=previous)
    )
    assert any("DATABASE_URL not set" in log for log in logs), logs


def selector(kube, ns):
    ps = pods(kube, ns)
    assert ps and all(ready(p) for p in ps) and ps[0].metadata.labels["app"] == "checkout"
    assert kube.core.read_namespaced_service("checkout", ns).spec.selector == {"app": "check-out"}
    assert endpoints(kube, ns, "checkout") == (0, 0)


def readiness(kube, ns):
    ps = pods(kube, ns)
    assert ps and all(p.status.phase == "Running" and not ready(p) for p in ps)

    def probe_failed():  # the first probe failure follows the pod start by a few seconds
        assert any("Readiness probe failed" in m and "404" in m for m in messages(kube, ns))

    eventually(probe_failed)
    assert endpoints(kube, ns, "catalog")[1] == 0


def healthy(kube, ns):
    ps = pods(kube, ns)
    assert ps and all(ready(p) for p in ps)

    def has_endpoints():  # the EndpointSlice can lag the pods' Ready
        assert endpoints(kube, ns, "inventory")[1] >= 1

    eventually(has_endpoints)
    assert [m for m in messages(kube, ns) if m.startswith("Warning")] == []


FAULTS = {"crashloop": crashloop, "selector": selector, "readiness": readiness, "healthy": healthy}


@pytest.mark.parametrize("name", FOLDERS)
def test_scenario_up_fault_down(kube: AdminClients, name: str):
    ns = namespace(name)
    try:
        agent("scenario", "up", name)
        FAULTS[name](kube, ns)
    finally:
        agent("scenario", "down", name)
    with pytest.raises(ApiException) as gone:
        kube.core.read_namespace(ns)
    assert gone.value.status == 404
