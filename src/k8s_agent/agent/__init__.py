"""The agent: an investigator, a critic and a remediator in one graph. The CLI, the evals and the
API call `investigate()`; nothing here knows about any of them."""

from k8s_agent.agent.approval import Approver
from k8s_agent.agent.core import McpUnavailable, NotStarted, investigate
from k8s_agent.agent.records import (
    Answer,
    ApprovalRequest,
    Decided,
    Drafted,
    FixCalled,
    ProgressEvent,
    Reasoning,
    Rechecked,
    Remediated,
    RunRecord,
    RunStatus,
    Stage,
    ToolCalled,
    Verified,
)

__all__ = [
    "Answer",
    "ApprovalRequest",
    "Approver",
    "Decided",
    "Drafted",
    "FixCalled",
    "McpUnavailable",
    "NotStarted",
    "ProgressEvent",
    "Reasoning",
    "Rechecked",
    "Remediated",
    "RunRecord",
    "RunStatus",
    "Stage",
    "ToolCalled",
    "Verified",
    "investigate",
]
