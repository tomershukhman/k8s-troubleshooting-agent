"""The agent's identities and grants (SPEC §3.5), on plain objects. No cluster."""

import base64
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes import client as k

from k8s_agent import access
from k8s_agent.policy import FORBIDDEN_NAMESPACES

SCENARIO_NAMESPACES = ["shop-orders", "shop-checkout", "shop-catalog", "shop-inventory"]


def _rules_by_resource(rules):
    """{(apiGroup, resource): verbs} for every resource a rule set grants."""
    out = {}
    for rule in rules:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                out.setdefault((group, resource), set()).update(rule["verbs"])
    return out


def built_roles(objects) -> dict[str, dict]:
    """{ClusterRole name: {(apiGroup, resource): verbs}} for the roles in `objects`."""
    return {
        o["metadata"]["name"]: _rules_by_resource(o["rules"])
        for o in objects
        if o["kind"] == "ClusterRole"
    }


ROLES = built_roles(access.identity_objects())  # what `agent setup` applies


# --- the RBAC rules are the boundary (checked on the roles setup builds) ---------------


def test_read_role_is_exactly_the_spec_resources_read_only():
    granted = ROLES["k8s-agent-read"]
    assert set(granted) == {
        ("", "pods"),
        ("", "pods/log"),
        ("", "events"),
        ("", "services"),
        ("", "endpoints"),
        ("", "configmaps"),
        ("events.k8s.io", "events"),
        ("discovery.k8s.io", "endpointslices"),
        ("apps", "deployments"),
        ("apps", "replicasets"),
    }
    assert all(verbs == {"get", "list", "watch"} for verbs in granted.values())


def test_exactly_three_roles():
    assert set(ROLES) == {"k8s-agent-read", "k8s-agent-list-namespaces", "k8s-agent-remediate"}


def test_no_rule_grants_secrets_wildcards_or_exec():
    for granted in ROLES.values():
        for (group, resource), verbs in granted.items():
            assert "secrets" not in resource
            assert "*" not in (group, resource) and "*" not in verbs
            assert resource.split("/")[-1] not in {"exec", "attach", "portforward", "proxy"}


def test_list_namespaces_role_only_lists_namespaces():
    assert ROLES["k8s-agent-list-namespaces"] == {("", "namespaces"): {"list"}}


def test_remediate_role_is_exactly_fix_and_verify():
    assert ROLES["k8s-agent-remediate"] == {
        ("", "services"): {"get", "patch"},
        ("", "pods"): {"get", "list"},
        ("discovery.k8s.io", "endpointslices"): {"get", "list"},
    }


def test_identity_objects():
    objs = {(o["kind"], o["metadata"]["name"]): o for o in access.identity_objects()}
    assert set(objs) == {
        ("Namespace", "k8s-agent"),
        ("ServiceAccount", "reader"),
        ("ServiceAccount", "remediator"),
        ("ClusterRole", "k8s-agent-read"),
        ("ClusterRole", "k8s-agent-list-namespaces"),
        ("ClusterRole", "k8s-agent-remediate"),
        ("ClusterRoleBinding", "k8s-agent-list-namespaces-reader"),
    }
    assert all(o["metadata"]["labels"] == access.LABELS for o in objs.values())
    # The only cluster-wide grant is reader's `list namespaces`; reading and patching are
    # always per namespace.
    crb = objs[("ClusterRoleBinding", "k8s-agent-list-namespaces-reader")]
    assert crb["roleRef"]["name"] == "k8s-agent-list-namespaces"
    assert crb["subjects"] == [
        {"kind": "ServiceAccount", "name": "reader", "namespace": "k8s-agent"}
    ]


# --- per-namespace grants --------------------------------------------------------------


def _grants(ns):
    return {
        (b["metadata"]["name"], b["roleRef"]["name"], b["subjects"][0]["name"])
        for b in access.namespace_grants(
            ns, read_namespaces=SCENARIO_NAMESPACES, remediation_namespaces=["shop-checkout"]
        )
    }


def test_selector_grants_reader_and_remediator():
    assert _grants("shop-checkout") == {
        ("k8s-agent-reader", "k8s-agent-read", "reader"),
        ("k8s-agent-remediator", "k8s-agent-remediate", "remediator"),
    }


@pytest.mark.parametrize("ns", ["shop-orders", "shop-catalog", "shop-inventory"])
def test_other_scenarios_grant_reader_only(ns):
    assert _grants(ns) == {("k8s-agent-reader", "k8s-agent-read", "reader")}


def test_unlisted_namespace_gets_nothing():
    assert _grants("default") == set()


def test_grants_are_role_bindings_in_that_namespace():
    for b in access.namespace_grants(
        "shop-checkout", read_namespaces=["shop-checkout"], remediation_namespaces=["shop-checkout"]
    ):
        assert b["kind"] == "RoleBinding"
        assert b["metadata"]["namespace"] == "shop-checkout"
        assert b["roleRef"]["kind"] == "ClusterRole"
        assert b["subjects"][0]["namespace"] == "k8s-agent"


@pytest.mark.parametrize("ns", sorted(FORBIDDEN_NAMESPACES))
def test_system_namespaces_are_never_granted(ns):
    with pytest.raises(ValueError, match="may never be granted"):
        access.namespace_grants(ns, read_namespaces=[ns], remediation_namespaces=[ns])


# --- kubeconfigs -----------------------------------------------------------------------


def test_endpoint_embeds_a_ca_file(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n")
    cfg = k.Configuration(host="https://127.0.0.1:26443")
    cfg.ssl_ca_cert = str(ca)
    ep = access.endpoint_from(cfg)
    assert ep.server == "https://127.0.0.1:26443"
    assert base64.b64decode(ep.ca_data) == ca.read_bytes()
    assert ep.insecure is False


def test_endpoint_without_ca_uses_system_cas():
    cfg = k.Configuration(host="https://k8s.example.com")
    ep = access.endpoint_from(cfg)
    assert ep.ca_data is None


def test_kubeconfig_carries_only_the_token():
    ep = access.ClusterEndpoint(server="https://127.0.0.1:26443", ca_data="Q0E=")
    kc = access.kubeconfig_for(ep, "reader", "tok")
    assert kc["current-context"] == "k8s-agent-reader"
    assert kc["clusters"] == [
        {
            "name": "k8s-agent",
            "cluster": {"server": "https://127.0.0.1:26443", "certificate-authority-data": "Q0E="},
        }
    ]
    assert kc["users"] == [{"name": "k8s-agent-reader", "user": {"token": "tok"}}]


def test_kubeconfig_keeps_tls_options():
    ep = access.ClusterEndpoint(
        server="https://h", ca_data=None, insecure=True, tls_server_name="api", proxy_url="http://p"
    )
    cluster = access.kubeconfig_for(ep, "remediator", "t")["clusters"][0]["cluster"]
    assert cluster == {
        "server": "https://h",
        "insecure-skip-tls-verify": True,
        "tls-server-name": "api",
        "proxy-url": "http://p",
    }


def test_credentials_are_private_and_rewritten_in_place(tmp_path):
    path = tmp_path / ".kube-agent" / "reader.kubeconfig"
    access.write_private(path, "first")
    inode = path.stat().st_ino
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    access.write_private(path, "second, longer")
    assert path.read_text() == "second, longer"
    assert path.stat().st_ino == inode  # same file: a running bind mount sees the new token


def test_loosened_permissions_are_tightened(tmp_path):
    path = tmp_path / "reader.kubeconfig"
    path.write_text("x")
    path.chmod(0o644)
    access.write_private(path, "y")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- setup, against a fake API ---------------------------------------------------------


class FakeResource:
    """A dynamic-client resource. RoleBindings can also be listed by label and deleted."""

    def __init__(self, kind: str, existing: list[tuple[str, str]]):
        self.kind = kind
        self.existing = existing
        self.deleted: list[tuple[str, str]] = []

    def get(self, *, label_selector):
        assert label_selector == f"{access.MANAGED_BY_LABEL}={access.FIELD_MANAGER}"
        meta = [SimpleNamespace(namespace=ns, name=name) for ns, name in self.existing]
        return SimpleNamespace(items=[SimpleNamespace(metadata=m) for m in meta])

    def delete(self, *, name, namespace):
        self.deleted.append((namespace, name))


class FakeDynamic:
    def __init__(self, role_bindings: list[tuple[str, str]] = ()):
        self.applied: list[tuple[str, str, str | None]] = []
        self.bodies: list[dict] = []
        self.role_bindings = FakeResource("RoleBinding", list(role_bindings))
        self.resources = SimpleNamespace(get=self._resource)

    def _resource(self, *, api_version, kind):
        return self.role_bindings if kind == "RoleBinding" else FakeResource(kind, [])

    def server_side_apply(self, res, *, body, namespace, field_manager, force_conflicts):
        assert field_manager == access.FIELD_MANAGER and force_conflicts
        self.applied.append((res.kind, body["metadata"]["name"], namespace))
        self.bodies.append(body)


class FakeCore:
    def __init__(self, existing: set[str]):
        self.existing = existing
        self.tokens: list[tuple[str, int]] = []

    def read_namespace(self, name):
        if name not in self.existing:
            raise k.ApiException(status=404)
        return k.V1Namespace(metadata=k.V1ObjectMeta(name=name))

    def create_namespaced_service_account_token(self, name, namespace, body):
        assert namespace == "k8s-agent"
        self.tokens.append((name, body.spec.expiration_seconds))
        return SimpleNamespace(
            status=SimpleNamespace(token=f"tok-{name}", expiration_timestamp=datetime.now(UTC))
        )


def run_setup(tmp_path, monkeypatch, *, existing_ns, role_bindings=(), read=SCENARIO_NAMESPACES):
    monkeypatch.setattr(access, "whoami", lambda kc: kc["users"][0]["name"])
    cfg = k.Configuration(host="https://127.0.0.1:26443")
    core, dynamic = FakeCore(existing_ns), FakeDynamic(role_bindings)
    c = SimpleNamespace(api=SimpleNamespace(configuration=cfg), core=core, dynamic=dynamic)
    creds = access.setup(
        c,
        read_namespaces=read,
        remediation_namespaces=["shop-checkout"],
        token_ttl_seconds=3600,
        reader_kubeconfig=tmp_path / "reader.kubeconfig",
        remediator_kubeconfig=tmp_path / "remediator.kubeconfig",
    )
    return creds, core, dynamic


def test_setup_grants_existing_namespaces_and_writes_both_kubeconfigs(tmp_path, monkeypatch):
    creds, core, dynamic = run_setup(tmp_path, monkeypatch, existing_ns={"shop-checkout"})

    bindings = [(name, ns) for kind, name, ns in dynamic.applied if kind == "RoleBinding"]
    assert bindings == [
        ("k8s-agent-reader", "shop-checkout"),
        ("k8s-agent-remediator", "shop-checkout"),
    ]  # missing namespaces are left to `scenario up`
    # what reached the cluster: each binding points at the right role, with the right rules
    applied_roles = built_roles(dynamic.bodies)
    role_of = {
        (b["metadata"]["name"], b["subjects"][0]["name"]): b["roleRef"]["name"]
        for b in dynamic.bodies
        if b["kind"] in ("RoleBinding", "ClusterRoleBinding")
    }
    assert role_of == {
        ("k8s-agent-reader", "reader"): "k8s-agent-read",
        ("k8s-agent-remediator", "remediator"): "k8s-agent-remediate",
        ("k8s-agent-list-namespaces-reader", "reader"): "k8s-agent-list-namespaces",
    }
    assert applied_roles == ROLES
    assert core.tokens == [("reader", 3600), ("remediator", 3600)]
    assert [c.service_account for c in creds] == ["reader", "remediator"]
    assert "tok-reader" in (tmp_path / "reader.kubeconfig").read_text()
    assert "tok-remediator" in (tmp_path / "remediator.kubeconfig").read_text()


def test_setup_removes_grants_from_namespaces_no_longer_configured(tmp_path, monkeypatch):
    existing = [
        ("shop-checkout", "k8s-agent-reader"),  # still configured: kept
        ("shop-checkout", "k8s-agent-remediator"),
        ("shop-orders", "k8s-agent-reader"),  # dropped from AGENT_NAMESPACES
        ("default", "k8s-agent-reader"),  # configured once, long ago
    ]
    _, _, dynamic = run_setup(
        tmp_path,
        monkeypatch,
        existing_ns={"shop-checkout", "shop-orders", "default"},
        role_bindings=existing,
        read=["shop-checkout"],
    )
    assert dynamic.role_bindings.deleted == [
        ("shop-orders", "k8s-agent-reader"),
        ("default", "k8s-agent-reader"),
    ]
