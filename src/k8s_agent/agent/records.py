"""The agent's records, all plain data:
- the run record and the result JSON (DESIGN 2, "What comes back"): `investigate()` returns the full
  `RunRecord`; the CLI prints `RunRecord.result`, which carries only the cited steps. Steps are
  read back from the agent's messages, never tracked separately;
- the fix (DESIGN 3.6): its arguments, the approval request and decision, and its outcome;
- the live progress events `investigate()` sends its caller (`ProgressEvent`)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from langchain_core.messages import BaseMessage, ToolCall, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from k8s_agent.agent.tool_output import untag
from k8s_agent.schemas import Diagnosis, FindingVerdict, PostFixCheck

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


# --- the fix (DESIGN 3.6) --------------------------------------------------------------------


class FixArgs(BaseModel):
    """patch_service_selector's arguments: exactly what runs once a person approves."""

    model_config = ConfigDict(extra="forbid")

    namespace: str
    service: str
    selector: dict[str, str] = Field(description="The Service's complete new selector")
    resource_version: str = Field(
        description="The Service's metadata.resourceVersion as you read it; the fix is refused "
        "if the Service changed since"
    )
    explanation: str = Field(
        description="For the person who approves: what changes, and the effect you expect"
    )


class Facts(BaseModel):
    """Read by code, as the remediator, while the run waits for approval."""

    current_selector: dict[str, str] | None = None
    resource_version: str | None = None
    # The ready pods the exact new selector matches, each with its managing controller
    # (`ReplicaSet/checkout-69db948c9f`); None: no controller manages it (a standalone pod,
    # perhaps planted to receive the traffic).
    owners: dict[str, str | None] = {}
    error: str | None = None  # why they couldn't be read


class ApprovalRequest(BaseModel):
    """The change the person is shown before deciding, after the verified diagnosis it comes
    from: the call exactly as it would run (FixArgs, once the gate has checked it), and facts
    read at the pause."""

    tool: str
    args: dict[str, Any]
    facts: Facts


@dataclass(frozen=True)
class Answer:
    """An approver's answer, and how it was given (recorded with the decision)."""

    approved: bool
    reason: str  # how it was given, e.g. "answered yes in the terminal"


class Decision(BaseModel):
    approved: bool
    reason: str  # how: in the terminal, timed out, no interactive approval available, ...
    by: str  # the user who answered, or the agent when code decided
    at: datetime


class FixOutcome(StrEnum):
    applied = "applied"
    stale = "stale"  # the Service changed after the remediator read it: nothing applied
    invalid = "invalid"  # validation failed: nothing applied
    failed = "failed"  # an API error, or the run stopped mid-fix: see `detail`
    not_applied = "not_applied"  # the remediator declined, or the request was rejected


class Change(BaseModel):
    namespace: str
    service: str
    before: dict[str, str] | None  # the selector the fix found; None if it never read it
    after: dict[str, str]  # the selector requested (or proposed, if it never was)


class FixStep(BaseModel):
    """One remediator tool call."""

    tool: str
    args: dict[str, Any]
    output: str
    error: bool

    @classmethod
    def of(cls, call: ToolCall, result: ToolMessage) -> FixStep:
        """The call, with the result it got."""
        return cls(
            tool=call["name"], args=call["args"], output=result.text, error=result.status == "error"
        )


class Remediation(BaseModel):
    """The fix: the change, what the person was shown and decided, what happened, and whether
    a recheck with fresh evidence confirmed it."""

    change: Change
    request: ApprovalRequest | None = None  # None: nobody was asked
    decision: Decision | None = None
    outcome: FixOutcome
    detail: str  # the fix tool's report, or why the remediator declined or stopped
    endpoints_ready: int = 0  # counted by the fix tool after applying
    verified: bool = False  # applied, endpoints ready, and the recheck says resolved
    recheck_note: str | None = None  # why the recheck couldn't confirm it
    steps: list[FixStep] = []


class RunResult(BaseModel):
    run_id: str
    thread_id: str
    user_id: str
    trace_id: str | None = None  # the run's MLflow trace; None when tracing is off
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
    remediation: Remediation | None = None


class RunRecord(BaseModel):
    result: RunResult
    steps: list[Step]  # every tool call, cited or not
    forced_reason: str | None = None  # why code replaced the answer with inconclusive


def evidence_of(message: BaseMessage) -> dict[str, Any] | None:
    if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
        return message.artifact.get(EVIDENCE)
    return None


def steps_from_messages(messages: Sequence[BaseMessage]) -> list[Step]:
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
        if isinstance(m, ToolMessage) and (ev := evidence_of(m)) is not None
    ]
    return sorted(steps, key=lambda s: int(s.ref[1:]))


def thinking_of(message: BaseMessage) -> list[str]:
    """The model's reasoning summaries in a reply, when it thought and returned them."""
    blocks = message.content_blocks
    return [r.strip() for b in blocks if b["type"] == "reasoning" and (r := b.get("reasoning"))]


# --- live progress (core -> CLI) ---------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    name: str  # a graph node: intake | investigate | verify_evidence | result | remediate | recheck
    round: int
    recheck: bool = False  # investigate: the round that rechecks a fix


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


@dataclass(frozen=True)
class FixCalled:
    """A remediator tool call, with its result."""

    step: FixStep


@dataclass(frozen=True)
class Decided:
    request: ApprovalRequest
    decision: Decision


@dataclass(frozen=True)
class Remediated:
    """The remediator is done: the fix's outcome."""

    remediation: Remediation


@dataclass(frozen=True)
class Rechecked:
    """The recheck is done: what it found, and whether the fix is verified."""

    check: PostFixCheck | None
    remediation: Remediation


ProgressEvent = (
    Stage
    | ToolCalled
    | Reasoning
    | Drafted
    | Verified
    | FixCalled
    | Decided
    | Remediated
    | Rechecked
)
