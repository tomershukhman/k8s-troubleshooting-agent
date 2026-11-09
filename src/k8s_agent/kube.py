"""Kubernetes clients built from the user's own (admin) kubeconfig, and the helpers `setup`,
`scenario` and the remediator share. Every admin API call has a time limit
(AGENT_KUBE_TIMEOUT_SECONDS), so a stalled API server can't hang them."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from kubernetes.dynamic import DynamicClient

from k8s_agent.config import Settings

Progress = Callable[[str], None]
FIELD_MANAGER = "k8s-agent"


class KubeError(Exception):
    """Cluster access failed in a way the user can fix (bad kubeconfig, unreachable API)."""


@dataclass(frozen=True)
class AdminClients:
    """Calls through `core` pass `_request_timeout=timeout`, as the remediator's do; `dynamic`
    adds it to every request itself."""

    api: client.ApiClient
    core: client.CoreV1Api
    dynamic: DynamicClient
    context: str
    timeout: float


class TimedDynamicClient(DynamicClient):
    """A DynamicClient whose every request has a time limit: its API discovery too, which takes
    no per-call arguments (server_side_apply passes its own on to `request`)."""

    def __init__(self, api: client.ApiClient, timeout: float) -> None:
        self.timeout = timeout  # set first: the constructor runs discovery
        super().__init__(api)

    def request(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        params.setdefault("_request_timeout", self.timeout)
        return super().request(method, path, body=body, **params)


def admin_clients(settings: Settings) -> AdminClients:
    config_file = str(settings.kubeconfig) if settings.kubeconfig else None
    try:
        contexts, active = config.list_kube_config_contexts(config_file=config_file)
        name = settings.kube_context or active["name"]
        # The kubeconfig loader is unannotated, and pyright infers a context may be a list.
        known = {c["name"] for c in contexts}  # pyright: ignore[reportCallIssue, reportArgumentType]
        if name not in known:
            raise KubeError(f"kube context {name!r} not found in kubeconfig")
        api = config.new_client_from_config(config_file=config_file, context=name)
    except ConfigException as e:
        raise KubeError(f"cannot load kubeconfig: {e}") from e
    timeout = settings.kube_timeout_seconds
    try:
        # Runs API discovery, so it also proves the API is reachable.
        dynamic = TimedDynamicClient(api, timeout)
    except Exception as e:
        raise KubeError(f"cannot reach the Kubernetes API (context {name!r}): {e}") from e
    core = client.CoreV1Api(api)
    return AdminClients(api=api, core=core, dynamic=dynamic, context=name, timeout=timeout)


def load_yaml(*paths: Path) -> list[dict[str, Any]]:
    """Every document in the given YAML files, in order."""
    return [d for p in paths for d in yaml.safe_load_all(p.read_text()) if d]


def apply(c: AdminClients, docs: Iterable[dict[str, Any]], progress: Progress) -> None:
    """Server-side apply, so re-running converges instead of failing on existing objects."""
    for doc in docs:
        res = c.dynamic.resources.get(api_version=doc["apiVersion"], kind=doc["kind"])
        meta = doc["metadata"]
        c.dynamic.server_side_apply(
            res,
            body=doc,
            namespace=meta.get("namespace"),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )
        where = f" in {meta['namespace']}" if "namespace" in meta else ""
        progress(f"applied {doc['kind']}/{meta['name']}{where}")


def present[T](value: T | None) -> T:
    """A field the API server always sets, such as an object's metadata, spec, status or name:
    the client types every field Optional."""
    if value is None:
        raise ValueError("the API server's answer lacks a field it always sets")
    return value


def pod_ready(pod: client.V1Pod) -> bool:
    conditions = present(pod.status).conditions
    return any(c.type == "Ready" and c.status == "True" for c in conditions or [])


def read_or_none[T](read: Callable[[], T]) -> T | None:
    """`read()`, or None if the object doesn't exist."""
    try:
        return read()
    except ApiException as e:
        if e.status == 404:
            return None
        raise
