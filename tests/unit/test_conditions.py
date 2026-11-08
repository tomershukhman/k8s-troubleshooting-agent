"""ready_when condition logic, on hand-built API objects. No cluster."""

from datetime import UTC, datetime

from kubernetes import client as k

from k8s_agent.scenarios.cluster import (
    count_ready,
    count_running_not_ready,
    event_matching,
    max_restarts,
    ready_endpoint_count,
)


def pod(*, phase="Running", ready=True, restarts=0, deleting=False) -> k.V1Pod:
    return k.V1Pod(
        metadata=k.V1ObjectMeta(
            name="p", deletion_timestamp=datetime.now(UTC) if deleting else None
        ),
        status=k.V1PodStatus(
            phase=phase,
            conditions=[k.V1PodCondition(type="Ready", status="True" if ready else "False")],
            container_statuses=[
                k.V1ContainerStatus(
                    name="c", image="i", image_id="", ready=ready, restart_count=restarts
                )
            ],
        ),
    )


def test_ready_counts_skip_terminating_pods():
    pods = [pod(), pod(), pod(deleting=True), pod(ready=False)]
    assert count_ready(pods) == 2


def test_running_not_ready_ignores_pending_and_ready():
    pods = [pod(ready=False), pod(phase="Pending", ready=False), pod()]
    assert count_running_not_ready(pods) == 1


def test_max_restarts():
    assert max_restarts([pod(restarts=1), pod(restarts=3)]) == 3
    assert max_restarts([]) == 0
    assert max_restarts([k.V1Pod(metadata=k.V1ObjectMeta(name="p"), status=None)]) == 0


def test_event_matching_needs_all_needles_in_one_event():
    events = [
        k.CoreV1Event(involved_object=k.V1ObjectReference(), metadata=k.V1ObjectMeta(), message=m)
        for m in ["Readiness probe failed: timeout", "HTTP 404", None]
    ]
    assert event_matching(events, ["Readiness probe failed", "404"]) is None
    hit = "Readiness probe failed: HTTP probe failed with statuscode: 404"
    events.append(
        k.CoreV1Event(involved_object=k.V1ObjectReference(), metadata=k.V1ObjectMeta(), message=hit)
    )
    assert event_matching(events, ["Readiness probe failed", "404"]) == hit


def test_ready_endpoint_count():
    def ep(ready):
        return k.V1Endpoint(addresses=["10.0.0.1"], conditions=k.V1EndpointConditions(ready=ready))

    slices = [
        k.V1EndpointSlice(address_type="IPv4", endpoints=[ep(True), ep(False), ep(None)]),
        k.V1EndpointSlice(address_type="IPv4", endpoints=None),  # selector matches nothing
        k.V1EndpointSlice(address_type="IPv4", endpoints=[ep(True)]),
    ]
    assert ready_endpoint_count(slices) == 2
