"""The admin clients (kube.py): every API call has a time limit, AGENT_KUBE_TIMEOUT_SECONDS.
No cluster: a kubeconfig pointing nowhere, and the dynamic client's transport recorded."""

from types import SimpleNamespace

import yaml
from kubernetes.dynamic import DynamicClient

from k8s_agent import kube
from k8s_agent.config import get_settings

KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:9"}}],
    "users": [{"name": "u", "user": {"token": "t"}}],
    "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
    "current-context": "ctx",
}


def test_every_admin_call_has_the_configured_time_limit(env, tmp_path, monkeypatch):
    path = tmp_path / "kubeconfig"
    path.write_text(yaml.safe_dump(KUBECONFIG))
    env(kubeconfig=str(path), kube_timeout_seconds="4.5")
    sent = []

    def discover(self, _api, cache_file=None, discoverer=None):  # as discovery does, at once
        self.request("GET", "/apis")

    monkeypatch.setattr(DynamicClient, "__init__", discover)
    monkeypatch.setattr(DynamicClient, "request", lambda _s, method, path, body=None, **params:
                        sent.append((method, path, params.get("_request_timeout"))))  # fmt: skip
    c = kube.admin_clients(get_settings())
    resource = SimpleNamespace(namespaced=False, path=lambda name, namespace: f"/x/{name}")
    c.dynamic.server_side_apply(resource, body={"metadata": {"name": "n"}}, field_manager="m")
    assert sent == [("GET", "/apis", 4.5), ("patch", "/x/n", 4.5)]
    assert c.timeout == 4.5  # what the core calls pass (tests/unit/test_scenarios, test_access)
