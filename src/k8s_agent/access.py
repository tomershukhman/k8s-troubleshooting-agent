"""The agent's cluster identities (SPEC §3.5): two ServiceAccounts, their roles, per-namespace
grants, and short-lived kubeconfigs. Uses the user's admin kubeconfig; the agent never does.

The RBAC rules here are the safety boundary, not tunables: changing them changes what the
agent can do. Which namespaces are granted is configuration (AGENT_NAMESPACES,
AGENT_REMEDIATION_NAMESPACES)."""

import base64
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from k8s_agent.kube import AdminClients
from k8s_agent.policy import AGENT_NAMESPACE, FORBIDDEN_NAMESPACES

READER = "reader"
REMEDIATOR = "remediator"

READ_ROLE = "k8s-agent-read"
LIST_NAMESPACES_ROLE = "k8s-agent-list-namespaces"
REMEDIATE_ROLE = "k8s-agent-remediate"

FIELD_MANAGER = "k8s-agent-setup"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
LABELS = {MANAGED_BY_LABEL: FIELD_MANAGER}

READ_VERBS = ["get", "list", "watch"]

# reader, bound per namespace. No Secrets, no writes, no exec/attach/port-forward.
READ_RULES: list[dict[str, Any]] = [
    {
        "apiGroups": [""],
        "resources": ["pods", "pods/log", "events", "services", "endpoints", "configmaps"],
        "verbs": READ_VERBS,
    },
    {"apiGroups": ["events.k8s.io"], "resources": ["events"], "verbs": READ_VERBS},
    {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": READ_VERBS},
    {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": READ_VERBS},
]
# reader, bound cluster-wide: find where to look, nothing else.
LIST_NAMESPACES_RULES: list[dict[str, Any]] = [
    {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list"]}
]
# remediator, bound only in AGENT_REMEDIATION_NAMESPACES. Used only by the fix tool (M6): what
# fixing a selector and verifying it need, nothing more (no delete, no watch, no Secrets).
REMEDIATE_RULES: list[dict[str, Any]] = [
    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "patch"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list"]},
]

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


# --- objects (pure, unit-tested) -------------------------------------------------------


def _meta(name: str, namespace: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "labels": dict(LABELS)}
    if namespace is not None:
        meta["namespace"] = namespace
    return meta


def _subject(service_account: str) -> dict[str, str]:
    return {"kind": "ServiceAccount", "name": service_account, "namespace": AGENT_NAMESPACE}


def _cluster_role(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": _meta(name),
        "rules": rules,
    }


def _role_ref(cluster_role: str) -> dict[str, str]:
    return {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role}


def identity_objects() -> list[dict[str, Any]]:
    """The long-lived pieces `agent setup` creates, in apply order."""
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(AGENT_NAMESPACE)},
        *(
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(sa, AGENT_NAMESPACE)}
            for sa in (READER, REMEDIATOR)
        ),
        _cluster_role(READ_ROLE, READ_RULES),
        _cluster_role(LIST_NAMESPACES_ROLE, LIST_NAMESPACES_RULES),
        _cluster_role(REMEDIATE_ROLE, REMEDIATE_RULES),
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": _meta(f"{LIST_NAMESPACES_ROLE}-{READER}"),
            "roleRef": _role_ref(LIST_NAMESPACES_ROLE),
            "subjects": [_subject(READER)],
        },
    ]


def namespace_grants(
    namespace: str, *, read_namespaces: Iterable[str], remediation_namespaces: Iterable[str]
) -> list[dict[str, Any]]:
    """RoleBindings for one namespace: reader if it's in AGENT_NAMESPACES, remediator if it's
    in AGENT_REMEDIATION_NAMESPACES. Empty if neither."""
    if namespace in FORBIDDEN_NAMESPACES:
        raise ValueError(f"namespace {namespace!r} may never be granted to the agent")
    wanted = [
        (READER, READ_ROLE, namespace in set(read_namespaces)),
        (REMEDIATOR, REMEDIATE_ROLE, namespace in set(remediation_namespaces)),
    ]
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": _meta(f"k8s-agent-{sa}", namespace),
            "roleRef": _role_ref(role),
            "subjects": [_subject(sa)],
        }
        for sa, role, on in wanted
        if on
    ]


@dataclass(frozen=True)
class ClusterEndpoint:
    """How to reach the API server, copied from the admin kubeconfig (never its credentials)."""

    server: str
    ca_data: str | None  # base64 PEM; None = system CAs
    insecure: bool = False
    tls_server_name: str | None = None
    proxy_url: str | None = None


def endpoint_from(configuration: client.Configuration) -> ClusterEndpoint:
    """The admin client's server address and CA. A CA given as a file path is embedded, so the
    kubeconfig works where that path doesn't exist (e.g. inside the MCP container)."""
    ca_data = None
    if configuration.ssl_ca_cert:
        ca_data = base64.b64encode(Path(configuration.ssl_ca_cert).read_bytes()).decode()
    return ClusterEndpoint(
        server=configuration.host,
        ca_data=ca_data,
        insecure=not configuration.verify_ssl,
        tls_server_name=configuration.tls_server_name or None,
        proxy_url=configuration.proxy or None,
    )


def kubeconfig_for(endpoint: ClusterEndpoint, service_account: str, token: str) -> dict[str, Any]:
    name = f"{AGENT_NAMESPACE}-{service_account}"
    cluster: dict[str, Any] = {"server": endpoint.server}
    if endpoint.ca_data:
        cluster["certificate-authority-data"] = endpoint.ca_data
    if endpoint.insecure:
        cluster["insecure-skip-tls-verify"] = True
    if endpoint.tls_server_name:
        cluster["tls-server-name"] = endpoint.tls_server_name
    if endpoint.proxy_url:
        cluster["proxy-url"] = endpoint.proxy_url
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": AGENT_NAMESPACE, "cluster": cluster}],
        "users": [{"name": name, "user": {"token": token}}],
        "contexts": [{"name": name, "context": {"cluster": AGENT_NAMESPACE, "user": name}}],
        "current-context": name,
    }


def write_private(path: Path, text: str) -> None:
    """Write a credential file, mode 0600, in place: a bind-mounted single file keeps its
    inode, so replacing the file (write + rename) would leave a running container on the old
    token."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o600)


# --- cluster side ----------------------------------------------------------------------


def apply(c: AdminClients, objects: Iterable[dict[str, Any]], progress: Progress = _quiet) -> None:
    for obj in objects:
        res = c.dynamic.resources.get(api_version=obj["apiVersion"], kind=obj["kind"])
        meta = obj["metadata"]
        c.dynamic.server_side_apply(
            res,
            body=obj,
            namespace=meta.get("namespace"),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )
        where = f" in {meta['namespace']}" if "namespace" in meta else ""
        progress(f"applied {obj['kind']}/{meta['name']}{where}")


def request_token(c: AdminClients, service_account: str, ttl_seconds: int) -> tuple[str, datetime]:
    """A short-lived token from the TokenRequest API. The API server may shorten the lifetime
    (--service-account-max-token-expiration), so the real expiry is returned."""
    body = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=ttl_seconds)
    )
    resp = c.core.create_namespaced_service_account_token(service_account, AGENT_NAMESPACE, body)
    return resp.status.token, resp.status.expiration_timestamp


def whoami(kubeconfig: dict[str, Any]) -> str:
    """The username the API server sees for a kubeconfig (proves the token and CA work)."""
    api = config.new_client_from_config_dict(kubeconfig)
    review = client.AuthenticationV1Api(api).create_self_subject_review(
        client.V1SelfSubjectReview()
    )
    return review.status.user_info.username


def prune_grants(c: AdminClients, keep: set[tuple[str, str]], progress: Progress = _quiet) -> None:
    """Delete the agent's RoleBindings except `keep` ({(namespace, name)}), so a namespace
    taken out of AGENT_NAMESPACES / AGENT_REMEDIATION_NAMESPACES loses its access."""
    res = c.dynamic.resources.get(api_version="rbac.authorization.k8s.io/v1", kind="RoleBinding")
    for rb in res.get(label_selector=f"{MANAGED_BY_LABEL}={FIELD_MANAGER}").items:
        ns, name = rb.metadata.namespace, rb.metadata.name
        if (ns, name) not in keep:
            res.delete(name=name, namespace=ns)
            progress(f"removed RoleBinding/{name} in {ns} (namespace no longer configured)")


def _namespace_exists(c: AdminClients, name: str) -> bool:
    try:
        ns = c.core.read_namespace(name)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    return ns.metadata.deletion_timestamp is None


@dataclass(frozen=True)
class Credential:
    service_account: str
    username: str
    path: Path
    expires: datetime


def setup(
    c: AdminClients,
    *,
    read_namespaces: list[str],
    remediation_namespaces: list[str],
    token_ttl_seconds: int,
    reader_kubeconfig: Path,
    remediator_kubeconfig: Path,
    progress: Progress = _quiet,
) -> list[Credential]:
    """Create or update the identities, grant every configured namespace that exists now
    (`scenario up` grants the rest), remove grants from namespaces no longer configured, and
    write a fresh kubeconfig for each identity. Idempotent: re-running with the same settings
    only refreshes the tokens."""
    apply(c, identity_objects(), progress)

    keep: set[tuple[str, str]] = set()
    for ns in sorted(set(read_namespaces) | set(remediation_namespaces)):
        grants = namespace_grants(
            ns, read_namespaces=read_namespaces, remediation_namespaces=remediation_namespaces
        )
        keep |= {(ns, g["metadata"]["name"]) for g in grants}
        if _namespace_exists(c, ns):
            apply(c, grants, progress)
        else:
            progress(f"namespace {ns} doesn't exist yet; `agent scenario up` will grant it")
    prune_grants(c, keep, progress)

    endpoint = endpoint_from(c.api.configuration)
    creds = []
    for sa, path in ((READER, reader_kubeconfig), (REMEDIATOR, remediator_kubeconfig)):
        token, expires = request_token(c, sa, token_ttl_seconds)
        kubeconfig = kubeconfig_for(endpoint, sa, token)
        username = whoami(kubeconfig)
        write_private(path, yaml.safe_dump(kubeconfig, sort_keys=False))
        progress(f"wrote {path} ({username}, expires {expires.isoformat()})")
        creds.append(Credential(sa, username, path, expires))
    return creds
