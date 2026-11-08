"""`up`/`down` never touch a namespace they didn't create. Fake API, no cluster."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes import client as k

from k8s_agent.scenarios import cluster
from k8s_agent.scenarios.spec import ScenarioError, get_scenario

SCN = get_scenario(Path(__file__).resolve().parents[2] / "scenarios", "selector")


class FakeCore:
    def __init__(self, labels: dict[str, str] | None):
        self.labels = labels
        self.deleted: list[str] = []
        self.created: list[str] = []

    def read_namespace(self, name):
        if self.labels is None:
            raise k.ApiException(status=404)
        return k.V1Namespace(metadata=k.V1ObjectMeta(name=name, labels=self.labels))

    def delete_namespace(self, name):
        self.deleted.append(name)
        self.labels = None  # gone at once

    def create_namespace(self, body):
        self.created.append(body.metadata.name)


def fake_clients(core: FakeCore):
    resource = SimpleNamespace(namespaced=True)
    dynamic = SimpleNamespace(resources=SimpleNamespace(get=lambda **_: resource))
    return SimpleNamespace(core=core, dynamic=dynamic)


OWNED = {cluster.MANAGED_BY_LABEL: cluster.FIELD_MANAGER, cluster.SCENARIO_LABEL: "selector"}
FOREIGN = [
    {},  # a user's own scn-selector
    {cluster.MANAGED_BY_LABEL: "helm"},
    {cluster.MANAGED_BY_LABEL: cluster.FIELD_MANAGER, cluster.SCENARIO_LABEL: "healthy"},
]


@pytest.mark.parametrize("labels", FOREIGN)
def test_down_refuses_foreign_namespace(labels):
    core = FakeCore(labels)
    with pytest.raises(ScenarioError, match="refusing to touch it"):
        cluster.down(SCN, fake_clients(core), timeout=1, poll=0.01)
    assert core.deleted == []


@pytest.mark.parametrize("labels", FOREIGN)
def test_up_refuses_foreign_namespace(labels):
    core = FakeCore(labels)
    with pytest.raises(ScenarioError, match="refusing to touch it"):
        cluster.up(SCN, fake_clients(core), ready_timeout=1, delete_timeout=1, poll=0.01)
    assert core.deleted == [] and core.created == []


def test_down_deletes_owned_namespace():
    core = FakeCore(OWNED)
    assert cluster.down(SCN, fake_clients(core), timeout=1, poll=0.01) is True
    assert core.deleted == ["scn-selector"]


def test_down_on_absent_namespace_is_a_no_op():
    core = FakeCore(None)
    assert cluster.down(SCN, fake_clients(core), timeout=1, poll=0.01) is False
    assert core.deleted == []
