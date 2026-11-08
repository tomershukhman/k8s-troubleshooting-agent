"""The investigator's tool-result middleware (SPEC §3.3). Each result is stripped of noise,
capped, and tagged `[E3]`; the evidence record (ID, effective arguments, timing) goes into the
ToolMessage's `artifact`, so the messages are the record of every step."""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from k8s_agent.agent import tool_output
from k8s_agent.agent.records import EVIDENCE, evidence_of
from k8s_agent.config import Settings


@dataclass(frozen=True)
class RunContext:
    """Per-run dependencies, given to every node and middleware through LangGraph's runtime
    context (`context_schema=RunContext`)."""

    settings: Settings
    log: logging.LoggerAdapter
    critic: Any  # the critic model with structured output (critic.with_verdict)


def next_ref(messages: list[BaseMessage], call_id: str) -> str:
    """The evidence ID for tool call `call_id`: one counter per run (tagged results so far).
    Parallel calls from one model turn all see the same messages, so each adds its position
    among that turn's calls still waiting for a result."""
    tagged = sum(evidence_of(m) is not None for m in messages)
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    turn = next(
        m
        for m in reversed(messages)
        if isinstance(m, AIMessage) and any(c["id"] == call_id for c in m.tool_calls)
    )
    pending = [c["id"] for c in turn.tool_calls if c["id"] not in answered]
    return f"E{tagged + pending.index(call_id) + 1}"


def bounded_args(settings: Settings, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """pods_log returns at most AGENT_LOG_TAIL_LINES lines, whatever the model asked for."""
    if tool != "pods_log":
        return args
    try:
        tail = int(args.get("tail") or settings.log_tail_lines)
    except TypeError, ValueError:
        tail = settings.log_tail_lines
    return {**args, "tail": min(max(tail, 1), settings.log_tail_lines)}


class RetryableToolError(Exception):
    """A tool error result a retry may fix, raised so that ToolRetryMiddleware retries it."""

    def __init__(self, result: ToolMessage) -> None:
        super().__init__(result.text)
        self.result = result


class RetryToolErrors(AgentMiddleware):
    """The MCP server reports a failed Kubernetes call as an error result, not an exception,
    so ToolRetryMiddleware (outside this one) would never retry it. Raise for every error
    except those a retry can't fix (tool_output.PERMANENT): a container restarting, an API
    server timeout and errors not seen before are retried; a refusal goes straight back."""

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if (
            request.tool is not None  # None: a tool the model made up; nothing ran
            and isinstance(result, ToolMessage)
            and result.status == "error"
            and not tool_output.PERMANENT.search(result.text)
        ):
            raise RetryableToolError(result)
        return result


def retries_exhausted(attempts: int) -> Callable[[Exception], str]:
    """What the model sees when every attempt failed: the server's own error text, or the
    exception for a call that failed below the tool (timeout, transport)."""

    def message(exc: Exception) -> str:
        if isinstance(exc, RetryableToolError):
            return exc.result.text
        return f"The call failed after {attempts} attempts: {type(exc).__name__}: {exc}"

    return message


class EvidenceMiddleware(AgentMiddleware):
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        ctx: RunContext = request.runtime.context
        call = request.tool_call
        ref = next_ref(request.state["messages"], call["id"])
        args = bounded_args(ctx.settings, call["name"], call["args"])
        if args != call["args"]:
            request = request.override(tool_call={**call, "args": args})
        t0 = time.monotonic()
        result = await handler(request)  # a failed call comes back as a tool error (ToolRetry)
        if not isinstance(result, ToolMessage):
            return result
        latency_ms = round((time.monotonic() - t0) * 1000)
        settings = ctx.settings
        text = tool_output.strip_fields(result.text, settings.tool_output_strip_fields)
        capped = tool_output.cap(text, settings.tool_output_max_chars)
        error = result.status == "error"
        ctx.log.log(
            logging.WARNING if error else logging.INFO,
            "tool_call",
            extra={"ref": ref, "tool": call["name"], "tool_args": args, "latency_ms": latency_ms,
                   "error": capped if error else None},
        )  # fmt: skip
        evidence = {"ref": ref, "args": args, "latency_ms": latency_ms, "truncated": capped != text}
        return result.model_copy(
            update={
                "content": tool_output.tag(ref, capped),
                "artifact": {EVIDENCE: evidence, "mcp": result.artifact},
            }
        )
