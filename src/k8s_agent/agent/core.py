"""`investigate()`: the one entry point the CLI (and later the evals and the API) call.

It runs the investigation graph (graph.py) on its own thread and never raises for a run that
started; every outcome becomes a `RunRecord`:
- a diagnosis that passed verification (`fault_found`, `no_fault` or `inconclusive`)
- an `inconclusive` forced by code: a limit or budget, a refusal, an invalid answer, or
  evidence that stayed unfounded after the last verify round
- `failed`: a model API error, or a bug
Only `NotStarted` is raised (e.g. `McpUnavailable`): nothing ran.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastmcp import Client as FastMCPClient
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain.chat_models import init_chat_model
from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.exceptions import ModelError, ModelRefusalError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import add_messages

from k8s_agent.agent.critic import with_verdict
from k8s_agent.agent.graph import build_graph, forced
from k8s_agent.agent.middleware import RunContext
from k8s_agent.agent.records import (
    Drafted,
    EvidenceStep,
    ProgressEvent,
    Reasoning,
    RunRecord,
    RunResult,
    RunStatus,
    Stage,
    ToolCalled,
    Verified,
    evidence_of,
    steps_from_messages,
    thinking_of,
)
from k8s_agent.agent.tool_output import cap
from k8s_agent.config import Settings
from k8s_agent.logs import run_log
from k8s_agent.policy import READ_ONLY_TOOLS
from k8s_agent.schemas import Diagnosis

# langchain.mcp is beta and says so on import; it's the MCP client we chose (SPEC 3.2).
with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

STAGES = ("intake", "investigate", "verify_evidence", "result")


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
    return tools


def _events(part: dict[str, Any], started: Counter) -> Iterator[ProgressEvent]:
    """Progress events from one part of LangGraph's stream (`tasks` + `updates`, subgraphs
    included)."""
    data, inner = part["data"], bool(part["ns"])
    if part["type"] == "tasks" and not inner and "input" in data and data["name"] in STAGES:
        started[data["name"]] += 1  # a node starts (a task's result has no "input")
        yield Stage(data["name"], started[data["name"]])
    if part["type"] != "updates":
        return
    for node, update in data.items():
        if not isinstance(update, dict):
            continue
        if node == "investigate" and not inner and update.get("structured_response"):
            yield Drafted(update["structured_response"])
        if node == "verify_evidence" and not inner and update.get("rounds"):
            yield Verified(update["rounds"][-1])
        for m in update.get("messages", []) if inner else []:
            if evidence_of(m) is not None:
                yield ToolCalled(steps_from_messages([m])[0])
            elif isinstance(m, AIMessage):
                yield from (Reasoning(t) for t in thinking_of(m))


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
    model: BaseChatModel,
    tools: list[BaseTool],
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: RunContext,
    on_event: Callable[[ProgressEvent], None],
) -> tuple[Outcome, dict[str, Any]]:
    """Run the graph to the end: how it ended, and its final state."""
    graph = outcome = None
    try:
        graph = build_graph(model, tools, context.settings)
        started: Counter = Counter()
        async with asyncio.timeout(context.settings.run_max_seconds):
            async for part in graph.astream(
                inputs, config, context=context, stream_mode=["tasks", "updates"],
                subgraphs=True, version="v2",
            ):  # fmt: skip
                for event in _events(part, started):
                    on_event(event)
    except Exception as e:
        if reason := _stop_reason(e, context.settings):
            outcome = Outcome(RunStatus.completed, forced(reason), None, reason)
        elif isinstance(e, ModelError):
            outcome = Outcome(RunStatus.failed, error=f"model API error: {type(e).__name__}: {e}")
        else:
            context.log.exception("run_crashed")
            outcome = Outcome(RunStatus.failed, error=f"unexpected error: {type(e).__name__}: {e}")
    if graph is None:
        return outcome, {}
    # A run cut off mid-round has that round's messages only in the investigator's own
    # checkpoint, so merge them in.
    snapshot = await graph.aget_state(config, subgraphs=True)
    values = dict(snapshot.values)
    for task in snapshot.tasks:
        if task.name == "investigate" and task.state is not None:
            values["messages"] = add_messages(values["messages"], task.state.values["messages"])
    final = values.get("final")
    return outcome or Outcome(RunStatus.completed, final, None, values.get("forced_reason")), values


async def investigate(
    question: str,
    *,
    settings: Settings,
    namespace: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    on_event: Callable[[ProgressEvent], None] = lambda _: None,
    model: BaseChatModel | None = None,
    critic_model: BaseChatModel | None = None,
    mcp_target: Any = None,
) -> RunRecord:
    """Investigate `question` and return the full run record. The models and `mcp_target`
    (any target `MCPAdapter` accepts) default to the settings; tests pass fakes."""
    run_id, thread_id = str(uuid4()), thread_id or str(uuid4())
    user_id = user_id or settings.effective_user_id
    log = run_log(run_id=run_id, thread_id=thread_id, user_id=user_id)
    started_at, t0 = datetime.now(UTC), time.monotonic()
    log.info("run_started", extra={"question": question, "namespace": namespace})
    try:
        model = model or make_model(settings, settings.model)
        critic = with_verdict(critic_model or make_model(settings, settings.effective_critic_model))
    except Exception as e:
        log.error("model_unavailable", extra={"error": f"{type(e).__name__}: {e}"})
        raise NotStarted(f"cannot create the models: {type(e).__name__}: {e}") from e
    context = RunContext(settings=settings, log=log, critic=critic)
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"run_id": run_id, "user_id": user_id},
        "recursion_limit": settings.recursion_limit,
    }
    async with AsyncExitStack() as stack:
        tools = await _load_tools(stack, mcp_target or mcp_client(settings), settings, log)
        inputs = {"question": question, "namespace": namespace}
        outcome, values = await _run(model, tools, inputs, config, context, on_event)

    steps = steps_from_messages(values.get("messages", []))
    by_ref = {s.ref: s for s in steps}
    cited = dict.fromkeys(outcome.diagnosis.refs if outcome.diagnosis else [])
    result = RunResult(
        run_id=run_id,
        thread_id=thread_id,
        user_id=user_id,
        question=question,
        namespace=namespace,
        status=outcome.status,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_ms=round((time.monotonic() - t0) * 1000),
        diagnosis=outcome.diagnosis,
        error=outcome.error,
        evidence_steps=[
            EvidenceStep(
                ref=s.ref,
                tool=s.tool,
                args=s.args,
                error=s.error,
                output_excerpt=cap(s.output, settings.evidence_excerpt_chars),
            )
            for ref in cited
            if (s := by_ref.get(ref))
        ],  # fmt: skip
        verification=values.get("rounds", []),
    )
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
        },
    )
    return RunRecord(result=result, steps=steps, forced_reason=outcome.forced_reason)
