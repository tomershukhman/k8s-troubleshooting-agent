"""The scenario folders and `scenario up|down` (DESIGN 3.7), with a fake API. No cluster."""

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
        # nothing the agent can see gives away the folder name, which describes the fault:
        # each name, and each label and annotation as key=value
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


@pytest.fixture(autouse=True)
def quick(env):
    env(
        scenario_ready_timeout_seconds="1",
        scenario_delete_timeout_seconds="1",
        scenario_poll_seconds="0.01",
    )


def up(kube: FakeKube, name: str = "selector") -> None:
    s = get_settings()
    scenarios.up(scenarios.get_scenario(s.scenarios_dir, name), kube.clients, s, print)


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


def test_up_waits_until_the_fault_is_live(env):
    env(scenario_ready_timeout_seconds="0.05")
    with pytest.raises(TimeoutError, match="fault not live"):
        up(FakeKube(pods=[pod()]), "crashloop")  # no restarts yet
    up(FakeKube(pods=[pod(ready=False, restarts=2)]), "crashloop")


def test_a_namespace_that_is_not_ours_is_refused_and_left_untouched():
    kube = FakeKube()
    kube.namespaces["shop-checkout"] = {"team": "payments"}
    with pytest.raises(scenarios.ScenarioError, match="refusing to touch it"):
        up(kube)
    assert kube.log == []


def test_down_on_an_absent_namespace_is_a_no_op():
    s, kube = get_settings(), FakeKube()
    assert not scenarios.down(
        scenarios.get_scenario(s.scenarios_dir, "selector"), kube.clients, s, print
    )
    assert kube.log == []
