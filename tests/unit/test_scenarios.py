"""The scenario folders and `scenario up|down` (SPEC 2.2, 4.4), with a fake API. No cluster."""

import pytest

from k8s_agent import scenarios
from k8s_agent.config import get_settings
from k8s_agent.scenarios import OWNER_LABEL, OWNER_VALUE, ReadyWhen, live_state
from tests.fakes import FakeKube, pod


def load():
    return scenarios.load_all(get_settings().scenarios_dir)


def test_every_scenario_loads_with_its_own_neutral_namespace():
    found = load()
    assert sorted(found) == ["crashloop", "healthy", "readiness", "selector"]
    namespaces = [scn.namespace for scn in found.values()]
    assert len(set(namespaces)) == 4 and all(ns.startswith("shop-") for ns in namespaces)
    for name, scn in found.items():
        # nothing the agent can see gives away the folder name, which describes the fault: as
        # M1 checks on the cluster, each name, and each label and annotation as key=value
        for doc in scn.manifests:
            pods = doc.get("spec", {}).get("template", {}).get("metadata", {})  # its pods'
            visible = [doc["metadata"]["name"]] + [
                f"{k}={v}"
                for meta in (doc["metadata"], pods)
                for field in ("labels", "annotations")
                for k, v in meta.get(field, {}).items()
            ]
            hints = [text for text in visible if name in text.lower()]
            assert hints == [], (name, doc["kind"], hints)
        [ns] = [d for d in scn.manifests if d["kind"] == "Namespace"]
        assert ns["metadata"]["labels"] == {OWNER_LABEL: OWNER_VALUE}


def test_every_image_is_pinned_by_digest():
    for scn in load().values():
        for doc in scn.manifests:
            for c in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                assert "@sha256:" in c["image"], (scn.name, c["image"])


def test_live_state_counts_live_pods_only():
    pods = [pod(), pod(ready=False, restarts=3), pod(deleting=True)]
    assert live_state(pods) == ReadyWhen(pods_ready=1, pods_not_ready=1, restarts=3)


def up(kube: FakeKube, name: str = "selector", ready_timeout: float = 1) -> None:
    scn = scenarios.get_scenario(get_settings().scenarios_dir, name)
    scenarios.up(scn, kube.clients, ready_timeout=ready_timeout, delete_timeout=1, poll=0.01,
                 progress=print)  # fmt: skip


def test_up_recreates_the_namespace_then_its_access_then_the_app():
    kube = FakeKube(pods=[pod(), pod()])
    kube.namespaces["shop-checkout"] = {OWNER_LABEL: OWNER_VALUE}  # an earlier run
    up(kube)
    assert kube.log == [
        "delete Namespace/shop-checkout",
        "apply Namespace/shop-checkout",
        "wait ServiceAccount/default",  # pods created before it would be rejected
        "apply RoleBinding/k8s-agent-reader",
        "apply RoleBinding/k8s-agent-remediator",
        "apply Deployment/checkout",
        "apply Service/checkout",
    ]
    assert kube.timeouts and set(kube.timeouts) == {FakeKube.TIMEOUT}  # every call is limited


def test_up_waits_until_the_fault_is_live():
    with pytest.raises(TimeoutError, match="fault not live"):
        up(FakeKube(pods=[pod()]), "crashloop", ready_timeout=0.05)  # no restarts yet
    up(FakeKube(pods=[pod(ready=False, restarts=2)]), "crashloop")


def test_up_and_down_refuse_a_namespace_that_is_not_ours():
    kube = FakeKube()
    kube.namespaces["shop-checkout"] = {"team": "payments"}
    with pytest.raises(scenarios.ScenarioError, match="refusing to touch it"):
        up(kube)
    assert kube.log == []


def test_down_on_an_absent_namespace_is_a_no_op():
    scn = scenarios.get_scenario(get_settings().scenarios_dir, "selector")
    kube = FakeKube()
    assert not scenarios.down(scn, kube.clients, timeout=1, poll=0.01, progress=print)
    assert kube.log == []
