"""The run record (SPEC §3.7) and the result JSON (SPEC §4.2). `investigate()` returns the
full record in memory; the CLI prints `RunRecord.result`, which carries only the cited steps.
Steps are read back from the agent's messages, never tracked separately."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage
from pydantic import BaseModel

from k8s_agent.agent.tool_output import untag
from k8s_agent.schemas import Diagnosis, FindingVerdict

EVIDENCE = "evidence"  # the key of our record in a ToolMessage's artifact


class RunStatus(StrEnum):
    completed = "completed"  # a diagnosis, including a forced inconclusive
    failed = "failed"  # no diagnosis: a model API error or a bug; see `error`


class Step(BaseModel):
    """One tool call. `output` is exactly what the model saw, without the evidence tag."""

    ref: str
    tool: str
    args: dict[str, Any]
    output: str
    error: bool
    latency_ms: int
    truncated: bool


class VerifyRound(BaseModel):
    """One pass of verify_evidence: code checks, then (if they pass) the critic's verdict."""

    round: int
    founded: bool
    problems: list[str]  # code checks that failed; the critic isn't asked then
    findings: list[FindingVerdict]
    proposed_fix_backed: bool | None  # None: no fix proposed, or the critic wasn't asked
    feedback: str | None  # what the investigator is told when another round follows


class EvidenceStep(BaseModel):
    """The tool call behind a cited ref."""

    ref: str
    tool: str
    args: dict[str, Any]
    error: bool
    output_excerpt: str


class RunResult(BaseModel):
    run_id: str
    thread_id: str
    user_id: str
    question: str
    namespace: str | None
    status: RunStatus
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    diagnosis: Diagnosis | None
    error: str | None
    evidence_steps: list[EvidenceStep]
    verification: list[VerifyRound]


class RunRecord(BaseModel):
    result: RunResult
    steps: list[Step]  # every tool call, cited or not
    forced_reason: str | None = None  # why code replaced the answer with inconclusive


def evidence_of(message: BaseMessage) -> dict[str, Any] | None:
    if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
        return message.artifact.get(EVIDENCE)
    return None


def steps_from_messages(messages: list[BaseMessage]) -> list[Step]:
    """Every tool call the investigator made, in evidence-ID order."""
    steps = [
        Step(
            ref=ev["ref"],
            tool=m.name or "?",
            args=ev["args"],
            output=untag(ev["ref"], m.text),
            error=m.status == "error",
            latency_ms=ev["latency_ms"],
            truncated=ev["truncated"],
        )
        for m in messages
        if (ev := evidence_of(m)) is not None
    ]
    return sorted(steps, key=lambda s: int(s.ref[1:]))


def thinking_of(message: BaseMessage) -> list[str]:
    """The model's reasoning summaries in a reply, when it thought and returned them."""
    blocks = message.content_blocks
    return [
        b["reasoning"].strip() for b in blocks if b["type"] == "reasoning" and b.get("reasoning")
    ]


# --- live progress (core -> CLI) ---------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    name: str  # a graph node: intake | investigate | verify_evidence | result
    round: int


@dataclass(frozen=True)
class ToolCalled:
    step: Step


@dataclass(frozen=True)
class Reasoning:
    text: str


@dataclass(frozen=True)
class Drafted:
    """The investigator's answer at the end of a round, before it is verified."""

    diagnosis: Diagnosis


@dataclass(frozen=True)
class Verified:
    round: VerifyRound


ProgressEvent = Stage | ToolCalled | Reasoning | Drafted | Verified
