"""The admin clients (kube.py): every API call has a time limit, AGENT_KUBE_TIMEOUT_SECONDS,
and only a local cluster is used unless AGENT_KUBE_CONTEXT names one. No cluster: a kubeconfig
pointing nowhere, and the dynamic client's transport recorded."""

import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from k8s_agent import kube
from k8s_agent.config import get_settings

KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:9"}}],
    "users": [{"name": "u", "user": {"token": "t"}}],
    "contexts": [
        {"name": name, "context": {"cluster": "c", "user": "u"}} for name in ("kind-dev", "prod")
    ],
}


@pytest.fixture
def sent(monkeypatch) -> list[tuple]:
    """The dynamic client's requests: (method, path, time limit). Discovery runs at once."""
    sent = []

    def discover(self, _api, cache_file=None, discoverer=None):  # as discovery does, at once
        self.request("GET", "/apis")

    monkeypatch.setattr(DynamicClient, "__init__", discover)
    monkeypatch.setattr(DynamicClient, "request", lambda _s, method, path, body=None, **params:
                        sent.append((method, path, params.get("_request_timeout"))))  # fmt: skip
    return sent


def kubeconfig(tmp_path, current: str) -> str:
    path = tmp_path / "kubeconfig"
    path.write_text(yaml.safe_dump({**KUBECONFIG, "current-context": current}))
    return str(path)


def test_every_admin_call_has_the_configured_time_limit(env, tmp_path, sent):
    env(kubeconfig=kubeconfig(tmp_path, "kind-dev"), kube_timeout_seconds="4.5")
    c = kube.admin_clients(get_settings())
    resource = SimpleNamespace(namespaced=False, path=lambda name, namespace: f"/x/{name}")
    c.dynamic.server_side_apply(resource, body={"metadata": {"name": "n"}}, field_manager="m")
    assert sent == [("GET", "/apis", 4.5), ("patch", "/x/n", 4.5)]
    assert c.timeout == 4.5  # what the core calls pass (tests/unit/test_scenarios, test_access)


def test_a_current_context_that_isnt_local_is_used_only_when_named(env, tmp_path, sent):
    env(kubeconfig=kubeconfig(tmp_path, "prod"))
    with pytest.raises(kube.KubeError, match=r"isn't a local cluster.*AGENT_KUBE_CONTEXT=prod"):
        kube.admin_clients(get_settings())
    assert sent == []  # refused before any request
    env(kube_context="prod")
    assert kube.admin_clients(get_settings()).context == "prod"


def test_an_api_error_reads_as_one_line_without_the_http_headers():
    e = ApiException(status=403, reason="Forbidden")
    e.headers, e.body = {"Audit-Id": "a1"}, '{"message": "services is forbidden"}'
    assert "Audit-Id" in str(e)  # the library's own text: several lines, headers included
    assert kube.error_text(e) == '403 Forbidden: {"message": "services is forbidden"}'


@pytest.fixture
def stalled(tmp_path):
    """A kubeconfig (context kind-dev) for an API server that accepts connections and never
    answers (the kernel completes them; nothing reads), and how many it got so far."""
    server = socket.create_server(("127.0.0.1", 0), backlog=10)
    server.setblocking(False)
    accepted = []

    def connections() -> int:
        while True:
            try:
                accepted.append(server.accept()[0])
            except BlockingIOError:
                return len(accepted)

    cluster = {"server": f"http://127.0.0.1:{server.getsockname()[1]}"}
    config = {**KUBECONFIG, "clusters": [{"name": "c", "cluster": cluster}]}
    path = tmp_path / "stalled.kubeconfig"
    path.write_text(yaml.safe_dump({**config, "current-context": "kind-dev"}))
    yield str(path), connections
    for conn in accepted:
        conn.close()
    server.close()


def test_a_call_to_a_stalled_api_server_is_tried_once_within_its_time_limit(env, stalled):
    # urllib3 would retry it 3 times by default, each try with the whole time limit
    path, connections = stalled
    env(kubeconfig=path, kube_timeout_seconds="0.3")
    t0 = time.monotonic()
    with pytest.raises(kube.KubeError, match="cannot reach the Kubernetes API"):
        kube.admin_clients(get_settings())  # its first call: API discovery
    assert time.monotonic() - t0 < 1 and connections() == 1
    remediator = kube.Kube.load(Path(path), timeout=0.3)
    with pytest.raises(kube.KUBE_ERRORS):
        remediator.service("shop-checkout", "checkout")
    assert time.monotonic() - t0 < 2 and connections() == 2
