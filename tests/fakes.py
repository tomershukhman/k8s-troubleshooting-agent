"""Test doubles: a scripted chat model, an in-process MCP "cluster" (a real FastMCP server, so
tests go through the same `MCPAdapter` path as production), and a fake Kubernetes API."""

import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP
from kubernetes import client as k
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from k8s_agent.kube import AdminClients

# --- the model -----------------------------------------------------------------------------


async def _next(model: ScriptedModel, messages: list[BaseMessage]) -> Any:
    model.received.append(list(messages))
    item = model.script.pop(0) if model.script else model.then
    if item is None:
        raise AssertionError("the script ran out")
    if isinstance(item, BaseException):
        raise item
    if callable(item):
        item = await item(messages) if inspect.iscoroutinefunction(item) else item(messages)
    # A copy: LangChain and LangGraph write an id into the message they get, and a reply that
    # carries an id already replaces the message with that id instead of adding a turn. So a
    # message object scripted twice, or shared between tests, still makes a new turn each time.
    return item.model_copy(deep=True) if isinstance(item, BaseMessage) else item


class ScriptedModel(BaseChatModel):
    """Replies from `script`, one item per call: an AIMessage (or, as the critic, a verdict
    dict), an exception to raise, or a function of the messages. Then `then`, repeatedly. A
    script is used up as it replies: build a new model for each run."""

    script: list[Any] = Field(default_factory=list)
    then: Any = None
    received: list[list[BaseMessage]] = Field(default_factory=list)
    bind_kwargs: list[dict[str, Any]] = Field(default_factory=list)
    bound_tools: list[list[str]] = Field(default_factory=list)
    structured: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        self.bound_tools.append([t.name for t in tools])
        self.bind_kwargs.append(kwargs)
        return self

    def with_structured_output(self, schema, **kwargs):  # noqa: ANN001
        self.structured.append({"schema": schema, **kwargs})

        async def reply(messages):
            item = await _next(self, messages)
            return schema.model_validate(item) if isinstance(item, dict) else item

        return RunnableLambda(reply)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        raise NotImplementedError("the agent runs async")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return ChatResult(generations=[ChatGeneration(message=await _next(self, messages))])


def call(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    """A model turn that calls tools (in parallel when several)."""
    tool_calls = [
        {"name": name, "args": args, "id": f"call_{uuid4().hex[:8]}", "type": "tool_call"}
        for name, args in calls
    ]
    return AIMessage(content="", tool_calls=tool_calls)


def answer(diagnosis: dict[str, Any], **metadata: Any) -> AIMessage:
    """A final answer: the JSON text native structured output returns."""
    return AIMessage(content=json.dumps(diagnosis), response_metadata=metadata)


SERVICE = {"kind": "Service", "namespace": "shop-checkout", "name": "checkout"}
SERVICE_ARGS = {"apiVersion": "v1", **SERVICE}  # resources_get arguments for that Service


def diagnosis(*refs: str, **fields: Any) -> dict[str, Any]:
    """A `fault_found` selector mismatch citing `refs`, with `fields` overridden."""
    return {
        "status": "fault_found",
        "fault_category": "selector_mismatch",
        "affected_resource": SERVICE,
        "root_cause": "The Service selects app=check-out; the pods are labelled app=checkout.",
        "evidence": [{"ref": r, "finding": f"finding {r}"} for r in refs],
        "recommended_action": "Set the Service selector to app=checkout.",
        "proposed_fix": None,
        "post_fix_check": None,
    } | fields


def healthy(*refs: str) -> dict[str, Any]:
    return diagnosis(*refs, status="no_fault", fault_category="none", affected_resource=None)


def unsure(*refs: str) -> dict[str, Any]:
    return diagnosis(
        *refs,
        status="inconclusive",
        fault_category="none",
        affected_resource=None,
        root_cause="Access to the namespace was refused.",
    )


def selector_fix(**fields: Any) -> dict[str, Any]:
    return {
        "namespace": "shop-checkout",
        "service": "checkout",
        "selector": [{"key": "app", "value": "checkout"}],
        "explanation": "The pods are labelled app=checkout.",
    } | fields


def verdict(*refs: str, founded: bool = True, fix: bool | None = None, feedback: str = ""):
    """A critic verdict: every finding supported iff `founded`."""
    return {
        "findings": [{"ref": r, "supported": founded, "reason": "r"} for r in refs],
        "proposed_fix": None if fix is None else {"backed": fix, "reason": "r"},
        "founded": founded,
        "feedback": feedback or ("" if founded else "The cited output doesn't show it."),
    }


def approve(messages: list[BaseMessage]) -> dict[str, Any]:
    """As the critic: support every finding it's shown ("1. [E3] ...") and any fix."""
    lines = messages[-1].content.splitlines()
    refs = [line.split("[", 1)[1].split("]")[0] for line in lines if line[:1].isdigit()]
    return verdict(*refs, fix=True)


def critic(*verdicts: Any) -> ScriptedModel:
    """A critic that gives `verdicts` in turn, then approves."""
    return ScriptedModel(script=list(verdicts), then=approve)


# --- the MCP server --------------------------------------------------------------------------

DEPLOYMENT_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: shop-checkout
  labels:
    app: checkout
  managedFields:
  - manager: kubectl
    operation: Update
spec:
  replicas: 2
  template:
    metadata:
      managedFields: kept, under spec
status:
  readyReplicas: 2
"""


class FakeCluster:
    """An in-process MCP server with the 7 read tools plus one the agent must never load.
    `outputs` maps a tool to its text, or to a function of its arguments (raise
    `fastmcp.exceptions.ToolError` for a tool error such as an RBAC refusal)."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.outputs = {"resources_get": DEPLOYMENT_YAML, **(outputs or {})}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.server = FastMCP("fake-cluster")
        respond, tool = self._respond, self.server.tool

        @tool
        async def namespaces_list() -> str:
            return await respond("namespaces_list", {})

        @tool
        async def pods_list_in_namespace(namespace: str) -> str:
            return await respond("pods_list_in_namespace", {"namespace": namespace})

        @tool
        async def pods_get(name: str, namespace: str = "") -> str:
            return await respond("pods_get", {"name": name, "namespace": namespace})

        @tool
        async def pods_log(name: str, namespace: str = "", tail: int = 100) -> str:
            return await respond("pods_log", {"name": name, "namespace": namespace, "tail": tail})

        @tool
        async def events_list(namespace: str = "") -> str:
            return await respond("events_list", {"namespace": namespace})

        @tool
        async def resources_get(apiVersion: str, kind: str, name: str, namespace: str = "") -> str:  # noqa: N803
            args = {"apiVersion": apiVersion, "kind": kind, "name": name, "namespace": namespace}
            return await respond("resources_get", args)

        @tool
        async def resources_list(apiVersion: str, kind: str, namespace: str = "") -> str:  # noqa: N803
            args = {"apiVersion": apiVersion, "kind": kind, "namespace": namespace}
            return await respond("resources_list", args)

        @tool
        async def pods_delete(name: str, namespace: str = "") -> str:
            return await respond("pods_delete", {"name": name, "namespace": namespace})

    async def _respond(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        out = self.outputs.get(name, f"{name} output")
        if callable(out):
            out = await out(args) if inspect.iscoroutinefunction(out) else out(args)
        return out


# --- the Kubernetes API ----------------------------------------------------------------------


def pod(ready: bool = True, restarts: int = 0, deleting: bool = False) -> k.V1Pod:
    deleted = datetime.now(UTC) if deleting else None
    status = k.V1ContainerStatus(
        name="c", image="i", image_id="", ready=ready, restart_count=restarts
    )
    return k.V1Pod(
        metadata=k.V1ObjectMeta(name="p", deletion_timestamp=deleted),
        status=k.V1PodStatus(
            phase="Running",
            conditions=[k.V1PodCondition(type="Ready", status=str(ready))],
            container_statuses=[status],
        ),
    )


class FakeKube:
    """The parts of the core and dynamic APIs `setup` and `scenario` use. Records every
    change in order, and keeps namespaces with their labels."""

    def __init__(self, pods: list[k.V1Pod] | None = None) -> None:
        self.log: list[str] = []
        self.namespaces: dict[str, dict[str, str]] = {}  # name -> labels
        self.applied: list[dict[str, Any]] = []
        self.pods = [pod()] if pods is None else pods
        self.resources = SimpleNamespace(get=lambda **kw: SimpleNamespace(**kw))
        conf = k.Configuration(host="https://127.0.0.1:6443")
        self.clients = AdminClients(
            api=SimpleNamespace(configuration=conf), core=self, dynamic=self, context="fake"
        )

    def server_side_apply(self, res, body, namespace, field_manager, force_conflicts):  # noqa: ANN001
        self.applied.append(body)
        self.log.append(f"apply {body['kind']}/{body['metadata']['name']}")
        if body["kind"] == "Namespace":
            self.namespaces[body["metadata"]["name"]] = body["metadata"].get("labels", {})

    def read_namespace(self, name):  # noqa: ANN001
        if name not in self.namespaces:
            raise k.ApiException(status=404)
        return k.V1Namespace(metadata=k.V1ObjectMeta(name=name, labels=self.namespaces[name]))

    def delete_namespace(self, name):  # noqa: ANN001
        self.log.append(f"delete Namespace/{name}")
        del self.namespaces[name]

    def read_namespaced_service_account(self, name, namespace):  # noqa: ANN001
        self.log.append(f"wait ServiceAccount/{name}")
        return k.V1ServiceAccount()

    def list_namespaced_pod(self, namespace):  # noqa: ANN001
        return k.V1PodList(items=self.pods)

    def create_namespaced_service_account_token(self, name, namespace, body):  # noqa: ANN001
        self.log.append(f"token {name} ttl={body.spec.expiration_seconds}")
        expires = datetime(2030, 1, 1, tzinfo=UTC)
        return SimpleNamespace(
            status=SimpleNamespace(token=f"tok-{name}", expiration_timestamp=expires)
        )


class FakeServices:
    """The Kubernetes API as the remediator sees it (the core and discovery calls it makes): the
    checkout Service, its resourceVersion and the pods. A JSON Patch is applied as the API server
    does: all or nothing, and a failed `test` is a 422."""

    def __init__(self, selector: dict[str, str] | None = None) -> None:
        self.selector = selector or {"app": "check-out"}
        self.version = 100
        self.pods = {
            "checkout-a": ({"app": "checkout"}, True),
            "checkout-b": ({"app": "checkout"}, True),
        }
        self.patches: list[tuple[str, str, Any, str]] = []

    def kube(self) -> Any:
        from k8s_agent.agent.remediator import Kube

        return Kube(self, self, timeout=1)

    def edit(self) -> None:
        """Someone changes the Service (any field): a new resourceVersion."""
        self.version += 1

    def read_namespaced_service(self, name, namespace, **_):  # noqa: ANN001
        if (namespace, name) != ("shop-checkout", "checkout"):
            raise k.ApiException(status=404, reason="Not Found")
        meta = k.V1ObjectMeta(name=name, namespace=namespace, resource_version=str(self.version))
        return k.V1Service(metadata=meta, spec=k.V1ServiceSpec(selector=dict(self.selector)))

    def list_namespaced_pod(self, namespace, label_selector=None, **_):  # noqa: ANN001
        want = dict(kv.split("=", 1) for kv in label_selector.split(",")) if label_selector else {}
        return k.V1PodList(items=[
            labelled_pod(name, labels, ready)
            for name, (labels, ready) in self.pods.items() if want.items() <= labels.items()
        ])  # fmt: skip

    def list_namespaced_endpoint_slice(self, namespace, label_selector, **_):  # noqa: ANN001
        """One endpoint per ready pod the selector matches."""
        ready = k.V1EndpointConditions(ready=True)
        endpoints = [
            k.V1Endpoint(addresses=[f"10.0.0.{i}"], conditions=ready,
                         target_ref=k.V1ObjectReference(kind="Pod", name=name, uid=name))
            for i, (name, (labels, is_ready)) in enumerate(self.pods.items(), 1)
            if is_ready and self.selector.items() <= labels.items()
        ]  # fmt: skip
        return k.V1EndpointSliceList(
            items=[k.V1EndpointSlice(address_type="IPv4", endpoints=endpoints)]
        )

    def patch_namespaced_service(self, name, namespace, body, _content_type, **_):  # noqa: ANN001
        self.patches.append((namespace, name, body, _content_type))
        for op in body:
            if op["op"] == "test" and op["value"] != str(self.version):
                raise k.ApiException(status=422, reason="Unprocessable Entity")
        for op in body:
            if (op["op"], op["path"]) == ("replace", "/spec/selector"):
                self.selector = dict(op["value"])
        self.version += 1


def labelled_pod(name: str, labels: dict[str, str], ready: bool) -> k.V1Pod:
    p = pod(ready=ready)
    p.metadata = k.V1ObjectMeta(name=name, labels=labels)
    return p


def fix_call(**args: Any) -> AIMessage:
    """The remediator requests the fix: the expected change at the fake Service's version."""
    base = {
        "namespace": "shop-checkout",
        "service": "checkout",
        "selector": {"app": "checkout"},
        "resource_version": "100",
        "explanation": "Select the two ready checkout pods.",
    }
    return call(("patch_service_selector", base | args))


def remediator(*turns: Any, reason: str = "done") -> ScriptedModel:
    """A remediator that takes `turns` (tool-call turns), then answers."""
    final = AIMessage(content=json.dumps({"reason": reason}))
    return ScriptedModel(script=[*turns, final])
