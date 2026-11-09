"""`agent setup` (SPEC 3.5): what it applies and the kubeconfigs it writes. Fake API."""

import base64
import os
import stat
from types import SimpleNamespace

import yaml
from kubernetes import client

from k8s_agent import access
from k8s_agent.config import get_settings
from tests.fakes import FakeKube


def setup(kube: FakeKube, tmp_path, monkeypatch):
    def whoami(kc, timeout):
        kube.timeouts.append(timeout)
        return f"system:serviceaccount:{kc['users'][0]['user']['token']}"

    monkeypatch.setattr(access, "whoami", whoami)
    paths = {access.READER: tmp_path / "r.kubeconfig", access.REMEDIATOR: tmp_path / "m.kubeconfig"}
    creds = access.setup(
        kube.clients, rbac_file=get_settings().rbac_file, token_ttl_seconds=3600,
        kubeconfigs=paths, progress=print,
    )  # fmt: skip
    return creds, paths


def grants(kube: FakeKube) -> dict[str, dict[tuple[str, str, tuple[str, ...]], set[str]]]:
    """{ClusterRole: {(apiGroup, resource, resourceNames): verbs}} for the roles setup applied
    (no resourceNames: every object of the type)."""
    out: dict = {}
    for role in (d for d in kube.applied if d["kind"] == "ClusterRole"):
        for rule in role["rules"]:
            names = tuple(rule.get("resourceNames", []))
            for group in rule["apiGroups"]:
                for res in rule["resources"]:
                    out.setdefault(role["metadata"]["name"], {}).setdefault(
                        (group, res, names), set()
                    ).update(rule["verbs"])
    return out


ADMIN_CERT, ADMIN_KEY, ADMIN_TOKEN = "/admin/client.crt", "/admin/client.key", "admin-token"


def test_setup_applies_the_identities_then_writes_a_kubeconfig_each(tmp_path, monkeypatch):
    kube = FakeKube()
    admin = kube.clients.api.configuration  # the admin's client: a cert, a key and a token
    admin.cert_file, admin.key_file = ADMIN_CERT, ADMIN_KEY
    admin.api_key = {"authorization": f"Bearer {ADMIN_TOKEN}"}
    creds, paths = setup(kube, tmp_path, monkeypatch)
    assert kube.log[:2] == ["apply Namespace/k8s-agent", "apply ServiceAccount/reader"]
    assert kube.log[-2:] == ["token reader ttl=3600", "token remediator ttl=3600"]
    assert kube.timeouts == [FakeKube.TIMEOUT] * 4  # each token request and whoami
    assert [c.username for c in creds] == [
        "system:serviceaccount:tok-reader",
        "system:serviceaccount:tok-remediator",
    ]
    for sa, path in paths.items():
        text = path.read_text()
        kubeconfig = yaml.safe_load(text)
        # only the token: never the admin's credentials, whatever the admin kubeconfig holds
        assert kubeconfig["users"] == [{"name": f"k8s-agent-{sa}", "user": {"token": f"tok-{sa}"}}]
        assert not [c for c in (ADMIN_CERT, ADMIN_KEY, ADMIN_TOKEN) if c in text]
        assert kubeconfig["clusters"][0]["cluster"] == {"server": "https://127.0.0.1:6443"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_roles_setup_applies_are_the_boundary(tmp_path, monkeypatch):
    kube = FakeKube()
    setup(kube, tmp_path, monkeypatch)
    roles = grants(kube)
    # the reader's per-namespace access is Kubernetes' built-in `view` (scenario RoleBindings)
    assert set(roles) == {"k8s-agent-list-namespaces", "k8s-agent-remediate"}
    assert roles["k8s-agent-list-namespaces"] == {("", "namespaces", ()): {"list"}}
    assert roles["k8s-agent-remediate"] == {
        ("", "services", ("checkout",)): {"get", "patch"},  # that one Service only
        ("", "pods", ()): {"list"},
        ("discovery.k8s.io", "endpointslices", ()): {"list"},
    }
    for granted in roles.values():
        for (group, res, _), verbs in granted.items():
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


def test_credentials_are_rewritten_in_place_and_made_private_before_writing(tmp_path, monkeypatch):
    # k8s-mcp bind-mounts the single file: a new inode would leave it on the old token
    path = tmp_path / "creds" / "reader.kubeconfig"
    access.write_private(path, "old")
    inode = path.stat().st_ino
    path.chmod(0o644)
    modes, fdopen = [], os.fdopen

    def opened(fd, *args, **kwargs):  # the mode when the new text is about to be written
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", opened)
    access.write_private(path, "new token")
    assert path.read_text() == "new token" and path.stat().st_ino == inode
    assert modes == [0o600] and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_whoami_closes_its_client(monkeypatch):
    closed = []
    monkeypatch.setattr(client.ApiClient, "close", lambda api: closed.append(api))
    review = SimpleNamespace(status=SimpleNamespace(user_info=SimpleNamespace(username="u")))
    timeouts = []

    def review_self(_body, _request_timeout):
        timeouts.append(_request_timeout)
        return review

    api = SimpleNamespace(create_self_subject_review=review_self)
    monkeypatch.setattr(access.client, "AuthenticationV1Api", lambda _client: api)
    kubeconfig = access.kubeconfig_for(FakeKube().clients.api.configuration, "reader", "t")
    assert access.whoami(kubeconfig, 5) == "u" and len(closed) == 1
    assert timeouts == [5]  # a time limit on its call too
