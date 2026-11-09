"""The identities and the MCP server hold the safety boundary. Checked two ways: through the MCP
server, with the client the agent uses, and directly at Kubernetes, with each identity's own
kubeconfig."""

import asyncio
import base64
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastmcp import Client as FastMCPClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from langchain_core._api import suppress_langchain_beta_warning

from k8s_agent import access
from k8s_agent.config import get_settings
from k8s_agent.policy import AGENT_NAMESPACE, READ_ONLY_TOOLS

from .conftest import (
    FOLDERS,
    SECRET,
    SECRET_VALUE,
    agent,
    compose,
    crash_log,
    crash_pod,
    eventually,
    mcp_call,
    namespace,
    reusable,
)

with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

pytestmark = pytest.mark.usefixtures("cluster")
NS = [namespace(f) for f in FOLDERS]
ORDERS, CHECKOUT = "shop-orders", "shop-checkout"


def obj(kind: str, namespace: str | None = None, name: str | None = None, api: str = "v1"):
    """Arguments for resources_get / resources_list."""
    args = {"apiVersion": api, "kind": kind, "namespace": namespace, "name": name}
    return {k: v for k, v in args.items() if v}


def identity(path: Path) -> client.ApiClient:
    return config.new_client_from_config(config_file=str(path))


# Per test, from the current file: `agent setup` rotates the tokens.
@pytest.fixture
def reader() -> client.ApiClient:
    return identity(access.READER_KUBECONFIG)


@pytest.fixture
def remediator() -> client.ApiClient:
    return identity(get_settings().remediator_kubeconfig)


# --- through the MCP server ------------------------------------------------------------


def test_mcp_offers_exactly_the_seven_read_tools():
    async def go():
        s = get_settings()
        async with MCPAdapter(FastMCPClient(s.mcp_endpoint, timeout=s.mcp_timeout_seconds)) as a:
            tools = {t.name: set(t.args) for t in await a.list_tools()}
            # tools that change or run things don't exist on the server
            for name in (
                "pods_exec",
                "pods_run",
                "pods_delete",
                "resources_create_or_update",
                "resources_delete",
            ):
                with pytest.raises(Exception, match=f'unknown tool "{name}"'):
                    await a.client.call_tool(name, {"name": "x", "namespace": ORDERS})
            return tools

    tools = asyncio.run(go())
    assert set(tools) == READ_ONLY_TOOLS
    assert {"apiVersion", "kind", "name", "namespace"} <= tools["resources_get"]
    assert {"name", "namespace", "previous", "tail"} <= tools["pods_log"]


@pytest.mark.parametrize(
    ("tool", "args", "expect"),
    [
        ("pods_list_in_namespace", {"namespace": ORDERS}, "orders-"),
        ("events_list", {"namespace": ORDERS}, "BackOff"),
        ("resources_get", obj("Service", CHECKOUT, "checkout"), "check-out"),
        ("resources_get", obj("ConfigMap", ORDERS, "orders-config"), "DATABSE_URL"),
        ("resources_list", obj("Deployment", CHECKOUT, api="apps/v1"), "checkout"),
    ],
)
def test_mcp_read_tools_work_as_the_reader(tool, args, expect):
    err, text = mcp_call(tool, args)
    assert not err and expect in text, text


def test_mcp_reads_the_crash_log(cluster):
    pod = crash_pod(cluster)
    logs = crash_log(
        lambda previous: mcp_call(
            "pods_log", {"name": pod, "namespace": ORDERS, "previous": previous}
        )[1]
    )
    assert any("DATABASE_URL not set" in log for log in logs), logs


def test_mcp_namespace_list_gives_nothing_away():
    err, text = mcp_call("namespaces_list", {})
    assert not err and all(ns in text for ns in NS)
    assert [f for f in FOLDERS if f in text.lower()] == []


SECRET_CALLS = [
    ("resources_get", obj("Secret", ORDERS, SECRET)),
    ("resources_list", obj("Secret", ORDERS)),
    ("resources_list", obj("Secret")),
]


def test_mcp_refuses_secrets_and_never_shows_the_value(cluster):
    outputs = []
    for tool, args in SECRET_CALLS:
        err, text = mcp_call(tool, args)
        assert err and "resource not allowed" in text, text
        outputs.append(text)
    outputs += [
        mcp_call("events_list", {"namespace": ORDERS})[1],
        mcp_call("pods_get", {"name": crash_pod(cluster), "namespace": ORDERS})[1],
        mcp_call("resources_list", {"apiVersion": "v1", "kind": "ConfigMap", "namespace": ORDERS})[
            1
        ],
    ]
    encoded = base64.b64encode(SECRET_VALUE.encode()).decode()
    assert not any(SECRET_VALUE in out or encoded in out for out in outputs)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pods_list_in_namespace", {"namespace": "kube-system"}),
        ("resources_get", obj("ConfigMap", "kube-system", "kube-root-ca.crt")),
        ("events_list", {}),  # all namespaces: never cluster-wide
        ("resources_list", obj("ConfigMap")),
    ],
)
def test_mcp_refuses_outside_the_scenario_namespaces(tool, args):
    err, text = mcp_call(tool, args)
    assert err and "forbidden" in text and '"system:serviceaccount:k8s-agent:reader"' in text, text


# --- the running k8s-mcp container ---------------------------------------------------


def inspect_mcp() -> dict:
    cid = compose("ps", "-q", "k8s-mcp").strip()
    out = subprocess.run(["docker", "inspect", cid], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)[0]


def test_mcp_container_is_pinned_hardened_and_holds_only_the_reader_credentials():
    c = inspect_mcp()
    assert c["Config"]["Image"].startswith("quay.io/containers/kubernetes_mcp_server:v")
    assert "@sha256:" in c["Config"]["Image"]
    assert c["HostConfig"]["NetworkMode"] == "host"
    assert c["HostConfig"]["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in c["HostConfig"]["SecurityOpt"]
    configured = yaml.safe_load(compose("config", "--format", "json"))["services"]["k8s-mcp"]
    assert c["Config"]["User"] == configured["user"] and not c["Config"]["User"].startswith("0:")
    assert c["State"]["Health"]["Status"] == "healthy"
    mounts = {m["Destination"]: m for m in c["Mounts"]}
    assert set(mounts) == {"/etc/k8s-mcp/config.toml", "/kube/reader.kubeconfig"}
    assert all(m["RW"] is False for m in mounts.values())
    assert (
        Path(mounts["/kube/reader.kubeconfig"]["Source"]).resolve()
        == access.READER_KUBECONFIG.resolve()
    )


def test_mcp_listens_on_loopback_only():
    # Host networking shares the machine's network: a bind to all interfaces would expose the
    # unauthenticated server. Read the listening sockets from inside its network.
    port = f"{get_settings().mcp_port:04X}"
    listening = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in compose("exec", "-T", "k8s-mcp", "cat", table).splitlines()[1:]:
            local, state = row.split()[1], row.split()[3]
            if state == "0A" and local.endswith(f":{port}"):  # 0A = LISTEN
                listening.append(local)
    assert listening == ["0100007F:" + port], listening  # 127.0.0.1 only, no IPv6


# --- directly at Kubernetes, each identity with its own kubeconfig ---------------------

READ, WRITE = ["get", "list", "watch"], ["create", "update", "patch", "delete", "deletecollection"]
READABLE = [  # (group, resource, subresource) the reader reads in scenario namespaces (`view`)
    ("", "pods", None),
    ("", "pods", "log"),
    ("", "events", None),
    ("events.k8s.io", "events", None),
    ("", "services", None),
    ("", "endpoints", None),
    ("discovery.k8s.io", "endpointslices", None),
    ("apps", "deployments", None),
    ("apps", "replicasets", None),
    ("apps", "statefulsets", None),
    ("apps", "daemonsets", None),
    ("batch", "jobs", None),
    ("", "configmaps", None),
    ("", "serviceaccounts", None),
    ("", "persistentvolumeclaims", None),
    ("", "resourcequotas", None),
    ("", "limitranges", None),
    ("networking.k8s.io", "networkpolicies", None),
]
OTHER = [  # never: credentials, a shell, who-may-do-what
    ("", "secrets", None),
    ("", "pods", "exec"),
    ("rbac.authorization.k8s.io", "roles", None),
    ("rbac.authorization.k8s.io", "rolebindings", None),
]
REMEDIATE = {  # (verb, group, resource) the remediator may do in shop-checkout (DESIGN 3.5)
    ("get", "", "services"),
    ("patch", "", "services"),
    ("list", "", "pods"),
    ("list", "discovery.k8s.io", "endpointslices"),
}
SERVICE = "checkout"  # the only Service it may get or patch (resourceNames)


def can(api: client.ApiClient, verb, group, resource, sub=None, ns=None, name=None) -> bool:
    attrs = client.V1ResourceAttributes(
        verb=verb, group=group, resource=resource, subresource=sub, namespace=ns, name=name
    )
    review = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(resource_attributes=attrs)
    )
    return client.AuthorizationV1Api(api).create_self_subject_access_review(review).status.allowed


def assert_reader_boundary(reader: client.ApiClient, namespaces: list[str]) -> None:
    for ns in namespaces:
        for group, res, sub in READABLE:
            for verb in ["get"] if sub == "log" else READ:
                assert can(reader, verb, group, res, sub, ns), (verb, res, sub, ns)
            for verb in WRITE:
                assert not can(reader, verb, group, res, sub, ns), (verb, res, sub, ns)
        for group, res, sub in OTHER:
            assert not any(can(reader, v, group, res, sub, ns) for v in READ + WRITE), (
                res,
                sub,
                ns,
            )


def assert_remediator_boundary(remediator: client.ApiClient) -> None:
    for ns in NS:
        for group, res, sub in READABLE + OTHER:
            name = SERVICE if res == "services" else None
            for verb in READ + WRITE:
                want = ns == CHECKOUT and sub is None and (verb, group, res) in REMEDIATE
                allowed = can(remediator, verb, group, res, sub, ns, name)
                assert allowed is want, (verb, res, sub, ns)
    for verb in ("get", "patch"):  # no other Service, not even in shop-checkout
        assert not can(remediator, verb, "", "services", ns=CHECKOUT, name="orders")
        assert not can(remediator, verb, "", "services", ns=CHECKOUT)


def test_reader_reads_every_scenario_namespace_and_changes_nothing(reader):
    assert_reader_boundary(reader, NS)
    assert can(reader, "list", "", "namespaces")  # cluster-wide, to find where to look
    for ns in ["kube-system", "default", AGENT_NAMESPACE, None]:
        for group, res, sub in READABLE:
            assert not can(reader, "list" if sub is None else "get", group, res, sub, ns), (res, ns)


def test_remediator_can_only_fix_and_verify_in_checkout(remediator, cluster):
    assert_remediator_boundary(remediator)
    for ns in ["default", "kube-system", AGENT_NAMESPACE]:
        assert not any(can(remediator, v, g, r, ns=ns, name=SERVICE) for v, g, r in REMEDIATE), ns
    assert not can(remediator, "list", "", "namespaces")
    # the one fix it exists for, as a dry run: allowed, and nothing changed
    core = client.CoreV1Api(remediator)
    patch = {"spec": {"selector": {"app": "checkout"}}}
    assert core.patch_namespaced_service(
        "checkout", CHECKOUT, patch, dry_run="All"
    ).spec.selector == {"app": "checkout"}
    assert cluster.core.read_namespaced_service("checkout", CHECKOUT).spec.selector == {
        "app": "check-out"
    }


# --- robustness ------------------------------------------------------------------------


def mcp_reads_the_checkout_service():
    err, text = mcp_call("resources_get", obj("Service", CHECKOUT, "checkout"))
    assert not err and "check-out" in text, text


def test_setup_again_is_harmless():
    agent("setup")  # again, with the scenarios and k8s-mcp up
    s = get_settings()
    assert_reader_boundary(identity(access.READER_KUBECONFIG), NS)
    assert_remediator_boundary(identity(s.remediator_kubeconfig))
    eventually(mcp_reads_the_checkout_service)


def test_mcp_picks_up_a_rotated_token_without_restart(cluster, request):
    """Re-running `agent setup` refreshes the running server's token (README claim)."""
    request.addfinalizer(lambda: agent("setup"))  # recreates the reader if this test fails
    old = yaml.safe_load(access.READER_KUBECONFIG.read_text())

    def state():
        c = inspect_mcp()
        return c["State"]["StartedAt"], c["RestartCount"]

    before = state()
    # Recreating the ServiceAccount revokes every token issued to the old one.
    cluster.core.delete_namespaced_service_account(access.READER, AGENT_NAMESPACE)

    def old_token_rejected():  # the API server caches token checks for ~10s
        try:
            access.whoami(old, get_settings().kube_timeout_seconds)
        except ApiException as e:
            assert e.status == 401
            return
        raise AssertionError("old reader token still accepted")

    eventually(old_token_rejected)
    agent("setup")
    eventually(mcp_reads_the_checkout_service)  # only works if the server reloaded the file
    assert state() == before  # same process: not restarted


def test_access_survives_down_and_up(reader, remediator, cluster, request):
    # if it fails midway, the selector scenario comes back up for the tests after it
    request.addfinalizer(
        lambda: reusable(cluster, "selector") or agent("scenario", "up", "selector")
    )
    agent("scenario", "down", "selector")
    assert not can(reader, "list", "", "pods", ns=CHECKOUT)  # the RoleBindings went with it
    agent("scenario", "up", "selector")
    assert_reader_boundary(reader, [CHECKOUT])
    assert_remediator_boundary(remediator)
    eventually(mcp_reads_the_checkout_service)
