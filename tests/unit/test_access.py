"""`agent setup` (SPEC 3.5): what it applies and the kubeconfigs it writes. Fake API."""

import base64
import stat

import yaml

from k8s_agent import access
from k8s_agent.config import get_settings
from tests.fakes import FakeKube


def setup(kube: FakeKube, tmp_path, monkeypatch):
    monkeypatch.setattr(
        access, "whoami", lambda kc: f"system:serviceaccount:{kc['users'][0]['user']['token']}"
    )
    paths = {access.READER: tmp_path / "r.kubeconfig", access.REMEDIATOR: tmp_path / "m.kubeconfig"}
    creds = access.setup(
        kube.clients, rbac_file=get_settings().rbac_file, token_ttl_seconds=3600,
        kubeconfigs=paths, progress=print,
    )  # fmt: skip
    return creds, paths


def grants(kube: FakeKube) -> dict[str, dict[tuple[str, str], set[str]]]:
    """{ClusterRole: {(apiGroup, resource): verbs}} for the roles setup applied."""
    out: dict = {}
    for role in (d for d in kube.applied if d["kind"] == "ClusterRole"):
        for rule in role["rules"]:
            for group in rule["apiGroups"]:
                for res in rule["resources"]:
                    out.setdefault(role["metadata"]["name"], {}).setdefault(
                        (group, res), set()
                    ).update(rule["verbs"])
    return out


def test_setup_applies_the_identities_then_writes_a_kubeconfig_each(tmp_path, monkeypatch):
    kube = FakeKube()
    creds, paths = setup(kube, tmp_path, monkeypatch)
    assert kube.log[:2] == ["apply Namespace/k8s-agent", "apply ServiceAccount/reader"]
    assert kube.log[-2:] == ["token reader ttl=3600", "token remediator ttl=3600"]
    assert [c.username for c in creds] == [
        "system:serviceaccount:tok-reader",
        "system:serviceaccount:tok-remediator",
    ]
    for sa, path in paths.items():
        kubeconfig = yaml.safe_load(path.read_text())
        # only the token: never the admin's credentials, whatever the admin kubeconfig holds
        assert kubeconfig["users"] == [{"name": f"k8s-agent-{sa}", "user": {"token": f"tok-{sa}"}}]
        assert kubeconfig["clusters"][0]["cluster"] == {"server": "https://127.0.0.1:6443"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_roles_setup_applies_are_the_boundary(tmp_path, monkeypatch):
    kube = FakeKube()
    setup(kube, tmp_path, monkeypatch)
    roles = grants(kube)
    # the reader's per-namespace access is Kubernetes' built-in `view` (scenario RoleBindings)
    assert set(roles) == {"k8s-agent-list-namespaces", "k8s-agent-remediate"}
    assert roles["k8s-agent-list-namespaces"] == {("", "namespaces"): {"list"}}
    assert roles["k8s-agent-remediate"] == {
        ("", "services"): {"get", "patch"},
        ("", "pods"): {"get", "list"},
        ("discovery.k8s.io", "endpointslices"): {"get", "list"},
    }
    for granted in roles.values():
        for (group, res), verbs in granted.items():
            assert "secrets" not in res and "*" not in {group, res, *verbs}
            assert res.split("/")[-1] not in {"exec", "attach", "portforward", "proxy"}
    [crb] = [d for d in kube.applied if d["kind"] == "ClusterRoleBinding"]
    assert crb["roleRef"]["name"] == "k8s-agent-list-namespaces"  # the only cluster-wide grant


def test_a_ca_file_is_embedded(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"PEM")
    conf = FakeKube().clients.api.configuration
    conf.ssl_ca_cert = str(ca)
    cluster = access.kubeconfig_for(conf, "reader", "t")["clusters"][0]["cluster"]
    assert cluster["certificate-authority-data"] == base64.b64encode(b"PEM").decode()


def test_credentials_are_rewritten_in_place_and_kept_private(tmp_path):
    # k8s-mcp bind-mounts the single file: a new inode would leave it on the old token
    path = tmp_path / "creds" / "reader.kubeconfig"
    access.write_private(path, "old")
    inode = path.stat().st_ino
    path.chmod(0o644)
    access.write_private(path, "new")
    assert path.read_text() == "new" and path.stat().st_ino == inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
