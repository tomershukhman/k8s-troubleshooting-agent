"""`investigate()`: the one entry point the CLI and the evals call.

It runs the investigation graph (graph.py) on its own thread and never raises for a run that
started; every outcome becomes a `RunRecord`:
- a diagnosis that passed verification (`fault_found`, `no_fault` or `inconclusive`)
- an `inconclusive` forced by code: a limit or budget, a refusal, an invalid answer, or
  evidence that stayed unfounded after the last verify round
- `failed`: a model API error, or a bug
Only `NotStarted` is raised (e.g. `McpUnavailable`): nothing ran.

A fix the remediator requests pauses the graph; the pause is decided here, inside the same call
(approval.py), and the graph resumes on the same thread. So a run is one trace, fix included.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any
from uuid import uuid4

from fastmcp import Client as FastMCPClient
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain.chat_models import init_chat_model
from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.exceptions import ModelError, ModelRefusalError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import add_messages
from langgraph.types import Command, TasksStreamPart, UpdatesStreamPart

from k8s_agent import tracing
from k8s_agent.agent import approval
from k8s_agent.agent.approval import Approver, Gate
from k8s_agent.agent.critic import with_verdict
from k8s_agent.agent.graph import build_graph, forced_inconclusive
from k8s_agent.agent.middleware import RunContext
from k8s_agent.agent.records import (
    ApprovalRequest,
    Change,
    Decided,
    Decision,
    Drafted,
    EvidenceStep,
    FixCalled,
    FixOutcome,
    FixStep,
    ProgressEvent,
    Reasoning,
    Rechecked,
    Remediated,
    Remediation,
    RunRecord,
    RunResult,
    RunStatus,
    Stage,
    Step,
    ToolCalled,
    Verified,
    evidence_of,
    steps_from_messages,
    thinking_of,
)
from k8s_agent.agent.remediator import Kube
from k8s_agent.agent.tool_output import cap
from k8s_agent.config import Settings
from k8s_agent.logs import run_log
from k8s_agent.policy import READ_ONLY_TOOLS
from k8s_agent.schemas import Diagnosis

# langchain.mcp is beta and says so on import; it's the MCP client we chose (SPEC 3.2).
with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

STAGES = ("intake", "investigate", "verify_evidence", "result", "remediate", "recheck")
Asked = list[tuple[ApprovalRequest, Decision]]


class NotStarted(Exception):
    """The run couldn't start, so nothing ran (no model call, no tool call)."""


class McpUnavailable(NotStarted):
    """The MCP server can't be reached or offers none of the allowed tools."""


@dataclass
class Outcome:
    status: RunStatus
    diagnosis: Diagnosis | None = None
    error: str | None = None
    forced_reason: str | None = None
    fix_stopped: str | None = None  # why it stopped after the result (in the fix), if it did


def make_model(settings: Settings, model: str) -> BaseChatModel:
    """A chat model from a LangChain model string (AGENT_MODEL, AGENT_CRITIC_MODEL)."""
    kwargs: dict[str, Any] = {
        "max_tokens": settings.max_output_tokens,
        "max_retries": settings.model_max_retries,  # the provider SDK retries 429/5xx/network
    }
    if model.startswith("anthropic:"):
        kwargs["effort"] = settings.effort
        # Readable reasoning summaries in the trace and progress lines; the default
        # ("omitted") returns thinking blocks with empty text.
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if settings.anthropic_api_key is not None:
            kwargs["api_key"] = settings.anthropic_api_key.get_secret_value()
    return init_chat_model(model, **kwargs)


def mcp_client(settings: Settings) -> FastMCPClient:
    return FastMCPClient(settings.mcp_endpoint, timeout=settings.mcp_timeout_seconds)


async def _load_tools(
    stack: AsyncExitStack, target: Any, settings: Settings, log: logging.LoggerAdapter
) -> list[BaseTool]:
    """Connect (the connection stays open for the run: every call, parallel ones included,
    shares it) and keep only the allowlisted read tools (SPEC §3.3)."""
    where = f"cannot use the MCP server at {settings.mcp_endpoint}"
    try:
        adapter = await stack.enter_async_context(MCPAdapter(target))
        offered = await adapter.list_tools()
    except Exception as e:
        log.error("mcp_unavailable", extra={"error": f"{type(e).__name__}: {e}"})
        raise McpUnavailable(f"{where}: {type(e).__name__}: {e}") from e
    if extra := sorted(t.name for t in offered if t.name not in READ_ONLY_TOOLS):
        log.warning("mcp_tools_ignored", extra={"tools": extra})
    tools = sorted((t for t in offered if t.name in READ_ONLY_TOOLS), key=lambda t: t.name)
    if not tools:
        raise McpUnavailable(f"{where}: it offers none of the allowed tools")
    for tool in tools:
        # A failed call raises (by default the adapter turns it into a result), so the trace
        # marks its span as an error and ToolRetryMiddleware sees it; ToolErrorMiddleware then
        # hands the model the server's own text, just as the adapter would.
        tool.handle_tool_error = False
    return tools


@dataclass
class _Stream:
    """Turns LangGraph's stream (`tasks` + `updates`, subgraphs included) into progress events,
    remembering what it needs across parts: how often each stage started, the tool calls seen
    (a remediator tool's result carries no arguments of its own), and whether the investigator
    is in the recheck round after a fix."""

    started: Counter = field(default_factory=Counter)
    calls: dict[str | None, ToolCall] = field(default_factory=dict)  # by id (LangChain: optional)
    rechecking: bool = False

    def events(self, part: TasksStreamPart | UpdatesStreamPart) -> Iterator[ProgressEvent]:
        inner = bool(part["ns"])
        if part["type"] == "tasks":
            data = part["data"]
            if not inner and "input" in data and data["name"] in STAGES:
                name = data["name"]  # a node starts (a task's result has no "input")
                self.started[name] += 1
                self.rechecking = name == "investigate" and bool(data["input"].get("remediation"))
                yield Stage(name, self.started[name], recheck=self.rechecking)
            return
        for node, update in part["data"].items():
            if not isinstance(update, dict):
                continue
            if inner:
                yield from self.messages(update.get("messages", []))
            elif node == "investigate" and update.get("structured_response"):
                if not self.rechecking:  # the recheck's answer: see Rechecked
                    yield Drafted(update["structured_response"])
            elif node == "verify_evidence" and update.get("rounds"):
                yield Verified(update["rounds"][-1])
            elif node == "remediate":
                yield Remediated(update["remediation"])
            elif node == "recheck":
                yield Rechecked(update["final"].post_fix_check, update["remediation"])

    def messages(self, messages: list[Any]) -> Iterator[ProgressEvent]:
        for m in messages:
            if evidence_of(m) is not None:
                yield ToolCalled(steps_from_messages([m])[0])
            elif isinstance(m, AIMessage):
                self.calls.update((c["id"], c) for c in m.tool_calls)
                yield from (Reasoning(t) for t in thinking_of(m))
            elif isinstance(m, ToolMessage) and (c := self.calls.get(m.tool_call_id)):
                yield FixCalled(FixStep.of(c, m))


def _stop_reason(e: Exception, s: Settings) -> str | None:
    """Why a run stopped early, for the stops that end in inconclusive; None for a failure."""
    match e:
        case TimeoutError():
            return f"The run hit its {s.run_max_seconds:g} s time budget (AGENT_RUN_MAX_SECONDS)."
        case ModelCallLimitExceededError(thread_limit=int(budget), thread_count=n) if n >= budget:
            return f"The run-wide model-call budget ({budget}, AGENT_RUN_MAX_MODEL_CALLS) ran out."
        case ModelCallLimitExceededError(run_limit=n):
            return f"The model-call limit for a round ({n}, AGENT_MAX_MODEL_CALLS) was reached."
        case GraphRecursionError():
            return f"The run hit the graph step limit ({s.recursion_limit}, AGENT_RECURSION_LIMIT)."
        case ModelRefusalError():  # before ModelError below: it's a subclass
            return f"The model declined to answer: {e}"
        case StructuredOutputValidationError(ai_message=m) if (
            m.response_metadata.get("stop_reason") == "max_tokens"
        ):
            return "The answer was cut off at the output limit (AGENT_MAX_OUTPUT_TOKENS)."
        case StructuredOutputValidationError():
            return f"The answer didn't match the schema: {str(e.source).splitlines()[0]}"
    return None


async def _run(
    build: Callable[[], Any],
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: RunContext,
    on_event: Callable[[ProgressEvent], None],
    gate: Gate,
) -> tuple[Outcome, dict[str, Any], Asked]:
    """Build the graph and run it to the end, deciding each approval pause on the way: how it
    ended, its final state, and what was asked and decided."""
    graph = outcome = None
    asked: Asked = []
    stream, command, budget = _Stream(), inputs, context.settings.run_max_seconds
    try:
        graph = build()
        while True:
            # Two clocks: the agents' working time before and after a pause shares
            # AGENT_RUN_MAX_SECONDS; the wait for an answer has its own (approval.py).
            t0 = time.monotonic()
            modes = ["tasks", "updates"]
            parts = graph.astream(
                command, config, context=context, stream_mode=modes, subgraphs=True, version="v2"
            )
            # aclosing: an error here (in on_event, say) stops the graph's tasks now, inside the
            # run's trace, not whenever the suspended stream is collected.
            async with asyncio.timeout(budget), aclosing(parts):
                async for part in parts:
                    for event in stream.events(part):
                        on_event(event)
            budget -= time.monotonic() - t0
            state = await graph.aget_state(config)
            if not state.interrupts:
                break
            answers = {}
            final = state.values["final"]  # verified: the fix comes from it
            steps = steps_from_messages(state.values["messages"])
            evidence = _evidence_steps(final, steps, context.settings)
            for pause in state.interrupts:  # one: the remediator may request the fix once
                request, decision = await gate.decide(pause.value, final, evidence)
                asked.append((request, decision))
                on_event(Decided(request, decision))
                answers[pause.id] = approval.resume(decision)
            command = Command(resume=answers)  # the same thread: the graph goes on where it paused
    except Exception as e:
        if reason := _stop_reason(e, context.settings):
            diagnosis = forced_inconclusive(reason)
            outcome = Outcome(RunStatus.completed, diagnosis=diagnosis, forced_reason=reason)
        elif isinstance(e, ModelError):
            outcome = Outcome(RunStatus.failed, error=f"model API error: {type(e).__name__}: {e}")
        else:
            context.log.exception("run_crashed")
            outcome = Outcome(RunStatus.failed, error=f"unexpected error: {type(e).__name__}: {e}")
    if graph is None:
        return outcome, {}, asked
    # A run cut off mid-round has that round's messages only in the investigator's own
    # checkpoint, so merge them in.
    snapshot = await graph.aget_state(config, subgraphs=True)
    values = dict(snapshot.values)
    for task in snapshot.tasks:
        if task.name == "investigate" and task.state is not None:
            values["messages"] = add_messages(values["messages"], task.state.values["messages"])
    final = values.get("final")
    if outcome is None:
        reason = values.get("forced_reason")
        outcome = Outcome(RunStatus.completed, diagnosis=final, forced_reason=reason)
    elif final is not None:  # it stopped in the fix: the verified diagnosis stands
        stopped = outcome.forced_reason or outcome.error
        outcome = Outcome(RunStatus.completed, diagnosis=final, fix_stopped=stopped)
    return outcome, values, asked


def _complete_remediation(
    values: dict[str, Any], asked: Asked, stopped: str | None
) -> Remediation | None:
    """The fix as the graph recorded it, with what the person was shown and decided. A run
    stopped mid-fix (`stopped`) says so."""
    done = values.get("remediation")
    fix = values["final"].proposed_fix if stopped else None
    if stopped and done is None and fix is not None:  # in the remediator: it may have applied it
        maybe = "; it was approved, so check the Service" if asked and asked[-1][1].approved else ""
        change = Change(namespace=fix.namespace, service=fix.service, before=None,
                        after=fix.selector_dict())  # fmt: skip
        detail = f"the run stopped before the fix finished: {stopped}{maybe}"
        done = Remediation(change=change, outcome=FixOutcome.failed, detail=detail)
    elif stopped and done is not None and not done.verified:  # in the recheck
        note = f"the run stopped in the recheck: {stopped}"
        done = done.model_copy(update={"recheck_note": note})
    if done is not None and asked:
        request, decision = asked[-1]
        change = done.change
        if change.before is None:  # the fix tool never ran: the selector read at the pause
            change = change.model_copy(update={"before": request.facts.current_selector})
        done = done.model_copy(update={"request": request, "decision": decision, "change": change})
    return done


def _evidence_steps(d: Diagnosis | None, steps: list[Step], s: Settings) -> list[EvidenceStep]:
    """The tool call behind each evidence ID `d` cites, in citation order, with an excerpt."""
    by_ref = {step.ref: step for step in steps}
    return [
        EvidenceStep(
            ref=ref,
            tool=step.tool,
            args=step.args,
            error=step.error,
            output_excerpt=cap(step.output, s.evidence_excerpt_chars),
        )
        for ref in dict.fromkeys(d.refs if d else [])
        if (step := by_ref.get(ref))
    ]


def _close_if_loaded(load_kube: Any) -> None:
    """At the end of a run, the remediator's client, if the run loaded it (`load_kube`: cached)."""
    if load_kube.cache_info().currsize:
        load_kube().close()


async def investigate(
    question: str,
    *,
    settings: Settings,
    namespace: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    on_event: Callable[[ProgressEvent], None] = lambda _: None,
    approve: Approver | None = None,
    model: BaseChatModel | None = None,
    critic_model: BaseChatModel | None = None,
    remediator_model: BaseChatModel | None = None,
    mcp_target: Any = None,
    remediator_kube: Callable[[], Kube] | None = None,
) -> RunRecord:
    """Investigate `question` and return the full run record. `approve` answers a fix request
    (the CLI asks in the terminal); without it a request is rejected, "no interactive approval
    available". The models, `mcp_target` (any target `MCPAdapter` accepts) and
    `remediator_kube` (the remediator's cluster access) default to the settings; tests pass
    fakes."""
    run_id, thread_id = str(uuid4()), thread_id or str(uuid4())
    user_id = user_id or settings.effective_user_id
    log = run_log(run_id=run_id, thread_id=thread_id, user_id=user_id)
    started_at, t0 = datetime.now(UTC), time.monotonic()
    log.info("run_started", extra={"question": question, "namespace": namespace})
    inputs = {"question": question, "namespace": namespace}
    # The whole run is one trace: its duration is the run's, and a run that can't start (no
    # models, no MCP server) is an error trace saying why.
    with tracing.run(
        "investigation",
        {**inputs, "user_id": user_id, "thread_id": thread_id},
        user=user_id,
        session=thread_id,
        metadata={"run_id": run_id, **settings.model_settings},
    ) as span:
        try:
            model = model or make_model(settings, settings.model)
            critic_llm = critic_model or make_model(settings, settings.effective_critic_model)
            critic = with_verdict(critic_llm)
            remediator_llm = remediator_model or make_model(
                settings, settings.effective_remediator_model
            )
        except Exception as e:
            log.error("model_unavailable", extra={"error": f"{type(e).__name__}: {e}"})
            raise NotStarted(f"cannot create the models: {type(e).__name__}: {e}") from e
        # Loaded on first use: a run that proposes no fix never needs it.
        load_own_kube = cache(
            lambda: Kube.load(settings.remediator_kubeconfig, settings.remediator_timeout_seconds)
        )
        load_kube = remediator_kube or load_own_kube
        context = RunContext(settings=settings, log=log, critic=critic)
        gate = Gate(settings, load_kube, approve, user_id, log)
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {"run_id": run_id, "user_id": user_id},
            "recursion_limit": settings.recursion_limit,
            "run_name": "graph",  # its trace span (default: "LangGraph")
        }
        async with AsyncExitStack() as stack:
            stack.callback(_close_if_loaded, load_own_kube)
            tools = await _load_tools(stack, mcp_target or mcp_client(settings), settings, log)
            build = lambda: build_graph(model, tools, settings, remediator_llm, load_kube)  # noqa: E731
            outcome, values, asked = await _run(build, inputs, config, context, on_event, gate)

        remediation = _complete_remediation(values, asked, outcome.fix_stopped)
        steps = steps_from_messages(values.get("messages", []))
        result = RunResult(
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            trace_id=span.trace_id if span else None,
            question=question,
            namespace=namespace,
            status=outcome.status,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=round((time.monotonic() - t0) * 1000),
            diagnosis=outcome.diagnosis,
            error=outcome.error,
            evidence_steps=_evidence_steps(outcome.diagnosis, steps, settings),
            verification=values.get("rounds", []),
            remediation=remediation,
        )
        tracing.finish(span, result.model_dump(mode="json"), result.error)
    level = "error" if outcome.status is RunStatus.failed else "info"
    getattr(log, level)(
        "run_finished",
        extra={
            "status": outcome.status,
            "diagnosis": outcome.diagnosis.status if outcome.diagnosis else None,
            "forced_reason": outcome.forced_reason,
            "error": outcome.error,
            "duration_ms": result.duration_ms,
            "tool_calls": len(steps),
            "verify_rounds": len(result.verification),
            "fix": remediation.outcome if remediation else None,
            "fix_verified": remediation.verified if remediation else None,
        },
    )
    return RunRecord(result=result, steps=steps, forced_reason=outcome.forced_reason)
