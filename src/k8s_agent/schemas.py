"""The diagnosis vocabulary and the agents' answer schemas (DESIGN 2, "What comes back").

Only what Claude's structured output enforces lives here: types, enums, required fields. The
consistency rules (no_fault ↔ category none, evidence required, ...) are code checks
(graph.code_checks, run by verify_evidence), which send the investigator feedback instead of
discarding its answer.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiagnosisStatus(StrEnum):
    fault_found = "fault_found"
    no_fault = "no_fault"
    inconclusive = "inconclusive"  # the agent couldn't tell; code also forces it (limits, refusal)


class FaultCategory(StrEnum):
    image_pull = "image_pull"
    crash_on_start = "crash_on_start"
    bad_config = "bad_config"
    probe_failure = "probe_failure"
    selector_mismatch = "selector_mismatch"
    oom = "oom"
    unschedulable = "unschedulable"
    quota = "quota"
    network_policy = "network_policy"
    rbac = "rbac"
    none = "none"
    other = "other"


# One line each, for the investigator's system prompt.
CATEGORY_DEFINITIONS = {
    "image_pull": "the container image can't be pulled (wrong name or tag, registry access)",
    "crash_on_start": "the container exits on start for a reason other than its configuration "
    "(a bug, a wrong command)",
    "bad_config": "configuration is wrong or missing (a ConfigMap key, an environment variable, "
    "a mounted file, an argument), even if the symptom is a crash",
    "probe_failure": "a readiness, liveness or startup probe checks something the app doesn't "
    "serve, or is misconfigured",
    "selector_mismatch": "a selector doesn't match the labels of the pods it should select",
    "oom": "a container is killed for exceeding its memory limit",
    "unschedulable": "pods can't be scheduled (resources, node selectors, taints, affinity)",
    "quota": "a ResourceQuota or LimitRange blocks the workload",
    "network_policy": "a NetworkPolicy blocks the traffic",
    "rbac": "the workload's own ServiceAccount lacks permissions it needs (your own access being "
    "refused is not this: answer inconclusive)",
    "none": "no fault; with inconclusive, nothing points to a category",
    "other": "a root cause none of the categories above describes",
}
CATEGORY_HELP = "\n".join(f"- {c}: {d}" for c, d in CATEGORY_DEFINITIONS.items())


class AffectedResource(Strict):
    kind: str = Field(description="e.g. Service, Deployment, ConfigMap")
    namespace: str
    name: str


class Evidence(Strict):
    ref: str = Field(description="The evidence ID of the tool result, e.g. E3")
    finding: str = Field(description="What that tool result shows, in one sentence")


class SelectorLabel(Strict):
    key: str
    value: str


class ProposedFix(Strict):
    """The one executable fix: the remediator requests it after the diagnosis, and it runs only
    if a person approves. The selector is a list of labels: structured output can't express a
    free-form map."""

    namespace: str
    service: str
    selector: list[SelectorLabel] = Field(
        min_length=1, description="The complete new selector: every label it should have"
    )
    explanation: str = Field(description="Why this selector fixes the root cause")

    def selector_dict(self) -> dict[str, str]:
        return {label.key: label.value for label in self.selector}


class PostFixCheck(Strict):
    """The recheck after a fix, from fresh tool calls."""

    resolved: bool = Field(description="Does the fresh evidence show the problem is gone?")
    evidence: list[Evidence] = Field(
        description="Findings citing only the tool calls made in this recheck"
    )


class Diagnosis(Strict):
    """The final diagnosis. Cite evidence IDs (E1, E2, …) from the tool results."""

    status: DiagnosisStatus
    fault_category: FaultCategory
    affected_resource: AffectedResource | None = Field(description="null for no_fault")
    root_cause: str = Field(
        description="The root cause in one or two sentences; for inconclusive, why you can't tell"
    )
    evidence: list[Evidence] = Field(description="At least one for fault_found and no_fault")
    recommended_action: str = Field(
        description="The next step for a human operator: the smallest change that fixes it"
    )
    proposed_fix: ProposedFix | None = Field(
        description="Only for a Service selector that doesn't match its pods, in a namespace "
        "where fixes are allowed; otherwise null"
    )
    post_fix_check: PostFixCheck | None = Field(
        description="Only when asked to check again after a fix; otherwise null"
    )

    @field_validator("status", "fault_category", mode="before")
    @classmethod
    def _lowercase(cls, v: Any) -> Any:
        # Anthropic's docs: an enum value may come back in another capitalisation.
        return v.strip().lower() if isinstance(v, str) else v

    @property
    def refs(self) -> list[str]:
        """Every evidence ID the answer cites, the recheck's included."""
        rechecked = self.post_fix_check.evidence if self.post_fix_check else []
        return [e.ref for e in [*self.evidence, *rechecked]]


class FindingVerdict(Strict):
    ref: str = Field(description="The finding's evidence ID, e.g. E3")
    supported: bool = Field(description="Does the cited tool output actually show the finding?")
    reason: str


class FixVerdict(Strict):
    backed: bool
    reason: str


class CriticVerdict(Strict):
    """The critic's judgement of a draft diagnosis, from the cited tool outputs only."""

    findings: list[FindingVerdict] = Field(description="One entry per finding, same order")
    proposed_fix: FixVerdict | None = Field(description="null when the draft proposes no fix")
    founded: bool
    feedback: str = Field(description="Empty when founded")


class RemediatorAnswer(Strict):
    """The remediator's final answer: after its fix request, or instead of one. Whether it
    requested the fix is in its tool calls; this says why it didn't, or what happened."""

    reason: str = Field(
        description="Why you declined; otherwise what the fix tool reported, or that the "
        "request was rejected"
    )
