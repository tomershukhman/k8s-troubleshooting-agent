"""`scenario up` grants the agent access on every run (SPEC §3.5). Fake API, no cluster."""

from pathlib import Path
from types import SimpleNamespace

from kubernetes import client as k

from k8s_agent import access
from k8s_agent.scenarios import cluster
from k8s_agent.scenarios.spec import get_scenario

SCN = get_scenario(Path(__file__).resolve().parents[2] / "scenarios", "selector")


def ready_pod() -> k.V1Pod:
    return k.V1Pod(
        metadata=k.V1ObjectMeta(name="checkout-1"),
        status=k.V1PodStatus(
            phase="Running", conditions=[k.V1PodCondition(type="Ready", status="True")]
        ),
    )


class Recorder:
    """Core + dynamic API fakes that log every change, in order."""

    def __init__(self):
        self.log: list[str] = []
        self.namespace_exists = False
        self.resources = SimpleNamespace(get=self._resource)

    def _resource(self, *, api_version, kind):
        return SimpleNamespace(kind=kind, namespaced=True)

    # core
    def read_namespace(self, name):
        if not self.namespace_exists:
            raise k.ApiException(status=404)
        labels = {cluster.OWNER_LABEL: cluster.OWNER_VALUE}
        return k.V1Namespace(metadata=k.V1ObjectMeta(name=name, labels=labels))

    def delete_namespace(self, name):
        self.log.append(f"delete Namespace/{name}")
        self.namespace_exists = False

    def create_namespace(self, body):
        self.log.append(f"create Namespace/{body.metadata.name}")
        self.namespace_labels = body.metadata.labels
        self.namespace_exists = True

    def read_namespaced_service_account(self, name, namespace):
        return object()

    def list_namespaced_pod(self, namespace):
        return SimpleNamespace(items=[ready_pod(), ready_pod()])

    # dynamic
    def server_side_apply(self, res, *, body, namespace, field_manager, force_conflicts):
        assert namespace == "shop-checkout"
        self.log.append(f"apply {body['kind']}/{body['metadata']['name']}")


def run_up(rec: Recorder) -> None:
    grants = access.namespace_grants(
        "shop-checkout", read_namespaces=["shop-checkout"], remediation_namespaces=["shop-checkout"]
    )
    c = SimpleNamespace(core=rec, dynamic=rec)
    cluster.up(SCN, c, ready_timeout=1, delete_timeout=1, poll=0.01, grants=grants)


def test_grants_applied_right_after_the_namespace_and_before_the_app():
    rec = Recorder()
    run_up(rec)
    assert rec.log == [
        "create Namespace/shop-checkout",
        "apply RoleBinding/k8s-agent-reader",
        "apply RoleBinding/k8s-agent-remediator",
        "apply Deployment/checkout",
        "apply Service/checkout",
    ]


def test_rerun_recreates_the_grants():
    rec = Recorder()
    run_up(rec)
    rec.log.clear()
    run_up(rec)  # deletes the namespace (and with it the old bindings), then grants again
    assert rec.log[:4] == [
        "delete Namespace/shop-checkout",
        "create Namespace/shop-checkout",
        "apply RoleBinding/k8s-agent-reader",
        "apply RoleBinding/k8s-agent-remediator",
    ]


def test_namespace_gets_only_the_neutral_ownership_label():
    rec = Recorder()
    run_up(rec)
    assert rec.namespace_labels == {"app.kubernetes.io/part-of": "k8s-agent-demo"}
