"""The remediator (SPEC 3.4): the agent that applies the one supported fix, a Service selector
change, as the `remediator` identity only.

It takes the investigator's proposed fix, checks with its own read tools that the exact new
selector matches ready pods, explains the change and calls `patch_service_selector`.
HumanInTheLoopMiddleware pauses the run before that tool runs; a person approves or rejects the
exact call (no edit), and only then does the tool validate and apply it, with a JSON Patch that
holds only if the Service is still at the version the remediator read.

Nothing here uses the MCP server, or any kubeconfig but the remediator's.
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import ValidationError
from urllib3.exceptions import HTTPError

from k8s_agent.agent.middleware import tool_error_text
from k8s_agent.agent.records import (
    Change,
    FixArgs,
    FixOutcome,
    FixStep,
    Remediation,
)
from k8s_agent.agent.tool_output import cap
from k8s_agent.config import Settings
from k8s_agent.kube import KubeError
from k8s_agent.policy import FIX_TOOL
from k8s_agent.schemas import ProposedFix, RemediatorAnswer

PROMPT = resources.files("k8s_agent.agent").joinpath("remediator.md").read_text()
SERVICE_NAME_LABEL = "kubernetes.io/service-name"  # on each of a Service's EndpointSlices
# What a call through the Kubernetes client can raise: the API server's error, or no answer at
# all (urllib3: a timeout, a refused or broken connection; only SSL errors become ApiException).
KUBE_ERRORS = (ApiException, HTTPError)


def labels(selector: dict[str, str] | None) -> str:
    """A selector as `kubectl` writes it: `app=checkout,tier=web`."""
    return ",".join(f"{k}={v}" for k, v in sorted((selector or {}).items())) or "(none)"


@dataclass(frozen=True)
class Kube:
    """The remediator's cluster access. Every call has a time limit."""

    core: Any  # client.CoreV1Api
    discovery: Any  # client.DiscoveryV1Api
    timeout: float

    @classmethod
    def load(cls, kubeconfig: Path, timeout: float) -> Kube:
        """From the remediator kubeconfig only: never $KUBECONFIG, ~/.kube/config or an
        in-cluster account, so a missing file is an error, not another identity."""
        try:
            api = config.new_client_from_config(config_file=str(kubeconfig))
        except ConfigException as e:
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
        query = {"label_selector": labels(selector)} if selector else {}
        found = self.core.list_namespaced_pod(namespace, _request_timeout=self.timeout, **query)
        return [p for p in found.items if p.metadata.deletion_timestamp is None]

    def ready_pods(self, namespace: str, selector: dict[str, str]) -> list[str]:
        """The ready pods the exact `selector` matches (as a Service's: an empty one, none)."""
        if not selector:
            return []
        return sorted(p.metadata.name for p in self.pods(namespace, selector) if ready(p))

    def ready_endpoints(self, namespace: str, service: str) -> int:
        """Ready endpoints across the Service's EndpointSlices, by the API's rules: an unset
        `ready` means ready, and one endpoint may be in several slices, so each counts once."""
        slices = self.discovery.list_namespaced_endpoint_slice(
            namespace,
            label_selector=f"{SERVICE_NAME_LABEL}={service}",
            _request_timeout=self.timeout,
        )
        return len({
            e.target_ref.uid if e.target_ref and e.target_ref.uid else tuple(e.addresses)
            for s in slices.items for e in s.endpoints or []
            if e.conditions is None or e.conditions.ready is not False
        })  # fmt: skip

    def replace_selector(self, namespace: str, name: str, selector: dict[str, str], version: str):
        """One JSON Patch, applied atomically by the API server: the test fails (422) if the
        Service is no longer at `version`, and then nothing is applied. `replace` sets the
        whole selector, so no old key survives (a merge patch would keep them)."""
        ops = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            {"op": "replace", "path": "/spec/selector", "value": selector},
        ]
        return self.core.patch_namespaced_service(
            name, namespace, ops,
            _content_type="application/json-patch+json", _request_timeout=self.timeout,
        )  # fmt: skip


def ready(pod: client.V1Pod) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or [])


# --- the fix ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FixResult:
    outcome: FixOutcome
    detail: str
    before: dict[str, str] | None = None
    endpoints_ready: int = 0


def apply_fix(kube: Kube, args: FixArgs, settings: Settings) -> FixResult:
    """Validate, apply exactly `args.selector` if the Service is still at
    `args.resource_version`, then wait for ready endpoints."""
    ns, name, want = args.namespace, args.service, labels(args.selector)
    if ns not in settings.remediation_namespaces:
        allowed = ", ".join(settings.remediation_namespaces)
        return FixResult(FixOutcome.invalid, f"fixes are allowed only in {allowed}, not in {ns}")
    if not args.selector or not all(k.strip() and v.strip() for k, v in args.selector.items()):
        return FixResult(FixOutcome.invalid, "the selector needs non-empty keys and values")
    before = None
    try:
        svc = kube.service(ns, name)
        before, version = svc.spec.selector, svc.metadata.resource_version
        if version != args.resource_version:
            return stale(args, version, before)
        if not kube.ready_pods(ns, args.selector):
            return FixResult(FixOutcome.invalid, f"no ready pod matches {want}", before)
    except ApiException as e:
        return failure(kube, args, e, before)
    except HTTPError as e:  # the API server didn't answer (urllib3: timeout, connection)
        return FixResult(FixOutcome.failed, f"no answer from the API server: {e}", before)
    try:
        kube.replace_selector(ns, name, args.selector, args.resource_version)
    except ApiException as e:
        return failure(kube, args, e, before)
    except HTTPError as e:  # it may have landed before the connection broke
        detail = f"no answer to the patch: {e}; check whether the Service's selector changed"
        return FixResult(FixOutcome.failed, detail, before)
    try:  # applied: whatever the wait finds, the outcome stays applied
        endpoints = wait_for_endpoints(kube, ns, name, settings)
        counted = f"{endpoints} ready endpoint{'s' * (endpoints != 1)}"
    except KUBE_ERRORS as e:
        endpoints, counted = 0, f"couldn't count the endpoints: {e}"
    detail = f"selector {labels(before)} → {want}; {counted}"
    return FixResult(FixOutcome.applied, detail, before, endpoints)


def stale(args: FixArgs, version: str | None, before: dict[str, str] | None) -> FixResult:
    detail = (f"the Service changed after the remediator read it (version "
              f"{args.resource_version} → {version}); nothing was applied")  # fmt: skip
    return FixResult(FixOutcome.stale, detail, before)


def failure(kube: Kube, args: FixArgs, e: ApiException, before: dict | None) -> FixResult:
    """An API error before anything was applied. A failed JSON Patch test is a 422 like any
    invalid request, so the Service's version tells whether it changed meanwhile."""
    if e.status == 404:
        return FixResult(FixOutcome.invalid, f"Service {args.namespace}/{args.service} not found")
    if e.status == 422:
        try:
            version = kube.service(args.namespace, args.service).metadata.resource_version
        except KUBE_ERRORS:
            version = None
        if version != args.resource_version:
            return stale(args, version, before)
        return FixResult(FixOutcome.invalid, f"the API server refused it: {e.body}", before)
    return FixResult(FixOutcome.failed, f"the API call failed: {e.status} {e.reason}", before)


def wait_for_endpoints(kube: Kube, namespace: str, service: str, settings: Settings) -> int:
    """Ready endpoints, polled until there is one or AGENT_VERIFY_WAIT_SECONDS pass."""
    deadline = time.monotonic() + settings.verify_wait_seconds
    while (n := kube.ready_endpoints(namespace, service)) == 0 and time.monotonic() < deadline:
        time.sleep(settings.verify_poll_seconds)
    return n


# --- the agent -------------------------------------------------------------------------------


def tools(kube: Callable[[], Kube], settings: Settings) -> list[BaseTool]:
    """The remediator's tools, all through `kube()` (the remediator identity). A read that
    fails raises, so its trace span is an error; ToolErrorMiddleware gives the model the text.
    A read's result has the investigator's safety cap (AGENT_TOOL_OUTPUT_MAX_CHARS)."""

    def capped(text: str) -> str:
        return cap(text, settings.tool_output_max_chars)

    def read[T](do: Callable[[Kube], T]) -> T:
        try:
            return do(kube())
        except KubeError as e:
            raise ToolException(str(e)) from e
        except ApiException as e:
            raise ToolException(f"{e.status} {e.reason}: {e.body}") from e
        except HTTPError as e:
            raise ToolException(f"no answer from the API server: {e}") from e

    def get_service(namespace: str, name: str) -> str:
        """Read a Service: its selector and its resourceVersion."""
        svc = read(lambda k: k.service(namespace, name))
        return capped(f"Service {namespace}/{name}: selector {labels(svc.spec.selector)}, "
                      f"resourceVersion {svc.metadata.resource_version}")  # fmt: skip

    def list_pods(namespace: str, selector: dict[str, str] | None = None) -> str:
        """List the pods in a namespace, or only those an exact label selector matches: each
        with whether it is ready, and its labels."""
        pods = read(lambda k: k.pods(namespace, selector))
        which = f"matching {labels(selector)}" if selector else "in total"
        rows = [f"{p.metadata.name}  {'ready' if ready(p) else 'not ready'}  "
                f"labels {labels(p.metadata.labels)}" for p in pods]  # fmt: skip
        summary = f"{sum(map(ready, pods))} of {len(pods)} pods ready, {which}"
        return capped("\n".join([summary, *rows]))

    def list_endpoint_slices(namespace: str, service: str) -> str:
        """Count a Service's ready endpoints, from its EndpointSlices."""
        n = read(lambda k: k.ready_endpoints(namespace, service))
        return capped(f"Service {namespace}/{service} has {n} ready endpoint{'s' * (n != 1)}")

    def patch_service_selector(**fields: Any) -> tuple[str, dict[str, Any]]:
        """Replace a Service's whole selector with `selector`. A person approves or rejects
        this exact call before it runs. Then it checks again (the namespace allows fixes, the
        Service is still at `resource_version`, the selector matches a ready pod), applies the
        change, and waits for the Service's ready endpoints."""
        args = FixArgs(**fields)
        try:
            result = apply_fix(kube(), args, settings)
        except KubeError as e:
            result = FixResult(FixOutcome.failed, str(e))
        return f"{result.outcome}: {result.detail}", asdict(result)

    reads = [StructuredTool.from_function(f) for f in (get_service, list_pods,
                                                        list_endpoint_slices)]  # fmt: skip
    fix = StructuredTool.from_function(
        patch_service_selector, name=FIX_TOOL, args_schema=FixArgs,
        response_format="content_and_artifact",
    )  # fmt: skip
    return [*reads, fix]


def build_remediator(model: BaseChatModel, kube: Callable[[], Kube], settings: Settings):
    """The limits count per remediation (`thread_limit`: kept across the approval pause, which
    resets run counts): the per-round limits, and one fix request."""
    return create_agent(
        model,
        tools(kube, settings),
        system_prompt=PROMPT,
        response_format=ProviderStrategy(RemediatorAnswer),  # native structured output (SPEC 3.2)
        middleware=[
            # The pause before the fix tool runs. per_call: LangGraph accepts only approve or
            # reject, before saving the answer. First, so it wraps the error handling below
            # (LangChain's docs: otherwise a retried call asks again).
            HumanInTheLoopMiddleware(
                {FIX_TOOL: {"allowed_decisions": ["approve", "reject"]}}, interrupt_mode="per_call"
            ),
            ToolErrorMiddleware(on_error=tool_error_text),
            ModelCallLimitMiddleware(thread_limit=settings.max_model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(thread_limit=settings.max_tool_calls, exit_behavior="continue"),
            ToolCallLimitMiddleware(tool_name=FIX_TOOL, thread_limit=1, exit_behavior="continue"),
        ],
        name="remediator",
    )


def fix_request(fix: ProposedFix) -> HumanMessage:
    """The remediator's input: the proposed fix only (it doesn't redo the diagnosis)."""
    return HumanMessage(
        f"Proposed fix: set the selector of Service {fix.namespace}/{fix.service} to "
        f"{labels(fix.selector_dict())}.\nWhy: {fix.explanation}"
    )


def remediation(
    fix: ProposedFix,
    messages: list[BaseMessage],
    answer: RemediatorAnswer | None,
    stopped: str | None = None,
) -> Remediation:
    """What the remediator did, read from its conversation: its fix request (if any), the fix
    tool's result (or the reject in its place), and every tool call. `stopped`: why it ended
    early; what it did before still stands (an applied fix stays applied)."""
    calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps = [
        FixStep(tool=calls[i]["name"], args=calls[i]["args"], output=m.text,
                error=m.status == "error")
        for i, m in results.items() if i in calls
    ]  # fmt: skip
    request = next((c for c in calls.values() if c["name"] == FIX_TOOL), None)
    reply = results.get(request["id"]) if request else None
    result = FixResult(**reply.artifact) if reply and isinstance(reply.artifact, dict) else None
    try:  # a malformed request was rejected at the gate: the proposal stands for the change
        args = FixArgs.model_validate(request["args"]) if request else None
    except ValidationError:
        args = None
    change = Change(
        namespace=args.namespace if args else fix.namespace,
        service=args.service if args else fix.service,
        before=result.before if result else None,
        after=args.selector if args else fix.selector_dict(),
    )
    if result is not None:  # the fix tool ran
        detail = result.detail + (f"; then the remediator stopped: {stopped}" if stopped else "")
        return Remediation(change=change, outcome=result.outcome, detail=detail,
                           endpoints_ready=result.endpoints_ready, steps=steps)  # fmt: skip
    if stopped:
        detail = f"the remediator stopped before the fix tool ran: {stopped}"
        return Remediation(change=change, outcome=FixOutcome.failed, detail=detail, steps=steps)
    if reply is not None:  # rejected (its text, with the reason, took the tool's place)
        detail = "the request was rejected, so nothing was applied"
    else:  # declined
        detail = answer.reason if answer else "the remediator ended without requesting the fix"
    return Remediation(change=change, outcome=FixOutcome.not_applied, detail=detail, steps=steps)
