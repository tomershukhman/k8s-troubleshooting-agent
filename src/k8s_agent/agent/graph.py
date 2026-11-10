"""The investigation workflow, an explicit LangGraph graph:

    intake -> investigate -> verify_evidence -> result -> remediate -> investigate -> recheck
                   ^               |               |          |        (a round that
                   +-- feedback ---+               +-> end    +-> end   rechecks the fix)

- investigate: the investigator agent (investigator.py), added directly as a node. It shares
  `messages` and `structured_response` with this graph and is compiled with per-thread
  persistence, so a feedback round, and the recheck round after a fix, continue its own
  conversation and its call-limit counters carry over (their `thread_limit` is the run-wide
  budget).
- verify_evidence: code checks, then the critic (no tools) on the cited outputs only.
- result: the final diagnosis, or a forced inconclusive with the reason; then the routing (code):
  only a proposed fix of the supported type, in an allowed namespace, goes on.
- remediate: the remediator agent (remediator.py) on that fix. Its fix tool pauses the whole
  graph for a person's approval; the caller resumes it on the same thread. An applied fix sends
  the investigator a recheck request.
- recheck (code, no critic): the fix is verified only if the recheck round's `post_fix_check`
  says resolved, citing only its own fresh tool calls, and the fix tool counted ready endpoints.
"""

import operator
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents.middleware import InputAgentState
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.exceptions import ModelError, OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from k8s_agent import prompts
from k8s_agent.agent import tool_output
from k8s_agent.agent.critic import review
from k8s_agent.agent.investigator import build_investigator
from k8s_agent.agent.middleware import RunContext
from k8s_agent.agent.records import (
    Change,
    FixOutcome,
    FixStep,
    Remediation,
    Step,
    VerifyRound,
    steps_from_messages,
)
from k8s_agent.agent.remediator import (
    Remediator,
    build_remediator,
    fix_request,
    read_remediation,
)
from k8s_agent.config import Settings
from k8s_agent.kube import Kube, labels
from k8s_agent.schemas import (
    AffectedResource,
    CriticVerdict,
    Diagnosis,
    DiagnosisStatus,
    Evidence,
    FaultCategory,
    FindingVerdict,
    PostFixCheck,
    ProposedFix,
    RemediatorAnswer,
    SelectorLabel,
)

RERUN = "Re-run the investigation; the steps it took are in the run record."
UNREADABLE = "The critic's verdict could not be read."
# Our types in the checkpointed state: LangGraph's serializer wants them listed.
CHECKPOINT_TYPES = [
    Diagnosis,
    AffectedResource,
    Evidence,
    ProposedFix,
    SelectorLabel,
    DiagnosisStatus,
    FaultCategory,
    FindingVerdict,
    PostFixCheck,
    RemediatorAnswer,
    VerifyRound,
    Remediation,
    Change,
    FixStep,
    FixOutcome,
]
# How the remediator or the recheck can stop early. Neither undoes the verified diagnosis: the
# stop is recorded with the fix. (A pause for approval isn't one of these: it propagates.)
AGENT_STOPS = (
    ModelError,
    ModelCallLimitExceededError,
    StructuredOutputValidationError,
    GraphRecursionError,
)


class InvestigationState(TypedDict):
    question: str
    namespace: NotRequired[str | None]
    messages: Annotated[list[AnyMessage], add_messages]  # shared with the investigator
    structured_response: NotRequired[Diagnosis | None]  # the investigator's latest draft
    rounds: Annotated[list[VerifyRound], operator.add]
    forced_reason: NotRequired[str | None]
    final: NotRequired[Diagnosis]
    remediation: NotRequired[Remediation]  # the fix, once the remediator is done


type InvestigationGraph = CompiledStateGraph[
    InvestigationState, RunContext, InvestigationState, InvestigationState
]


def intake(state: InvestigationState) -> dict[str, Any]:
    namespace = state.get("namespace")
    question = f"{state['question']}\n\nNamespace: {namespace}" if namespace else state["question"]
    return {"messages": [HumanMessage(question)]}


def code_checks(d: Diagnosis, steps: list[Step], settings: Settings) -> list[str]:
    """What structured output can't enforce. Each problem becomes feedback."""
    problems = []
    known = {s.ref for s in steps}
    if missing := [r for r in d.refs if r not in known]:
        problems.append(f"Cited evidence that doesn't exist: {', '.join(missing)}.")
    if empty := [e.ref for e in d.evidence if not e.finding.strip()]:
        problems.append(f"Findings with no text: {', '.join(empty)}. Say what each result shows.")
    if not d.root_cause.strip() or not d.recommended_action.strip():
        problems.append("root_cause and recommended_action must not be empty.")
    if d.status is not DiagnosisStatus.inconclusive and not d.evidence:
        problems.append(f"{d.status} needs at least one finding that cites evidence.")
    if d.status is DiagnosisStatus.no_fault and (
        d.fault_category is not FaultCategory.none or d.affected_resource is not None
    ):
        problems.append("no_fault takes fault_category none and no affected_resource.")
    if d.status is DiagnosisStatus.fault_found and (
        d.fault_category is FaultCategory.none or d.affected_resource is None
    ):
        problems.append("fault_found needs a fault_category other than none, and the resource.")
    if fix := d.proposed_fix:
        res = d.affected_resource
        target = (res.kind, res.namespace, res.name) if res else None
        if d.status is not DiagnosisStatus.fault_found:
            problems.append("A fix may be proposed only with status fault_found.")
        if d.fault_category is not FaultCategory.selector_mismatch:
            problems.append("A fix may be proposed only for a selector mismatch.")
        elif target != ("Service", fix.namespace, fix.service):
            problems.append("The proposed fix must target the affected Service.")
        if fix.namespace not in settings.remediation_namespaces:
            allowed = ", ".join(settings.remediation_namespaces)
            problems.append(f"Fixes are allowed only in {allowed}; set proposed_fix to null.")
        pairs = [(lb.key.strip(), lb.value.strip()) for lb in fix.selector]
        if not all(k and v for k, v in pairs) or len({k for k, _ in pairs}) < len(pairs):
            problems.append("The proposed selector needs unique, non-empty keys and values.")
    if d.post_fix_check is not None:
        problems.append("post_fix_check is only for a recheck after a fix; set it to null.")
    return problems


def feedback_message(feedback: str) -> HumanMessage:
    """The feedback, framed as data: the critic's words can quote tool output."""
    template = prompts.load("investigator_feedback")
    return HumanMessage(template.format(feedback=tool_output.quoted("feedback", feedback)))


def unread_message() -> HumanMessage:
    return HumanMessage(prompts.load("investigator_verdict_unread"))


async def read_verdict(
    ctx: RunContext, question: str, draft: Diagnosis, steps: list[Step]
) -> CriticVerdict | None:
    """The critic's verdict; None if it can't be read (a garbled answer: like an investigator's,
    it ends the round unfounded, not the run)."""
    try:
        return await review(ctx.critic, question, draft, steps)
    except OutputParserException as e:
        ctx.log.warning("critic_unreadable", extra={"error": f"{type(e).__name__}: {e}"})
        return None


def what_failed(verdict: CriticVerdict, draft: Diagnosis) -> str:
    """Why a round is unfounded when the critic gave no feedback: it said "founded" while its own
    findings, its fix verdict or its coverage of the draft's evidence say otherwise."""
    reasons = [f"{f.ref}: {f.reason}" for f in verdict.findings if not f.supported]
    judged = {f.ref for f in verdict.findings}
    if unjudged := [ref for ref in draft.refs if ref not in judged]:
        reasons.append(f"These findings couldn't be checked: {', '.join(unjudged)}.")
    if draft.proposed_fix is not None:
        if verdict.proposed_fix is None:
            reasons.append("The proposed fix couldn't be checked.")
        elif not verdict.proposed_fix.backed:
            reasons.append(f"The proposed fix isn't backed: {verdict.proposed_fix.reason}")
    return " ".join(reasons) or "The critic's verdict didn't cover the draft's evidence exactly."


async def verify_evidence(state: InvestigationState, runtime: Runtime[RunContext]) -> Command:
    ctx, draft = runtime.context, state.get("structured_response")  # unset: no answer yet
    if draft is None:
        reason = "The investigator stopped without a final answer."
        return Command(goto="result", update={"forced_reason": reason})
    steps = steps_from_messages(state["messages"])
    number = len(state["rounds"]) + 1
    problems = code_checks(draft, steps, ctx.settings)
    findings, fix_backed, unread = [], None, False
    if problems:
        founded, feedback = False, " ".join(problems)
    elif not draft.evidence:  # an inconclusive that cites nothing: nothing to check
        founded, feedback = True, None
    elif (verdict := await read_verdict(ctx, state["question"], draft, steps)) is None:
        founded, feedback, unread = False, UNREADABLE, True
    else:
        findings = verdict.findings
        if draft.proposed_fix is not None:  # a fix verdict on a draft without a fix is noise
            fix_backed = verdict.proposed_fix is not None and verdict.proposed_fix.backed
        covered = sorted(f.ref for f in findings) == sorted(draft.refs)  # one per finding
        supported = covered and all(f.supported for f in findings)
        founded = verdict.founded and supported and fix_backed is not False
        feedback = None if founded else (verdict.feedback.strip() or what_failed(verdict, draft))
    round_ = VerifyRound(
        round=number,
        founded=founded,
        problems=problems,
        findings=findings,
        proposed_fix_backed=fix_backed,
        feedback=feedback,
    )
    ctx.log.debug("verify_round", extra=round_.model_dump(exclude={"findings"}))
    if feedback is None:  # founded (an unfounded round always says why)
        return Command(goto="result", update={"rounds": [round_]})
    if number < ctx.settings.max_verify_rounds:
        ask = unread_message() if unread else feedback_message(feedback)
        update = {"messages": [ask], "structured_response": None}
        return Command(goto="investigate", update={"rounds": [round_], **update})
    if unread:
        reason = (
            f"The critic's verdict could not be read in the last verify round ({number}): the "
            "answer is unverified."
        )
    else:
        reason = (
            f"The evidence didn't support the conclusion after {number} verify rounds: {feedback}"
        )
    return Command(goto="result", update={"rounds": [round_], "forced_reason": reason})


def forced_inconclusive(reason: str) -> Diagnosis:
    return Diagnosis(
        status=DiagnosisStatus.inconclusive,
        fault_category=FaultCategory.none,
        affected_resource=None,
        root_cause=reason,
        evidence=[],
        recommended_action=RERUN,
        proposed_fix=None,
        post_fix_check=None,
    )


def final_diagnosis(state: InvestigationState) -> Diagnosis:
    if reason := state.get("forced_reason"):
        return forced_inconclusive(reason)
    draft = state.get("structured_response")
    if draft is None:  # verify_evidence gives a forced reason when there's no draft
        raise RuntimeError("the result has neither a draft nor a forced reason")
    steps = steps_from_messages(state["messages"])
    if draft.status is DiagnosisStatus.inconclusive and any(
        s.error and tool_output.REFUSED.search(s.output) for s in steps
    ):
        # Advice, so it goes with the next step: the root cause states only what the evidence
        # shows, and this is about the setup, which no tool output shows.
        note = (
            " The agent can read only namespaces that grant its reader a RoleBinding (see the "
            "README): to investigate a namespace it was refused, add one there."
        )
        action = draft.recommended_action.rstrip() + note
        draft = draft.model_copy(update={"recommended_action": action})
    return draft


def result(state: InvestigationState, runtime: Runtime[RunContext]) -> Command:
    """The final diagnosis, and the routing: on to the remediator only for a fix it may take.
    (Routed here rather than by a conditional edge: MLflow's autolog records an edge's lambda
    as a span with no name, and that loses the spans exported with it.)"""
    final = final_diagnosis(state)
    goto = "remediate" if fixable(final, runtime.context.settings) else END
    return Command(goto=goto, update={"final": final})


def fixable(d: Diagnosis, settings: Settings) -> bool:
    """Routing (code): on to the remediator only for a fault_found selector_mismatch whose
    proposed fix is in an allowed namespace; anything else stays advice. code_checks (in
    verify_evidence) already sent back any other fix, one that doesn't target the affected
    Service included, so only a checked draft gets here."""
    fix = d.proposed_fix
    return (
        fix is not None
        and d.status is DiagnosisStatus.fault_found
        and d.fault_category is FaultCategory.selector_mismatch
        and fix.namespace in settings.remediation_namespaces
    )


async def run_to_end(agent: Remediator, inputs: InputAgentState) -> tuple[dict, str | None]:
    """Run an agent to its end: its last state, and why it stopped early if it did (kept: what
    it did before the stop still counts). A pause for approval propagates: LangGraph's rules
    forbid catching it."""
    values: dict[str, Any] = {}
    try:
        async for chunk in agent.astream(inputs, stream_mode="values"):
            values = chunk
    except AGENT_STOPS as e:
        return values, f"{type(e).__name__}: {e}"
    return values, None


def remediate_node(remediator: Remediator) -> Callable[..., Awaitable[Command]]:
    async def remediate(state: InvestigationState) -> Command:
        """The remediator on the proposed fix. On resume after the approval pause this node
        runs again from its start and the remediator resumes from its checkpoint, so nothing
        here may have a side effect before that call (LangGraph's interrupt rules). An applied
        fix goes back to the investigator for one more round, the recheck: it continues its
        own conversation, so its evidence IDs and its run-wide budget (`thread_limit`) carry
        on."""
        final = state.get("final")
        fix = final.proposed_fix if final else None
        if fix is None:  # `result` routes here only with a fix (fixable)
            raise RuntimeError("remediate needs the final diagnosis's proposed fix")
        values, stopped = await run_to_end(remediator, {"messages": [fix_request(fix)]})
        remediation = read_remediation(
            fix, values.get("messages", []), values.get("structured_response"), stopped
        )
        if remediation.outcome is not FixOutcome.applied:
            return Command(goto=END, update={"remediation": remediation})
        ask = {"messages": [recheck_request(remediation)], "structured_response": None}
        return Command(goto="investigate", update={"remediation": remediation, **ask})

    return remediate


def recheck_request(r: Remediation) -> HumanMessage:
    c = r.change
    return HumanMessage(
        prompts.load("investigator_recheck").format(
            namespace=c.namespace,
            service=c.service,
            after=labels(c.after),
            before=labels(c.before),
            endpoints_ready=r.endpoints_ready,
        )
    )


def after_investigate(state: InvestigationState) -> str:
    """A round after a fix is the recheck, checked by code; any other goes to the critic."""
    return "recheck" if state.get("remediation") else "verify_evidence"


def why_unverified(r: Remediation, check: PostFixCheck | None, fresh: list[Step]) -> str | None:
    """Why the fix isn't verified; None when it is: the fix tool counted ready endpoints and
    the recheck, citing only its own fresh calls, says resolved."""
    if check is None or not check.evidence:
        return "the recheck gave no post_fix_check with evidence"
    fresh_refs = {s.ref for s in fresh}
    if old := [e.ref for e in check.evidence if e.ref not in fresh_refs]:
        return f"post_fix_check cites evidence not from the recheck: {', '.join(old)}"
    if not check.resolved:
        return "the recheck found the problem still there"
    if r.endpoints_ready == 0:
        return "the fix tool counted no ready endpoints"
    return None


def recheck(state: InvestigationState) -> dict[str, Any]:
    """The recheck round's answer: its post_fix_check, citing only the tool calls made since the
    recheck was asked (the last message the investigator was sent). No critic round."""
    remediation, final, messages = state.get("remediation"), state.get("final"), state["messages"]
    if remediation is None or final is None:  # after_investigate routes here only after a fix
        raise RuntimeError("recheck needs the final diagnosis and the fix")
    recheck_start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    answer = state.get("structured_response")
    check = answer.post_fix_check if answer else None
    note = why_unverified(remediation, check, steps_from_messages(messages[recheck_start:]))
    return {
        "final": final.model_copy(update={"post_fix_check": check}),
        "remediation": remediation.model_copy(
            update={"verified": note is None, "recheck_note": note}
        ),
    }


def build_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    settings: Settings,
    remediator_model: BaseChatModel,
    load_kube: Callable[[], Kube],
) -> InvestigationGraph:
    builder = StateGraph(InvestigationState, context_schema=RunContext)
    builder.add_node("intake", intake)
    builder.add_node("investigate", build_investigator(model, tools, settings))
    builder.add_node("verify_evidence", verify_evidence, destinations=("investigate", "result"))
    builder.add_node("result", result, destinations=("remediate", END))
    remediator = build_remediator(remediator_model, load_kube, settings)
    builder.add_node("remediate", remediate_node(remediator), destinations=("investigate", END))
    builder.add_node("recheck", recheck)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "investigate")
    builder.add_conditional_edges("investigate", after_investigate, ["verify_evidence", "recheck"])
    builder.add_edge("recheck", END)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return builder.compile(checkpointer=InMemorySaver(serde=serde))
