"""The investigation workflow, an explicit LangGraph graph:

    intake -> investigate -> verify_evidence -> result
                   ^               |
                   +-- feedback ---+   (not founded, verify rounds left)

- investigate: the investigator agent (`create_agent`), added directly as a node. It shares
  `messages` and `structured_response` with this graph and is compiled with per-thread
  persistence, so a feedback round continues its own conversation and its call-limit counters
  carry over (their `thread_limit` is the run-wide budget).
- verify_evidence: code checks, then the critic (no tools) on the cited outputs only.
- result: the final diagnosis, or a forced inconclusive with the reason.
"""

import operator
import textwrap
from importlib import resources
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRefusalMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from k8s_agent.agent import tool_output
from k8s_agent.agent.critic import judge
from k8s_agent.agent.middleware import (
    EvidenceMiddleware,
    RetryToolErrors,
    RunContext,
    retries_exhausted,
)
from k8s_agent.agent.records import Step, VerifyRound, steps_from_messages
from k8s_agent.config import Settings
from k8s_agent.schemas import CATEGORY_HELP, Diagnosis, DiagnosisStatus, FaultCategory

RERUN = "Re-run the investigation; the steps it took are in the run record."
# Our types in the checkpointed state: LangGraph's serializer wants them listed.
CHECKPOINT_TYPES = [
    ("k8s_agent.schemas", name)
    for name in ("Diagnosis", "AffectedResource", "Evidence", "ProposedFix", "SelectorLabel",
                 "DiagnosisStatus", "FaultCategory", "FindingVerdict")
] + [("k8s_agent.agent.records", "VerifyRound")]  # fmt: skip


class InvestigationState(TypedDict):
    question: str
    namespace: NotRequired[str | None]
    messages: Annotated[list[AnyMessage], add_messages]  # shared with the investigator
    structured_response: NotRequired[Diagnosis | None]  # the investigator's latest draft
    rounds: Annotated[list[VerifyRound], operator.add]
    forced_reason: NotRequired[str | None]
    final: NotRequired[Diagnosis]


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
            # A failed call is retried with backoff (RetryToolErrors decides which), then
            # comes back to the model as a tool error.
            ToolRetryMiddleware(
                max_retries=settings.tool_max_retries,
                initial_delay=settings.tool_retry_delay_seconds,
                on_failure=retries_exhausted(settings.tool_max_retries + 1),
            ),
            RetryToolErrors(),
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
        labels = [(lb.key.strip(), lb.value.strip()) for lb in fix.selector]
        if not all(k and v for k, v in labels) or len({k for k, _ in labels}) < len(labels):
            problems.append("The proposed selector needs unique, non-empty keys and values.")
    return problems


def feedback_message(feedback: str) -> HumanMessage:
    return HumanMessage(
        "A reviewer checked your answer against the tool outputs you cited and found it not "
        f"founded:\n{feedback}\n\nInvestigate further if needed, then answer again. Your "
        "earlier evidence IDs stay valid."
    )


async def verify_evidence(state: InvestigationState, runtime: Runtime[RunContext]) -> Command:
    ctx, draft = runtime.context, state["structured_response"]
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
    ctx.log.info("verify_round", extra=round_.model_dump(exclude={"findings"}))
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
    )


def result(state: InvestigationState) -> dict[str, Any]:
    if reason := state.get("forced_reason"):
        return {"final": forced(reason)}
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
    return {"final": draft}


def build_graph(model: BaseChatModel, tools: list[BaseTool], settings: Settings):
    builder = StateGraph(InvestigationState, context_schema=RunContext)
    builder.add_node("intake", intake)
    builder.add_node("investigate", build_investigator(model, tools, settings))
    builder.add_node("verify_evidence", verify_evidence, destinations=("investigate", "result"))
    builder.add_node("result", result)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "investigate")
    builder.add_edge("investigate", "verify_evidence")
    builder.add_edge("result", END)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return builder.compile(checkpointer=InMemorySaver(serde=serde))
