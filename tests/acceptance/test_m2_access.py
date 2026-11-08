"""M2: the agent's identities and the MCP server hold the safety boundary (PLAN.md M2).

The module fixture runs `agent setup`, brings up all four scenarios, plants a Secret, and
starts the `k8s-mcp` compose service if it isn't running. Then the boundary is checked two
ways: through the MCP server, with the client library the agent will use (`langchain[mcp]`),
and directly at Kubernetes, with each identity's own kubeconfig. Every step registers its own
cleanup first, so a failure anywhere still removes the scenarios (and stops k8s-mcp if this
module started it).
"""

import asyncio
import base64
import subprocess
from pathlib import Path

import pytest
import yaml
from fastmcp import Client as FastMCPClient
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from langchain.mcp import MCPAdapter
from mcp.shared.exceptions import MCPError

from k8s_agent import access
from k8s_agent.config import get_settings
from k8s_agent.kube import AdminClients
from k8s_agent.policy import READ_ONLY_TOOLS

from .conftest import agent, eventually, scenario_namespace

ROOT = Path(__file__).resolve().parents[2]
FOLDERS = ["crashloop", "selector", "readiness", "healthy"]
NS = {folder: scenario_namespace(folder) for folder in FOLDERS}
ORDERS, CHECKOUT = NS["crashloop"], NS["selector"]
SECRET, SECRET_VALUE = "orders-db-password", "canary-value-must-not-leak"


# --- fixtures --------------------------------------------------------------------------


def compose(*args: str) -> str:
    out = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return out.stdout


@pytest.fixture(scope="module", autouse=True)
def boundary(kube: AdminClients, request):
    agent("setup")
    for folder in FOLDERS:
        request.addfinalizer(lambda f=folder: agent("scenario", "down", f))
        agent("scenario", "up", folder)
    # A Secret to try to read: refusal must come from policy, not from "not found". It goes
    # with its namespace.
    kube.core.create_namespaced_secret(
        ORDERS,
        client.V1Secret(metadata=client.V1ObjectMeta(name=SECRET), string_data={"v": SECRET_VALUE}),
    )
    if not compose("ps", "-q", "--status", "running", "k8s-mcp").strip():
        request.addfinalizer(lambda: compose("rm", "-sf", "k8s-mcp"))
        print("starting k8s-mcp (stopped again afterwards)")
    compose("up", "-d", "--wait", "k8s-mcp")


def identity(path: Path) -> client.ApiClient:
    return config.new_client_from_config(config_file=str(path))


# Per test, from the current file: `agent setup` rotates the tokens, and one test revokes them.
@pytest.fixture
def reader() -> client.ApiClient:
    return identity(get_settings().reader_kubeconfig)


@pytest.fixture
def remediator() -> client.ApiClient:
    return identity(get_settings().remediator_kubeconfig)


def crash_pod(kube: AdminClients) -> str:
    pods = kube.core.list_namespaced_pod(ORDERS).items
    [pod] = [p for p in pods if not p.metadata.deletion_timestamp]
    return pod.metadata.name


# --- through the MCP server, with langchain[mcp] ---------------------------------------


def with_mcp(fn):
    async def run():
        s = get_settings()
        mcp_client = FastMCPClient(s.mcp_endpoint, timeout=s.accept_assert_timeout_seconds)
        async with MCPAdapter(mcp_client) as adapter:
            return await fn(adapter)

    return asyncio.run(run())


def call(tool: str, args: dict) -> tuple[bool, str]:
    """(is_error, text) of one tool call, made the way the agent loop makes it: a tool call
    in, a ToolMessage out."""

    async def run(adapter):
        tools = {t.name: t for t in await adapter.list_tools()}
        msg = await tools[tool].ainvoke(
            {"type": "tool_call", "id": "m2", "name": tool, "args": args}
        )
        text = "\n".join(part.get("text", "") for part in msg.content)
        return msg.status == "error", text

    return with_mcp(run)


def test_mcp_offers_exactly_the_seven_read_tools():
    async def run(adapter):
        return {t.name: set(t.args) for t in await adapter.list_tools()}

    tools = with_mcp(run)
    assert set(tools) == READ_ONLY_TOOLS
    # the argument names the evals' required_tools match on (SPEC 4.4)
    assert {"apiVersion", "kind", "name", "namespace"} <= tools["resources_get"]
    assert {"name", "namespace", "previous", "tail"} <= tools["pods_log"]


@pytest.mark.parametrize(
    "tool",
    ["pods_exec", "pods_run", "pods_delete", "resources_create_or_update", "resources_delete"],
)
def test_mcp_tools_that_change_or_run_things_do_not_exist(tool):
    async def run(adapter):
        try:
            await adapter.client.call_tool(tool, {"name": "x", "namespace": ORDERS})
        except MCPError as e:
            return str(e)
        return "called"

    assert with_mcp(run) == f'unknown tool "{tool}"'


def test_mcp_lists_the_orders_pod(kube):
    err, text = call("pods_list_in_namespace", {"namespace": ORDERS})
    assert not err, text
    assert crash_pod(kube) in text  # the container reached the cluster as reader


def test_mcp_reads_the_crash_log(kube):
    # The previous container's log can be gone after a few restarts (M1 finding); then the
    # current (terminated) container's log has the same line.
    pod = crash_pod(kube)
    logs = [
        call("pods_log", {"name": pod, "namespace": ORDERS, "previous": True}),
        call("pods_log", {"name": pod, "namespace": ORDERS}),
    ]
    assert any(not err and "DATABASE_URL not set" in text for err, text in logs), logs


@pytest.mark.parametrize(
    ("tool", "args", "expect"),
    [  # every allowed tool works as reader
        ("pods_get", {"name": "<crash_pod>", "namespace": ORDERS}, "orders-config"),
        ("events_list", {"namespace": ORDERS}, "BackOff"),
        (
            "resources_get",
            {"apiVersion": "v1", "kind": "Service", "name": "checkout", "namespace": CHECKOUT},
            "check-out",
        ),
        (
            "resources_get",
            {"apiVersion": "v1", "kind": "ConfigMap", "name": "orders-config", "namespace": ORDERS},
            "DATABSE_URL",
        ),
        (
            "resources_list",
            {"apiVersion": "apps/v1", "kind": "Deployment", "namespace": CHECKOUT},
            "checkout",
        ),
    ],
)
def test_mcp_read_tools_work(kube, tool, args, expect):
    args = {k: crash_pod(kube) if v == "<crash_pod>" else v for k, v in args.items()}
    err, text = call(tool, args)
    assert not err, text
    assert expect in text


def test_mcp_namespace_list_gives_nothing_away():
    err, text = call("namespaces_list", {})
    assert not err, text
    for ns in NS.values():
        assert ns in text
    leaked = [folder for folder in FOLDERS if folder in text.lower()]
    assert leaked == [], text


SECRET_CALLS = [
    ("resources_get", {"apiVersion": "v1", "kind": "Secret", "name": SECRET, "namespace": ORDERS}),
    ("resources_list", {"apiVersion": "v1", "kind": "Secret", "namespace": ORDERS}),
    ("resources_list", {"apiVersion": "v1", "kind": "Secret"}),
]


@pytest.mark.parametrize(("tool", "args"), SECRET_CALLS)
def test_mcp_refuses_secrets(tool, args):
    err, text = call(tool, args)
    assert err, text
    assert "resource not allowed" in text


def test_secret_value_never_appears_in_any_mcp_output(kube):
    outputs = [call(tool, args)[1] for tool, args in SECRET_CALLS]
    outputs += [
        call("events_list", {"namespace": ORDERS})[1],
        call("pods_get", {"name": crash_pod(kube), "namespace": ORDERS})[1],
        call("resources_list", {"apiVersion": "v1", "kind": "ConfigMap", "namespace": ORDERS})[1],
    ]
    encoded = base64.b64encode(SECRET_VALUE.encode()).decode()
    for out in outputs:
        assert SECRET_VALUE not in out and encoded not in out


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pods_list_in_namespace", {"namespace": "kube-system"}),
        (
            "resources_get",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "name": "kube-root-ca.crt",
                "namespace": "kube-system",
            },
        ),
        ("events_list", {"namespace": "kube-system"}),
        ("events_list", {}),  # all namespaces: never cluster-wide
        ("resources_list", {"apiVersion": "v1", "kind": "ConfigMap"}),
    ],
)
def test_mcp_refuses_outside_the_scenario_namespaces(tool, args):
    err, text = call(tool, args)
    assert err, text
    assert "forbidden" in text and '"system:serviceaccount:k8s-agent:reader"' in text


# --- the running k8s-mcp container ---------------------------------------------------


def inspect_mcp() -> dict:
    cid = compose("ps", "-q", "k8s-mcp").strip()
    out = subprocess.run(
        ["docker", "inspect", cid], check=True, capture_output=True, text=True
    ).stdout
    return yaml.safe_load(out)[0]  # JSON is YAML


def test_mcp_container_is_pinned_and_hardened():
    c = inspect_mcp()
    image = c["Config"]["Image"]
    assert image.startswith("quay.io/containers/kubernetes_mcp_server:v") and "@sha256:" in image
    assert c["HostConfig"]["NetworkMode"] == "host"
    assert c["HostConfig"]["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in c["HostConfig"]["SecurityOpt"]
    configured = yaml.safe_load(compose("config", "--format", "json"))["services"]["k8s-mcp"]
    assert c["Config"]["User"] == configured["user"]  # AGENT_CONTAINER_USER, default 65532:65532
    assert not c["Config"]["User"].startswith("0:")
    assert c["State"]["Health"]["Status"] == "healthy"


def test_mcp_container_holds_only_the_reader_credentials():
    mounts = {m["Destination"]: m for m in inspect_mcp()["Mounts"]}
    assert set(mounts) == {"/etc/k8s-mcp/config.toml", "/kube/reader.kubeconfig"}
    assert all(m["RW"] is False for m in mounts.values())
    reader = Path(mounts["/kube/reader.kubeconfig"]["Source"])
    assert reader.resolve() == get_settings().reader_kubeconfig.resolve()
    assert compose("exec", "-T", "k8s-mcp", "ls", "/kube").split() == ["reader.kubeconfig"]


def test_mcp_listens_on_loopback_only():
    # Host networking shares the machine's network, so a bind to all interfaces would expose
    # the unauthenticated server. Read the listening sockets from inside its network.
    port = f"{get_settings().mcp_port:04X}"
    listening = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        rows = compose("exec", "-T", "k8s-mcp", "cat", table).splitlines()[1:]
        for row in rows:
            local, state = row.split()[1], row.split()[3]
            if state == "0A" and local.endswith(f":{port}"):  # 0A = LISTEN
                listening.append(local)
    assert listening == ["0100007F:" + port], listening  # 127.0.0.1 only, no IPv6


# --- directly at Kubernetes, each identity with its own kubeconfig ---------------------

READ = ["get", "list", "watch"]
WRITE = ["create", "update", "patch", "delete", "deletecollection"]
# (group, resource, subresource) the reader may read in scenario namespaces
READABLE = [
    ("", "pods", None),
    ("", "pods", "log"),
    ("", "events", None),
    ("events.k8s.io", "events", None),
    ("", "services", None),
    ("", "endpoints", None),
    ("discovery.k8s.io", "endpointslices", None),
    ("apps", "deployments", None),
    ("apps", "replicasets", None),
    ("", "configmaps", None),
]
OTHER = [("", "secrets", None), ("", "pods", "exec"), ("rbac.authorization.k8s.io", "roles", None)]
# (verb, group, resource) the remediator may do in the remediation namespace (SPEC 3.5)
REMEDIATE = {
    ("get", "", "services"),
    ("patch", "", "services"),
    ("get", "", "pods"),
    ("list", "", "pods"),
    ("get", "discovery.k8s.io", "endpointslices"),
    ("list", "discovery.k8s.io", "endpointslices"),
}


def can(api: client.ApiClient, verb, group, resource, sub=None, ns=None) -> bool:
    attrs = client.V1ResourceAttributes(
        verb=verb, group=group, resource=resource, subresource=sub, namespace=ns
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
            for verb in READ + WRITE:
                assert not can(reader, verb, group, res, sub, ns), (verb, res, sub, ns)


def assert_remediator_boundary(remediator: client.ApiClient) -> None:
    for ns in NS.values():
        for group, res, sub in READABLE + OTHER:
            for verb in READ + WRITE:
                want = ns == CHECKOUT and sub is None and (verb, group, res) in REMEDIATE
                assert can(remediator, verb, group, res, sub, ns) is want, (verb, res, sub, ns)


def test_reader_can_read_every_scenario_namespace_and_change_nothing(reader):
    assert_reader_boundary(reader, list(NS.values()))
    assert can(reader, "list", "", "namespaces")  # cluster-wide, to find where to look


@pytest.mark.parametrize("ns", ["kube-system", "default", access.AGENT_NAMESPACE, None])
def test_reader_has_nothing_elsewhere(reader, ns):
    for group, res, sub in READABLE:
        assert not can(reader, "list" if sub is None else "get", group, res, sub, ns), (res, ns)


def test_reader_real_calls(reader, kube):
    core = client.CoreV1Api(reader)
    pod = crash_pod(kube)
    logs = []
    for previous in (True, False):
        try:
            logs.append(core.read_namespaced_pod_log(pod, ORDERS, previous=previous))
        except ApiException as e:
            logs.append(f"<{e.status}>")
    assert any("DATABASE_URL not set" in log for log in logs), logs
    assert core.read_namespaced_config_map("orders-config", ORDERS).data["DATABSE_URL"]

    refused = {
        "read Secret": lambda: core.read_namespaced_secret(SECRET, ORDERS),
        "list kube-system pods": lambda: core.list_namespaced_pod("kube-system"),
        "delete pod (dry run)": lambda: core.delete_namespaced_pod(pod, ORDERS, dry_run="All"),
        "create ConfigMap (dry run)": lambda: core.create_namespaced_config_map(
            ORDERS, client.V1ConfigMap(metadata=client.V1ObjectMeta(name="x")), dry_run="All"
        ),
        "patch Deployment (dry run)": lambda: client.AppsV1Api(reader).patch_namespaced_deployment(
            "orders", ORDERS, {"spec": {"replicas": 0}}, dry_run="All"
        ),
    }
    for what, do in refused.items():
        with pytest.raises(ApiException) as e:
            do()
        assert e.value.status == 403, what


def test_remediator_can_only_fix_and_verify_in_checkout(remediator):
    assert_remediator_boundary(remediator)
    for ns in ["default", "kube-system", access.AGENT_NAMESPACE]:
        for verb, group, res in REMEDIATE:
            assert not can(remediator, verb, group, res, ns=ns), (verb, res, ns)
    assert not can(remediator, "list", "", "namespaces")


def test_remediator_real_calls(remediator, kube):
    core = client.CoreV1Api(remediator)
    assert core.read_namespaced_service("checkout", CHECKOUT).spec.selector == {"app": "check-out"}
    assert len(core.list_namespaced_pod(CHECKOUT).items) == 2
    discovery = client.DiscoveryV1Api(remediator)
    assert discovery.list_namespaced_endpoint_slice(
        CHECKOUT, label_selector="kubernetes.io/service-name=checkout"
    ).items
    patched = core.patch_namespaced_service(
        "checkout", CHECKOUT, {"spec": {"selector": {"app": "checkout"}}}, dry_run="All"
    )
    assert patched.spec.selector == {"app": "checkout"}  # allowed, but only a dry run:
    live = kube.core.read_namespaced_service("checkout", CHECKOUT)
    assert live.spec.selector == {"app": "check-out"}

    refused = {
        "patch Service elsewhere": lambda: core.patch_namespaced_service(
            "x", ORDERS, {"spec": {"selector": {"a": "b"}}}, dry_run="All"
        ),
        "list pods elsewhere": lambda: core.list_namespaced_pod(ORDERS),
        "read ConfigMap": lambda: core.read_namespaced_config_map("kube-root-ca.crt", CHECKOUT),
        "read Secret": lambda: core.list_namespaced_secret(CHECKOUT),
        "delete Service": lambda: core.delete_namespaced_service(
            "checkout", CHECKOUT, dry_run="All"
        ),
        "delete pod": lambda: core.delete_namespaced_pod(
            core.list_namespaced_pod(CHECKOUT).items[0].metadata.name, CHECKOUT, dry_run="All"
        ),
    }
    for what, do in refused.items():
        with pytest.raises(ApiException) as e:
            do()
        assert e.value.status == 403, what


# --- robustness ------------------------------------------------------------------------

EXPECTED_BINDINGS = {(ns, "k8s-agent-reader") for ns in NS.values()} | {
    (CHECKOUT, "k8s-agent-remediator")
}


def agent_bindings(kube: AdminClients) -> set[tuple[str, str]]:
    """The agent's RoleBindings in the four scenario namespaces."""
    rbac = client.RbacAuthorizationV1Api(kube.api)
    selector = f"{access.MANAGED_BY_LABEL}={access.FIELD_MANAGER}"
    items = rbac.list_role_binding_for_all_namespaces(label_selector=selector).items
    return {
        (b.metadata.namespace, b.metadata.name)
        for b in items
        if b.metadata.namespace in NS.values()
    }


def mcp_reads_the_checkout_service():
    err, text = call(
        "resources_get",
        {"apiVersion": "v1", "kind": "Service", "name": "checkout", "namespace": CHECKOUT},
    )
    assert not err and "check-out" in text, text


def test_setup_again_is_harmless(kube):
    assert agent_bindings(kube) == EXPECTED_BINDINGS
    agent("setup")  # again, now that the scenarios and k8s-mcp are up
    assert agent_bindings(kube) == EXPECTED_BINDINGS
    rbac = client.RbacAuthorizationV1Api(kube.api)
    selector = f"{access.MANAGED_BY_LABEL}={access.FIELD_MANAGER}"
    sas = kube.core.list_namespaced_service_account(access.AGENT_NAMESPACE, label_selector=selector)
    assert sorted(sa.metadata.name for sa in sas.items) == ["reader", "remediator"]
    crbs = rbac.list_cluster_role_binding(label_selector=selector).items
    assert [b.metadata.name for b in crbs] == ["k8s-agent-list-namespaces-reader"]
    # the rewritten kubeconfigs work, with the same boundary
    s = get_settings()
    assert_reader_boundary(identity(s.reader_kubeconfig), list(NS.values()))
    assert_remediator_boundary(identity(s.remediator_kubeconfig))
    eventually(mcp_reads_the_checkout_service)


def test_setup_removes_grants_of_namespaces_no_longer_configured(kube, request):
    request.addfinalizer(lambda: agent("setup"))  # restore the default grants
    agent("setup", env={"AGENT_NAMESPACES": CHECKOUT})
    assert agent_bindings(kube) == {
        (CHECKOUT, "k8s-agent-reader"),
        (CHECKOUT, "k8s-agent-remediator"),
    }
    reader = identity(get_settings().reader_kubeconfig)
    assert not can(reader, "list", "", "pods", ns=ORDERS)
    err, text = call("pods_list_in_namespace", {"namespace": ORDERS})
    assert err and "forbidden" in text, text


def mcp_container_state() -> str:
    state = inspect_mcp()
    return f"{state['State']['StartedAt']} restarts={state['RestartCount']}"


def test_mcp_picks_up_a_rotated_token_without_restart(kube, request):
    """Re-running `agent setup` refreshes the running server's token (README claim)."""
    request.addfinalizer(lambda: agent("setup"))  # recreates the reader if this test fails
    old = yaml.safe_load(get_settings().reader_kubeconfig.read_text())
    before = mcp_container_state()
    # Recreating the ServiceAccount revokes every token issued to the old one.
    kube.core.delete_namespaced_service_account(access.READER, access.AGENT_NAMESPACE)

    def old_token_rejected():  # the API server caches token checks for ~10s
        try:
            access.whoami(old)
        except ApiException as e:
            assert e.status == 401
            return
        raise AssertionError("old reader token still accepted")

    eventually(old_token_rejected)
    agent("setup")
    eventually(mcp_reads_the_checkout_service)  # only works if the server reloaded the file
    assert mcp_container_state() == before  # same process: not restarted


def test_grants_survive_down_and_up(reader, remediator):
    agent("scenario", "down", "selector")
    assert not can(reader, "list", "", "pods", ns=CHECKOUT)  # bindings went with the namespace
    assert not can(remediator, "patch", "", "services", ns=CHECKOUT)

    agent("scenario", "up", "selector")
    assert_reader_boundary(reader, [CHECKOUT])
    assert_remediator_boundary(remediator)
    eventually(mcp_reads_the_checkout_service)
