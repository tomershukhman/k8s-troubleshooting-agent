"""Shared diagnosis vocabulary (SPEC §4.2). The full Investigation schema is added in M3."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DiagnosisStatus(StrEnum):
    fault_found = "fault_found"
    no_fault = "no_fault"
    inconclusive = "inconclusive"  # set only by code (limit reached, refusal, error)


class FaultCategory(StrEnum):
    """Root cause, not symptom (SPEC §2.2)."""

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


class ProposedFix(BaseModel):
    """The only fix type the agent may propose (SPEC §3.4)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["patch_service_selector"]
    namespace: str = Field(min_length=1)
    service: str = Field(min_length=1)
    selector: dict[str, str] = Field(min_length=1)
