"""The gate between the approval pause and the resume (SPEC 3.4).

When the remediator's fix tool pauses the graph, `Gate.decide`:
1. reads facts as the remediator: the Service's current selector and version, and the ready
   pods the exact new selector matches;
2. rejects at once a request that is malformed or differs from the investigator's proposed
   fix (the one the critic checked), so nobody is asked to approve anything else;
3. otherwise asks the approver the caller gave (the CLI's y/N prompt, or the evals), with the
   verified diagnosis the fix comes from and the tool calls behind its evidence. The wait has
   its own clock: no answer within AGENT_APPROVAL_TIMEOUT_SECONDS is a reject. No approver (no
   interactive terminal) is a reject too.

The decision goes on the trace, as an `approval` span (what was shown, then the answer, who and
when), and into the result.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from k8s_agent import tracing
from k8s_agent.agent.records import (
    Answer,
    ApprovalRequest,
    Decision,
    EvidenceStep,
    Facts,
    FixArgs,
)
from k8s_agent.agent.remediator import KUBE_ERRORS, Kube, labels
from k8s_agent.config import Settings
from k8s_agent.kube import KubeError
from k8s_agent.schemas import Diagnosis, ProposedFix

NO_APPROVER = "no interactive approval available"
AGENT = "k8s-agent"  # who decided, when it was code and not a person

# (the request, the verified diagnosis, the tool calls behind its evidence) -> the answer
Approver = Callable[[ApprovalRequest, Diagnosis, list[EvidenceStep]], Awaitable[Answer]]


def read_facts(load_kube: Callable[[], Kube], args: FixArgs) -> Facts:
    try:
        k = load_kube()
        svc = k.service(args.namespace, args.service)
        pods = k.ready_pods(args.namespace, args.selector)
    except (KubeError, *KUBE_ERRORS) as e:
        return Facts(error=str(e))
    return Facts(
        current_selector=svc.spec.selector,
        resource_version=svc.metadata.resource_version,
        ready_pods=pods,
    )


def differs(args: FixArgs, fix: ProposedFix) -> str | None:
    if (args.namespace, args.service, args.selector) == (
        fix.namespace,
        fix.service,
        fix.selector_dict(),
    ):
        return None
    return (
        f"the request ({args.namespace}/{args.service}: {labels(args.selector)}) differs from "
        f"the proposed fix ({fix.namespace}/{fix.service}: {labels(fix.selector_dict())})"
    )


def resume(decision: Decision) -> dict[str, str]:
    """The answer for HumanInTheLoopMiddleware: approve, or reject with the reason (the
    remediator reads it)."""
    if decision.approved:
        return {"type": "approve"}
    return {"type": "reject", "message": decision.reason}


@dataclass(frozen=True)
class Gate:
    settings: Settings
    load_kube: Callable[[], Kube]
    approve: Approver | None
    user_id: str
    log: logging.LoggerAdapter

    async def decide(
        self, pause: dict[str, Any], final: Diagnosis, evidence: list[EvidenceStep]
    ) -> tuple[ApprovalRequest, Decision]:
        """`pause`: the middleware's interrupt value, with the tool call exactly as it would
        run. `final`: the verified diagnosis that proposed the fix; `evidence`: the tool calls
        behind it. The approver gets both, to show what the fix is based on."""
        request = ApprovalRequest(tool=pause["name"], args=pause["args"], facts=Facts())
        try:
            args = FixArgs.model_validate(pause["args"])
        except ValidationError as e:  # the middleware pauses before the tool checks its input
            problem = "the request is malformed: " + "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
            )
        else:
            facts = await asyncio.to_thread(read_facts, self.load_kube, args)
            request = request.model_copy(update={"facts": facts})
            problem = differs(args, final.proposed_fix)
        with tracing.span("approval", request.model_dump(mode="json"), "GUARDRAIL") as span:
            if problem:  # nobody is asked to approve it
                approved, reason, by = False, problem, AGENT
            else:
                approved, reason, by = await self._answer(request, final, evidence)
            decision = Decision(approved=approved, reason=reason, by=by, at=datetime.now(UTC))
            tracing.finish(span, decision.model_dump(mode="json"), None)
        self.log.info("fix_decided", extra=decision.model_dump(mode="json"))
        return request, decision

    async def _answer(
        self, request: ApprovalRequest, final: Diagnosis, evidence: list[EvidenceStep]
    ) -> tuple[bool, str, str]:
        """(approved, how, by whom), from the approver, within the approval's own time limit."""
        if self.approve is None:
            return False, NO_APPROVER, AGENT
        limit = self.settings.approval_timeout_seconds
        try:
            async with asyncio.timeout(limit):
                answer = await self.approve(request, final, evidence)
        except TimeoutError:
            return False, f"the approval timed out after {limit:g} s", AGENT
        return answer.approved, answer.how, self.user_id
