"""The investigation workflow, an explicit LangGraph graph:

    intake -> investigate -> verify_evidence -> result -> remediate -> investigate -> recheck
                   ^               |               |          |        (a round that
                   +-- feedback ---+               +-> end    +-> end   rechecks the fix)

- investigate: the investigator agent (`create_agent`), added directly as a node. It shares
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
- recheck: code, no critic: the recheck round's `post_fix_check` must cite only its own fresh
  tool calls, and decides with the fix tool's endpoint count whether the fix is verified.
"""

import operator
import textwrap
from collections.abc import Callable
from importlib import resources
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRefusalMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import ProviderStrategy, StructuredOutputValidationError
from langchain_core.exceptions import ModelError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from k8s_agent.agent import tool_output
from k8s_agent.agent.critic import judge
from k8s_agent.agent.middleware import (
    EvidenceMiddleware,
    RunContext,
    ToolErrorAsResult,
    retries_exhausted,
    worth_retrying,
)
from k8s_agent.agent.records import (
    FixOutcome,
    Remediation,
    Step,
    VerifyRound,
    steps_from_messages,
)
from k8s_agent.agent.remediator import Kube, build_remediator, fix_request, labels, remediation
from k8s_agent.config import Settings
from k8s_agent.schemas import (
    CATEGORY_HELP,
    Diagnosis,
    DiagnosisStatus,
    FaultCategory,
    PostFixCheck,
)

RERUN = "Re-run the investigation; the steps it took are in the run record."
# Our types in the checkpointed state: LangGraph's serializer wants them listed.
CHECKPOINT_TYPES = [
    ("k8s_agent.schemas", name)
    for name in ("Diagnosis", "AffectedResource", "Evidence", "ProposedFix", "SelectorLabel",
                 "DiagnosisStatus", "FaultCategory", "FindingVerdict", "PostFixCheck",
                 "RemediatorAnswer")
] + [
    ("k8s_agent.agent.records", name)
    for name in ("VerifyRound", "Remediation", "Change", "FixStep", "FixOutcome")
]  # fmt: skip
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


def system_prompt(settings: Settings) -> str:
    return (
        resources.files("k8s_agent.agent")
        .joinpath("prompt.md")
        .read_text()
        .format(
            categories=textwrap.indent(CATEGORY_HELP, "  "),
            max_tool_calls=settings.max_tool_calls,
            remediation_namespaces=", ".join(settings.remediation_namespaces),
        )
    )


def build_investigator(model: BaseChatModel, tools: list[BaseTool], settings: Settings):
    # run_limit: per investigate round; thread_limit: the whole run, one thread per run. M8
    # keeps a thread across questions, so it must reset this budget for each question.
    return create_agent(
        model,
        tools,
        system_prompt=system_prompt(settings),
        # Native structured output, set explicitly: a bare schema lets LangChain fall back to a
        # forced tool call, which Sonnet 5.5 rejects (SPEC §3.2).
        response_format=ProviderStrategy(Diagnosis),
        middleware=[
            EvidenceMiddleware(),
            # A failed call raises: it is retried with backoff unless a retry can't fix it,
            # then comes back to the model as a tool error with the server's text.
            ToolErrorAsResult(),
            ToolRetryMiddleware(
                max_retries=settings.tool_max_retries,
                initial_delay=settings.tool_retry_delay_seconds,
                retry_on=worth_retrying,
                on_failure=retries_exhausted(settings.tool_max_retries + 1),
            ),
            ModelRefusalMiddleware(),
            ModelCallLimitMiddleware(  # "error": the run stops, and the core says which limit
                run_limit=settings.max_model_calls,
                thread_limit=settings.run_max_model_calls,
                exit_behavior="error",
            ),
            ToolCallLimitMiddleware(  # "continue": further calls refused, the model concludes
                run_limit=settings.max_tool_calls,
                thread_limit=settings.run_max_tool_calls,
                exit_behavior="continue",
            ),
        ],
        context_schema=RunContext,
        checkpointer=True,
        name="investigator",
    )


def intake(state: InvestigationState) -> dict[str, Any]:
    namespace = state.get("namespace")
    question = f"{state['question']}\n\nNamespace: {namespace}" if namespace else state["question"]
    return {"messages": [HumanMessage(question)], "rounds": []}


def code_checks(d: Diagnosis, steps: list[Step], settings: Settings) -> list[str]:
    """What structured output can't enforce. Each problem becomes feedback."""
    problems = []
    if missing := [r for r in d.refs if r not in {s.ref for s in steps}]:
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
    return HumanMessage(
        "A reviewer checked your answer against the tool outputs you cited and found it not "
        f"founded:\n{feedback}\n\nInvestigate further if needed, then answer again. Your "
        "earlier evidence IDs stay valid."
    )


async def verify_evidence(state: InvestigationState, runtime: Runtime[RunContext]) -> Command:
    ctx, draft = runtime.context, state.get("structured_response")  # unset: no answer yet
    if draft is None:
        reason = "The investigator stopped without a final answer."
        return Command(goto="result", update={"forced_reason": reason})
    steps = steps_from_messages(state["messages"])
    number = len(state["rounds"]) + 1
    problems = code_checks(draft, steps, ctx.settings)
    findings, fix_backed = [], None
    if problems:
        founded, feedback = False, " ".join(problems)
    elif not draft.evidence:  # an inconclusive that cites nothing: nothing to check
        founded, feedback = True, None
    else:
        verdict = await judge(ctx.critic, state["question"], draft, steps)
        findings = verdict.findings
        if draft.proposed_fix is not None:  # a fix verdict on a draft without a fix is noise
            fix_backed = verdict.proposed_fix is not None and verdict.proposed_fix.backed
        covered = sorted(f.ref for f in findings) == sorted(draft.refs)  # one per finding
        supported = covered and all(f.supported for f in findings)
        founded = verdict.founded and supported and fix_backed is not False
        feedback = None if founded else (verdict.feedback.strip() or "Not every finding held up.")
    round_ = VerifyRound(
        round=number,
        founded=founded,
        problems=problems,
        findings=findings,
        proposed_fix_backed=fix_backed,
        feedback=feedback,
    )
    ctx.log.debug("verify_round", extra=round_.model_dump(exclude={"findings"}))
    if founded:
        return Command(goto="result", update={"rounds": [round_]})
    if number < ctx.settings.max_verify_rounds:
        update = {"messages": [feedback_message(feedback)], "structured_response": None}
        return Command(goto="investigate", update={"rounds": [round_], **update})
    reason = f"The evidence didn't support the conclusion after {number} verify rounds: {feedback}"
    return Command(goto="result", update={"rounds": [round_], "forced_reason": reason})


def forced(reason: str) -> Diagnosis:
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
        return forced(reason)
    draft = state["structured_response"]
    steps = steps_from_messages(state["messages"])
    if draft.status is DiagnosisStatus.inconclusive and any(
        s.error and tool_output.REFUSED.search(s.output) for s in steps
    ):
        note = (
            " The agent can read only namespaces that grant its reader a RoleBinding (see the "
            "README); add one to investigate this namespace."
        )
        draft = draft.model_copy(update={"root_cause": draft.root_cause.rstrip() + note})
    return draft


def result(state: InvestigationState, runtime: Runtime[RunContext]) -> Command:
    """The final diagnosis, and the routing: on to the remediator only for a fix it may take.
    (Routed here rather than by a conditional edge: MLflow's autolog records an edge's lambda
    as a span with no name, and that loses the spans exported with it.)"""
    final = final_diagnosis(state)
    goto = "remediate" if fixable(final, runtime.context.settings) else END
    return Command(goto=goto, update={"final": final})


def fixable(d: Diagnosis, settings: Settings) -> bool:
    """Routing (code): only a proposed fix of the supported type (a selector mismatch on the
    affected Service), in an allowed namespace, reaches the remediator. Anything else stays
    advice. verify_evidence already sent back a fix proposed elsewhere; this is the gate."""
    fix = d.proposed_fix
    return (
        fix is not None
        and d.status is DiagnosisStatus.fault_found
        and d.fault_category is FaultCategory.selector_mismatch
        and fix.namespace in settings.remediation_namespaces
    )


async def run_to_end(agent: Any, inputs: dict[str, Any], **kwargs: Any) -> tuple[dict, str | None]:
    """Run an agent to its end: its last state, and why it stopped early if it did (kept: what
    it did before the stop still counts). A pause for approval propagates: LangGraph's rules
    forbid catching it."""
    values: dict[str, Any] = {}
    try:
        async for chunk in agent.astream(inputs, stream_mode="values", **kwargs):
            values = chunk
    except AGENT_STOPS as e:
        return values, f"{type(e).__name__}: {e}"
    return values, None


def remediate_node(remediator: Any) -> Callable:
    async def remediate(state: InvestigationState) -> Command:
        """The remediator on the proposed fix. On resume after the approval pause this node
        runs again from its start and the remediator resumes from its checkpoint, so nothing
        here may have a side effect before that call (LangGraph's interrupt rules). An applied
        fix goes back to the investigator for one more round, the recheck: it continues its
        own conversation, so its evidence IDs and its run-wide budget (`thread_limit`) carry
        on."""
        fix = state["final"].proposed_fix
        values, stopped = await run_to_end(remediator, {"messages": [fix_request(fix)]})
        done = remediation(
            fix, values.get("messages", []), values.get("structured_response"), stopped
        )
        if done.outcome is not FixOutcome.applied:
            return Command(goto=END, update={"remediation": done})
        ask = {"messages": [recheck_request(done)], "structured_response": None}
        return Command(goto="investigate", update={"remediation": done, **ask})

    return remediate


def recheck_request(r: Remediation) -> HumanMessage:
    c = r.change
    return HumanMessage(
        f"A fix was just applied: Service {c.namespace}/{c.service} now selects "
        f"{labels(c.after)} (before: {labels(c.before)}), and the fix tool counted "
        f"{r.endpoints_ready} ready endpoints. Check again with fresh tool calls whether the "
        "problem is gone: your earlier results are from before the fix. Then answer as before, "
        "with post_fix_check filled in: resolved, and findings that cite only the tool calls "
        "you make now."
    )


def after_investigate(state: InvestigationState) -> str:
    """A round after a fix is the recheck, checked by code; any other goes to the critic."""
    return "recheck" if state.get("remediation") else "verify_evidence"


def unverified(r: Remediation, check: PostFixCheck | None, fresh: list[Step]) -> str | None:
    """Why the fix isn't verified; None when it is: the fix tool counted ready endpoints and
    the recheck, citing only its own fresh calls, says resolved."""
    if check is None or not check.evidence:
        return "the recheck gave no post_fix_check with evidence"
    if old := [e.ref for e in check.evidence if e.ref not in {s.ref for s in fresh}]:
        return f"post_fix_check cites evidence not from the recheck: {', '.join(old)}"
    if not check.resolved:
        return "the recheck found the problem still there"
    if r.endpoints_ready == 0:
        return "the fix tool counted no ready endpoints"
    return None


def recheck(state: InvestigationState) -> dict[str, Any]:
    """The recheck round's answer: its post_fix_check, citing only the tool calls made since the
    recheck was asked (the last message the investigator was sent). No critic round."""
    fix, messages = state["remediation"], state["messages"]
    asked = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    answer = state.get("structured_response")
    check = answer.post_fix_check if answer else None
    note = unverified(fix, check, steps_from_messages(messages[asked:]))
    return {
        "final": state["final"].model_copy(update={"post_fix_check": check}),
        "remediation": fix.model_copy(update={"verified": note is None, "recheck_note": note}),
    }


def build_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    settings: Settings,
    remediator_model: BaseChatModel,
    kube: Callable[[], Kube],
):
    builder = StateGraph(InvestigationState, context_schema=RunContext)
    builder.add_node("intake", intake)
    builder.add_node("investigate", build_investigator(model, tools, settings))
    builder.add_node("verify_evidence", verify_evidence, destinations=("investigate", "result"))
    builder.add_node("result", result, destinations=("remediate", END))
    remediator = build_remediator(remediator_model, kube, settings)
    builder.add_node("remediate", remediate_node(remediator), destinations=("investigate", END))
    builder.add_node("recheck", recheck)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "investigate")
    builder.add_conditional_edges("investigate", after_investigate, ["verify_evidence", "recheck"])
    builder.add_edge("recheck", END)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return builder.compile(checkpointer=InMemorySaver(serde=serde))
