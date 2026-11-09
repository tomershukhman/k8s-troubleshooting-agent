"""The agent's identities (DESIGN 3.5). `deploy/rbac.yaml` defines them; `setup` applies it and
writes a short-lived kubeconfig per identity. Each scenario folder holds its namespace's
RoleBindings. Uses the user's admin kubeconfig; the agent never does."""

import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from kubernetes import client, config

from k8s_agent.kube import AdminClients, Progress, apply, load_yaml, present, unretried
from k8s_agent.policy import AGENT_NAMESPACE

READER = "reader"
REMEDIATOR = "remediator"
# Where k8s-mcp's compose mount reads the reader's kubeconfig: not a setting, nothing else uses it.
READER_KUBECONFIG = Path(".kube-agent/reader.kubeconfig")


class Credential(NamedTuple):
    username: str
    expires: datetime


def kubeconfig_for(conf: client.Configuration, service_account: str, token: str) -> dict:
    """A kubeconfig with the admin client's server and CA (never its credentials). A CA given
    as a file path is embedded, so it works where the path doesn't exist (the MCP container)."""
    name = f"{AGENT_NAMESPACE}-{service_account}"
    cluster: dict[str, Any] = {"server": conf.host}
    if conf.ssl_ca_cert:
        ca = base64.b64encode(Path(conf.ssl_ca_cert).read_bytes()).decode()
        cluster["certificate-authority-data"] = ca
    if not conf.verify_ssl:
        cluster["insecure-skip-tls-verify"] = True
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": AGENT_NAMESPACE, "cluster": cluster}],
        "users": [{"name": name, "user": {"token": token}}],
        "contexts": [{"name": name, "context": {"cluster": AGENT_NAMESPACE, "user": name}}],
        "current-context": name,
    }


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read (mode 0600), in place: a bind-mounted credential
    file keeps its inode, so replacing the file (write + rename) would leave a running
    container on the old token."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # the mode above applies only to a new file: private before writing
    with os.fdopen(fd, "w") as f:
        f.write(text)


def request_token(c: AdminClients, service_account: str, ttl_seconds: int) -> tuple[str, datetime]:
    """A short-lived token from the TokenRequest API. The API server may shorten the lifetime
    (--service-account-max-token-expiration), so the real expiry is returned."""
    body = client.AuthenticationV1TokenRequest(
        spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=ttl_seconds)
    )
    resp = c.core.create_namespaced_service_account_token(
        service_account, AGENT_NAMESPACE, body, _request_timeout=c.timeout
    )
    status = present(resp.status)
    return present(status.token), present(status.expiration_timestamp)


def whoami(kubeconfig: dict, timeout: float) -> str:
    """The username the API server sees for a kubeconfig (proves the token and CA work)."""
    api = config.new_client_from_config_dict(kubeconfig, client_configuration=unretried())
    with api:  # closes its connections
        review = client.AuthenticationV1Api(api).create_self_subject_review(
            client.V1SelfSubjectReview(), _request_timeout=timeout
        )
    user = present(present(review.status).user_info)
    return present(user.username)


def setup(
    c: AdminClients,
    *,
    rbac_file: Path,
    token_ttl_seconds: int,
    kubeconfigs: dict[str, Path],
    progress: Progress,
) -> list[Credential]:
    """Apply the identities, then write a fresh kubeconfig for each ({service account: path}).
    Idempotent: re-running only refreshes the tokens."""
    apply(c, load_yaml(rbac_file), progress)
    creds = []
    for sa, path in kubeconfigs.items():
        token, expires = request_token(c, sa, token_ttl_seconds)
        kubeconfig = kubeconfig_for(c.api.configuration, sa, token)
        username = whoami(kubeconfig, c.timeout)
        write_private(path, yaml.safe_dump(kubeconfig, sort_keys=False))
        progress(f"wrote {path} ({username}, expires {expires.isoformat()})")
        creds.append(Credential(username, expires))
    return creds
