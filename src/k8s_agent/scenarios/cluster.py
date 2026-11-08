"""`scenario up` / `scenario down` against the cluster, with the user's admin kubeconfig."""

import time
from collections.abc import Callable, Iterable
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from k8s_agent.kube import AdminClients
from k8s_agent.scenarios.spec import (
    Condition,
    EndpointsReady,
    EventsContain,
    PodNotReady,
    PodRestartsAtLeast,
    PodsReady,
    Scenario,
    ScenarioError,
)

FIELD_MANAGER = "k8s-agent-scenarios"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
SCENARIO_LABEL = "k8s-agent/scenario"

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


# --- conditions (pure functions over API objects, unit-tested) -------------------------


def pod_is_ready(pod: client.V1Pod) -> bool:
    conditions = (pod.status and pod.status.conditions) or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def live_pods(pods: Iterable[client.V1Pod]) -> list[client.V1Pod]:
    """Pods not being deleted (old ReplicaSet pods during a rollout don't count)."""
    return [p for p in pods if p.metadata.deletion_timestamp is None]


def max_restarts(pods: Iterable[client.V1Pod]) -> int:
    counts = [
        cs.restart_count for p in pods for cs in ((p.status and p.status.container_statuses) or [])
    ]
    return max(counts, default=0)


def count_ready(pods: Iterable[client.V1Pod]) -> int:
    return sum(pod_is_ready(p) for p in live_pods(pods))


def count_running_not_ready(pods: Iterable[client.V1Pod]) -> int:
    return sum(
        p.status is not None and p.status.phase == "Running" and not pod_is_ready(p)
        for p in live_pods(pods)
    )


def event_matching(events: Iterable[client.CoreV1Event], needles: list[str]) -> str | None:
    for e in events:
        message = e.message or ""
        if all(n in message for n in needles):
            return message
    return None


def ready_endpoint_count(slices: Iterable[client.V1EndpointSlice]) -> int:
    return sum(
        1
        for s in slices
        for ep in (s.endpoints or [])
        if ep.conditions is not None and ep.conditions.ready is True
    )


def evaluate(condition: Condition, c: AdminClients, ns: str) -> tuple[bool, str]:
    """Check one ready_when condition. Returns (met, human-readable observation)."""
    match condition:
        case PodRestartsAtLeast(value=n):
            got = max_restarts(c.core.list_namespaced_pod(ns).items)
            return got >= n, f"max restarts {got} (want >= {n})"
        case PodsReady(value=n):
            got = count_ready(c.core.list_namespaced_pod(ns).items)
            return got >= n, f"{got} pods Ready (want >= {n})"
        case PodNotReady(value=n):
            got = count_running_not_ready(c.core.list_namespaced_pod(ns).items)
            return got >= n, f"{got} pods Running but not Ready (want >= {n})"
        case EventsContain(value=needles):
            hit = event_matching(c.core.list_namespaced_event(ns).items, needles)
            return hit is not None, f"event with {needles}: {'found' if hit else 'none yet'}"
        case EndpointsReady(service=svc, value=n):
            slices = c.discovery.list_namespaced_endpoint_slice(
                ns, label_selector=f"kubernetes.io/service-name={svc}"
            ).items
            got = ready_endpoint_count(slices)
            return got >= n, f"Service {svc}: {got} ready endpoints (want >= {n})"
    raise AssertionError(f"unhandled condition {condition!r}")


# --- up / down -------------------------------------------------------------------------


def up(
    scn: Scenario,
    c: AdminClients,
    *,
    ready_timeout: float,
    delete_timeout: float,
    poll: float,
    progress: Progress = _quiet,
) -> None:
    """Recreate the scenario from scratch and wait until its fault is live (ready_when)."""
    ns = scn.spec.namespace
    resources = [_resolve(c, doc) for doc in scn.manifests]  # fail before changing anything

    _create_fresh_namespace(
        scn,
        c,
        ready_timeout=ready_timeout,
        delete_timeout=delete_timeout,
        poll=poll,
        progress=progress,
    )
    for res, doc in zip(resources, scn.manifests, strict=True):
        body = {**doc, "metadata": {**doc["metadata"], "namespace": ns}}
        c.dynamic.server_side_apply(
            res, body=body, namespace=ns, field_manager=FIELD_MANAGER, force_conflicts=True
        )
        progress(f"applied {doc['kind']}/{doc['metadata']['name']}")

    progress(f"waiting for ready_when (timeout {ready_timeout:g}s)")
    deadline = time.monotonic() + ready_timeout
    while True:
        results = [evaluate(cond, c, ns) for cond in scn.spec.ready_when]
        if all(ok for ok, _ in results):
            for _, detail in results:
                progress(f"ready: {detail}")
            return
        if time.monotonic() >= deadline:
            unmet = "; ".join(d for ok, d in results if not ok)
            raise TimeoutError(f"{scn.name}: fault not live after {ready_timeout:g}s: {unmet}")
        time.sleep(poll)


def down(
    scn: Scenario, c: AdminClients, *, timeout: float, poll: float, progress: Progress = _quiet
) -> bool:
    """Delete the scenario namespace and wait until it's gone. False if it was already absent."""
    ns = _read_namespace(c, scn.spec.namespace)
    if ns is None:
        return False
    _require_managed(ns, scn)
    if ns.metadata.deletion_timestamp is None:
        c.core.delete_namespace(scn.spec.namespace)
        progress(f"deleting namespace {scn.spec.namespace}")
    _wait_gone(c, scn.spec.namespace, timeout=timeout, poll=poll)
    progress(f"namespace {scn.spec.namespace} deleted")
    return True


# --- helpers ---------------------------------------------------------------------------


def _resolve(c: AdminClients, doc: dict[str, Any]) -> Any:
    try:
        res = c.dynamic.resources.get(api_version=doc["apiVersion"], kind=doc["kind"])
    except Exception as e:
        raise ScenarioError(f"cluster has no {doc['apiVersion']} {doc['kind']}: {e}") from e
    if not res.namespaced:
        raise ScenarioError(
            f"{doc['kind']}/{doc['metadata']['name']} is cluster-scoped; not allowed"
        )
    return res


def _read_namespace(c: AdminClients, name: str) -> client.V1Namespace | None:
    try:
        return c.core.read_namespace(name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _require_managed(ns: client.V1Namespace, scn: Scenario) -> None:
    """Never touch a namespace these commands didn't create (e.g. a user's own scn-* one)."""
    labels = ns.metadata.labels or {}
    if labels.get(MANAGED_BY_LABEL) != FIELD_MANAGER or labels.get(SCENARIO_LABEL) != scn.name:
        raise ScenarioError(
            f"namespace {ns.metadata.name} exists but wasn't created by `agent scenario`; "
            "refusing to touch it"
        )


def _create_fresh_namespace(
    scn: Scenario,
    c: AdminClients,
    *,
    ready_timeout: float,
    delete_timeout: float,
    poll: float,
    progress: Progress,
) -> None:
    """Delete any earlier copy, so a re-run never inherits old state (hand-made fixes, restart
    counts), then create the namespace and wait for its `default` ServiceAccount: pods created
    before it exists are rejected, which leaves FailedCreate warnings the agent would see."""
    down(scn, c, timeout=delete_timeout, poll=poll, progress=progress)
    name = scn.spec.namespace
    labels = {MANAGED_BY_LABEL: FIELD_MANAGER, SCENARIO_LABEL: scn.name}
    c.core.create_namespace(
        client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels))
    )
    progress(f"created namespace {name}")
    deadline = time.monotonic() + ready_timeout
    while not _exists(lambda: c.core.read_namespaced_service_account("default", name)):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"namespace {name}: no default ServiceAccount after {ready_timeout:g}s"
            )
        time.sleep(poll)


def _exists(read: Callable[[], Any]) -> bool:
    try:
        read()
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _wait_gone(c: AdminClients, name: str, *, timeout: float, poll: float) -> None:
    deadline = time.monotonic() + timeout
    while _read_namespace(c, name) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"namespace {name} still exists after {timeout:g}s")
        time.sleep(poll)
