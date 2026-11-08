"""Kubernetes clients built from the user's own (admin) kubeconfig."""

from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from kubernetes.dynamic import DynamicClient

from k8s_agent.config import Settings


class KubeError(Exception):
    """Cluster access failed in a way the user can fix (bad kubeconfig, unreachable API)."""


@dataclass(frozen=True)
class AdminClients:
    api: client.ApiClient
    core: client.CoreV1Api
    discovery: client.DiscoveryV1Api
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
    return AdminClients(
        api=api,
        core=client.CoreV1Api(api),
        discovery=client.DiscoveryV1Api(api),
        dynamic=dynamic,
        context=name,
    )
