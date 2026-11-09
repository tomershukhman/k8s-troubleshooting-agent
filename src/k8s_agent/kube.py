"""The Kubernetes clients, and the helpers they share:
- the admin clients, from the user's own kubeconfig, for `setup`, `scenario` and `eval`
  (AGENT_KUBE_TIMEOUT_SECONDS per call);
- `Kube`, the remediator's, from its own kubeconfig only (AGENT_REMEDIATOR_TIMEOUT_SECONDS per
  call), and `token_expiry`, when an agent kubeconfig's token expires (checked before a run).
Every call has a time limit, so a stalled API server can't hang them."""

import base64
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from kubernetes.dynamic import DynamicClient
from urllib3.exceptions import HTTPError

from k8s_agent.config import Settings

Progress = Callable[[str], None]
FIELD_MANAGER = "k8s-agent"
# The context names local development clusters get (OrbStack, Docker Desktop, minikube, kind),
# as fnmatch patterns.
LOCAL_CONTEXTS = ("orbstack", "docker-desktop", "minikube", "kind-*")
SERVICE_NAME_LABEL = "kubernetes.io/service-name"  # on each of a Service's EndpointSlices
# What a call through the Kubernetes client can raise: the API server's error, or no answer at
# all (urllib3: a timeout, a refused or broken connection; only SSL errors become ApiException).
KUBE_ERRORS = (ApiException, HTTPError)


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


def unretried() -> client.Configuration:
    """A client configuration whose calls aren't retried, so each takes at most its time limit:
    by default urllib3 retries a failed call 3 times, and each try gets the whole limit."""
    conf = client.Configuration()
    conf.retries = 0
    return conf


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
    """The admin clients for AGENT_KUBE_CONTEXT, else the kubeconfig's current context, which
    must then be a local cluster: the admin commands create and delete namespaces, so a
    current context left on a shared cluster is refused, not used."""
    config_file = str(settings.kubeconfig) if settings.kubeconfig else None
    try:
        contexts, active = config.list_kube_config_contexts(config_file=config_file)
        name = settings.kube_context or active["name"]
        # The kubeconfig loader is unannotated, and pyright infers a context may be a list.
        known = {c["name"] for c in contexts}  # pyright: ignore[reportCallIssue, reportArgumentType]
        if name not in known:
            raise KubeError(f"kube context {name!r} not found in kubeconfig")
        if not settings.kube_context and not any(fnmatchcase(name, p) for p in LOCAL_CONTEXTS):
            raise KubeError(
                f"the current kube context {name!r} isn't a local cluster "
                f"({', '.join(LOCAL_CONTEXTS)}); to use it anyway, set AGENT_KUBE_CONTEXT={name}"
            )
        api = config.new_client_from_config(
            config_file=config_file, context=name, client_configuration=unretried()
        )
    except (ConfigException, yaml.YAMLError) as e:
        raise KubeError(f"cannot load kubeconfig: {e}") from e
    timeout = settings.kube_timeout_seconds
    try:
        # Runs API discovery, unless an earlier run cached it on disk (the client's own cache).
        dynamic = TimedDynamicClient(api, timeout)
    except Exception as e:
        raise KubeError(
            f"cannot reach the Kubernetes API (context {name!r}): {error_text(e)}"
        ) from e
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


def labels(selector: dict[str, str] | None) -> str:
    """A selector as `kubectl` writes it: `app=checkout,tier=web`."""
    return ",".join(f"{k}={v}" for k, v in sorted((selector or {}).items())) or "(none)"


def error_text(e: Exception) -> str:
    """One line for a Kubernetes API error: an ApiException's own text adds the HTTP headers."""
    if isinstance(e, ApiException):
        return f"{e.status} {e.reason}: {e.body}" if e.body else f"{e.status} {e.reason}"
    return str(e)


def read_or_none[T](read: Callable[[], T]) -> T | None:
    """`read()`, or None if the object doesn't exist."""
    try:
        return read()
    except ApiException as e:
        if e.status == 404:
            return None
        raise


# --- the remediator's client ------------------------------------------------------------


@dataclass(frozen=True)
class Kube:
    """The remediator's cluster access. Every call has a time limit."""

    core: client.CoreV1Api
    discovery: client.DiscoveryV1Api
    timeout: float

    @classmethod
    def load(cls, kubeconfig: Path, timeout: float) -> Kube:
        """From the remediator kubeconfig only: never $KUBECONFIG, ~/.kube/config or an
        in-cluster account, so a missing file is an error, not another identity."""
        try:
            api = config.new_client_from_config(
                config_file=str(kubeconfig), client_configuration=unretried()
            )
        except (ConfigException, yaml.YAMLError) as e:
            raise KubeError(
                f"cannot load the remediator kubeconfig {kubeconfig}: {e} (run `agent setup`)"
            ) from e
        return cls(client.CoreV1Api(api), client.DiscoveryV1Api(api), timeout)

    def close(self) -> None:
        """Close the connections of the client both APIs share."""
        self.core.api_client.close()

    def service(self, namespace: str, name: str) -> client.V1Service:
        return self.core.read_namespaced_service(name, namespace, _request_timeout=self.timeout)

    def pods(self, namespace: str, selector: dict[str, str] | None = None) -> list[client.V1Pod]:
        found = self.core.list_namespaced_pod(
            namespace,
            label_selector=labels(selector) if selector else None,  # None: every pod
            _request_timeout=self.timeout,
        )
        return [p for p in found.items if present(p.metadata).deletion_timestamp is None]

    def ready_pod_owners(self, namespace: str, selector: dict[str, str]) -> dict[str, str | None]:
        """The ready pods the exact `selector` matches (as a Service's: an empty one, none), by
        name, each with the controller that manages it, from its ownerReferences
        (`ReplicaSet/checkout-69db948c9f`); None when none does."""
        if not selector:
            return {}
        pods = [p for p in self.pods(namespace, selector) if pod_ready(p)]
        return dict(sorted((present(present(p.metadata).name), controller(p)) for p in pods))

    def ready_endpoints(self, namespace: str, service: str) -> int:
        """Ready endpoints across the Service's EndpointSlices, by the API's rules: an unset
        `ready` means ready, and one endpoint may be in several slices, so each counts once."""
        slices = self.discovery.list_namespaced_endpoint_slice(
            namespace,
            label_selector=f"{SERVICE_NAME_LABEL}={service}",
            _request_timeout=self.timeout,
        )
        return len(
            {
                e.target_ref.uid if e.target_ref and e.target_ref.uid else tuple(e.addresses)
                for s in slices.items
                for e in s.endpoints or []
                if e.conditions is None or e.conditions.ready is not False
            }
        )

    def replace_selector(
        self, namespace: str, name: str, selector: dict[str, str], version: str
    ) -> client.V1Service:
        """One JSON Patch, applied atomically by the API server: the test fails (422) if the
        Service is no longer at `version`, and then nothing is applied. `replace` sets the
        whole selector, so no old key survives (a merge patch would keep them)."""
        ops = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            {"op": "replace", "path": "/spec/selector", "value": selector},
        ]
        return self.core.patch_namespaced_service(
            name,
            namespace,
            ops,
            _content_type="application/json-patch+json",
            _request_timeout=self.timeout,
        )


def controller(pod: client.V1Pod) -> str | None:
    """The pod's managing controller, as `Kind/name`; None for a pod no controller manages."""
    refs = present(pod.metadata).owner_references or []
    ref = next((r for r in refs if r.controller), None)
    return f"{ref.kind}/{ref.name}" if ref else None


def token_expiry(kubeconfig: Path) -> datetime | None:
    """When the token in one of the agent's kubeconfigs expires (`agent setup` writes them, with
    tokens of AGENT_TOKEN_TTL_HOURS): the `exp` claim of the service account token, a JWT. None
    if the file or its token can't be read; using the kubeconfig then reports why."""
    try:
        token = yaml.safe_load(kubeconfig.read_text())["users"][0]["user"]["token"]
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return datetime.fromtimestamp(claims["exp"], UTC)
    except OSError, yaml.YAMLError, LookupError, TypeError, AttributeError, ValueError:
        return None
