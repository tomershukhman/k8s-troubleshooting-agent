"""Kubernetes clients built from the user's own (admin) kubeconfig, and the helpers `setup` and
`scenario` share."""

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
    api: client.ApiClient
    core: client.CoreV1Api
    dynamic: DynamicClient
    context: str


def admin_clients(settings: Settings) -> AdminClients:
    config_file = str(settings.kubeconfig) if settings.kubeconfig else None
    try:
        contexts, active = config.list_kube_config_contexts(config_file=config_file)
        name = settings.kube_context or active["name"]
        if name not in {c["name"] for c in contexts}:
            raise KubeError(f"kube context {name!r} not found in kubeconfig")
        api = config.new_client_from_config(config_file=config_file, context=name)
    except ConfigException as e:
        raise KubeError(f"cannot load kubeconfig: {e}") from e
    try:
        dynamic = DynamicClient(api)  # runs API discovery, so it also proves the API is reachable
    except Exception as e:
        raise KubeError(f"cannot reach the Kubernetes API (context {name!r}): {e}") from e
    return AdminClients(api=api, core=client.CoreV1Api(api), dynamic=dynamic, context=name)


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


def read_or_none[T](read: Callable[[], T]) -> T | None:
    """`read()`, or None if the object doesn't exist."""
    try:
        return read()
    except ApiException as e:
        if e.status == 404:
            return None
        raise
