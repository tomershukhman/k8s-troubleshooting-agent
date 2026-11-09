"""M6, the fix (PLAN.md M6, SPEC 3.4): the remediator, the approval pause, the fix tool and the
recheck, with fake models, the in-process fake cluster and a fake Kubernetes API for the
remediator (tests/fakes.py). The real cluster and model: tests/acceptance/test_m6_fix.py."""

import asyncio
import logging
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
import urllib3
import yaml
from kubernetes import client as k
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde import jsonplus
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError
from rich.console import Console

from k8s_agent import tracing, ui
from k8s_agent.agent import Answer, investigate
from k8s_agent.agent.graph import InvestigationState, fixable, remediate_node
from k8s_agent.agent.records import Change, Facts, FixArgs, FixOutcome
from k8s_agent.agent.remediator import Kube, apply_fix, build_remediator
from k8s_agent.agent.remediator import tools as remediator_tools
from k8s_agent.config import get_settings
from k8s_agent.kube import KubeError
from k8s_agent.policy import FIX_TOOL, READ_ONLY_TOOLS, REMEDIATOR_READ_TOOLS
from k8s_agent.schemas import Diagnosis
from tests.fakes import (
    SERVICE_ARGS,
    FakeCluster,
    FakeServices,
    ScriptedModel,
    answer,
    call,
    critic,
    diagnosis,
    fix_call,
    remediator,
    selector_fix,
)

NS = {"namespace": "shop-checkout"}
SLICES = {"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice", **NS}
PROPOSED = diagnosis("E1", "E2", proposed_fix=selector_fix())
RECHECKED = PROPOSED | {
    "post_fix_check": {"resolved": True, "evidence": [{"ref": "E3", "finding": "2 endpoints"}]}
}
READS = call(
    ("get_service", {**NS, "name": "checkout"}),
    ("list_pods", {**NS, "selector": {"app": "checkout"}}),
)
PATCH = [
    {"op": "test", "path": "/metadata/resourceVersion", "value": "100"},
    {"op": "replace", "path": "/spec/selector", "value": {"app": "checkout"}},
]


def investigator(recheck: dict = RECHECKED) -> ScriptedModel:
    """Reads the Service and the pods, proposes the fix; asked to recheck, reads the
    EndpointSlices (E3) and answers `recheck`."""
    return ScriptedModel(
        script=[
            call(("resources_get", SERVICE_ARGS), ("pods_list_in_namespace", NS)),
            answer(PROPOSED),
            call(("resources_list", SLICES)),
            answer(recheck),
        ]
    )


def approver(yes: bool = True, *, seen: list | None = None, then=None, delay: float = 0):
    """Answers after `delay`; records each request in `seen`, and calls `then()` first."""

    async def ask(request, _diagnosis, _evidence_steps):
        if seen is not None:
            seen.append(request)
        if then:
            then()
        await asyncio.sleep(delay)
        return Answer(yes, f"{'approved' if yes else 'rejected'} in a test")

    return ask


def fix(approve=None, *, services=None, model=None, remediator_model=None):
    return asyncio.run(
        investigate(
            "Why is checkout unreachable?",
            settings=get_settings(),
            user_id="alice",
            approve=approve,
            model=model or investigator(),
            critic_model=critic(),
            remediator_model=remediator_model or remediator(READS, fix_call()),
            mcp_target=FakeCluster().server,
            remediator_kube=(services or FakeServices()).kube,
        )
    )


def args(**fields) -> FixArgs:
    base = {"namespace": "shop-checkout", "service": "checkout", "selector": {"app": "checkout"}}
    return FixArgs(**(base | {"resource_version": "100", "explanation": "x"} | fields))


# --- the pause, approve, reject ----------------------------------------------------------


def test_the_run_pauses_before_the_fix_runs_and_approving_applies_exactly_that_call():
    services, seen, at_pause = FakeServices(), [], []
    record = fix(approver(seen=seen, then=lambda: at_pause.append(list(services.patches))),
                 services=services)  # fmt: skip
    assert at_pause == [[]]  # paused: the tool hadn't run
    [request] = seen
    assert (request.tool, request.args) == (FIX_TOOL, fix_call().tool_calls[0]["args"])
    assert request.facts == Facts(  # read as the remediator, while the run waited
        current_selector={"app": "check-out"}, resource_version="100",
        ready_pods=["checkout-a", "checkout-b"],
    )  # fmt: skip
    assert services.patches == [("shop-checkout", "checkout", PATCH, "application/json-patch+json")]
    r = record.result.remediation
    assert r.change == Change(namespace="shop-checkout", service="checkout",
                              before={"app": "check-out"}, after={"app": "checkout"})  # fmt: skip
    assert (r.outcome, r.endpoints_ready, r.verified, r.recheck_note) == ("applied", 2, True, None)
    assert (r.decision.approved, r.decision.by, r.decision.reason) == (
        True,
        "alice",
        "approved in a test",
    )
    assert r.request == request
    assert [s.tool for s in r.steps] == ["get_service", "list_pods", FIX_TOOL]
    d = record.result.diagnosis
    assert d.post_fix_check.resolved and d.proposed_fix == Diagnosis(**PROPOSED).proposed_fix
    assert [s.ref for s in record.result.evidence_steps] == ["E1", "E2", "E3"]  # E3: the recheck


def test_the_approver_gets_the_verified_diagnosis_and_the_tool_calls_behind_it():
    shown = []

    async def ask(_request, diagnosis, evidence_steps):
        shown.append((diagnosis, evidence_steps))
        return Answer(False, "rejected in a test")

    record = fix(ask)
    [(d, steps)] = shown
    assert d == Diagnosis(**PROPOSED) == record.result.diagnosis  # the one the critic verified
    assert steps == record.result.evidence_steps  # the summary's own, cited ones only
    assert [(s.ref, s.tool) for s in steps] == [("E1", "resources_get"),
                                                ("E2", "pods_list_in_namespace")]  # fmt: skip


def test_a_reject_applies_nothing_and_there_is_no_recheck():
    services, model = FakeServices(), investigator()
    record = fix(approver(yes=False), services=services, model=model)
    assert services.patches == [] and services.selector == {"app": "check-out"}
    r = record.result.remediation
    assert (r.outcome, r.decision.approved, r.verified) == ("not_applied", False, False)
    assert "with reason: rejected in a test" in r.steps[-1].output  # the remediator was told why
    assert len(model.received) == 2 and record.result.diagnosis.post_fix_check is None


def test_without_an_approver_the_fix_is_rejected_by_the_agent():
    services = FakeServices()
    r = fix(services=services).result.remediation
    assert (r.decision.approved, r.decision.reason, r.decision.by) == (
        False,
        "no interactive approval available",
        "k8s-agent",
    )
    assert services.patches == [] and r.outcome == "not_applied"


def test_only_approve_or_reject_are_accepted():
    services = FakeServices()
    agent = build_remediator(remediator(fix_call()), services.kube, get_settings())
    builder = StateGraph(InvestigationState)
    builder.add_node("remediate", remediate_node(agent), destinations=("recheck", END))
    builder.add_node("recheck", lambda _: {})
    builder.add_edge(START, "remediate")
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}

    async def go():
        await graph.ainvoke({"final": Diagnosis(**PROPOSED)}, config)
        [pause] = (await graph.aget_state(config)).interrupts
        edit = {"name": FIX_TOOL, "args": {**pause.value["args"], "selector": {"app": "x"}}}
        for answer_ in (
            {"type": "edit", "edited_action": edit},
            {"type": "respond", "message": "ok"},
        ):
            with pytest.raises(ValidationError):  # LangGraph refuses it before saving it
                await graph.ainvoke(Command(resume={pause.id: answer_}), config)
        return (await graph.aget_state(config)).interrupts

    assert len(asyncio.run(go())) == 1  # still waiting for approve or reject
    assert services.patches == []


# --- the two clocks ---------------------------------------------------------------------


def test_the_wait_for_approval_does_not_count_toward_the_run_time_budget(env):
    env(run_max_seconds="1")
    record = fix(approver(delay=1.5))
    assert record.result.duration_ms > 1500 and record.forced_reason is None
    assert record.result.remediation.verified


def test_an_approval_that_does_not_come_in_time_is_a_reject(env):
    env(approval_timeout_seconds="0.2")
    services, t0 = FakeServices(), time.monotonic()
    r = fix(approver(delay=30), services=services).result.remediation
    assert time.monotonic() - t0 < 10
    assert (r.decision.approved, r.decision.reason) == (False, "the approval timed out after 0.2 s")
    assert services.patches == [] and r.outcome == "not_applied"


def test_a_time_budget_that_runs_out_in_the_fix_keeps_the_diagnosis(env):
    env(run_max_seconds="1")

    async def slow(_messages):
        await asyncio.sleep(10)

    rem = ScriptedModel(script=[READS, slow])  # it never gets to request the fix
    record = fix(approver(), remediator_model=rem)
    assert record.forced_reason is None and record.result.diagnosis.status == "fault_found"
    r = record.result.remediation
    assert (r.outcome, r.decision) == ("failed", None)
    assert "the run stopped before the fix finished: The run hit its 1 s time budget" in r.detail


@pytest.mark.parametrize(
    ("limit", "turns", "why"),
    [
        (None, [READS, ModelRateLimitError("rate limited")],
         "ModelRateLimitError: rate limited"),
        ("2", [READS, READS, fix_call()], "ModelCallLimitExceededError: "),  # its 3rd call
    ],
)  # fmt: skip
def test_a_remediator_that_stops_before_requesting_the_fix_keeps_the_diagnosis(
    env, limit, turns, why
):
    if limit:  # per round and per remediation; the investigation needs 2
        env(max_model_calls=limit)
    services, seen = FakeServices(), []
    rem = ScriptedModel(script=turns)
    record = fix(approver(seen=seen), services=services, remediator_model=rem)
    assert (record.result.status, record.result.diagnosis.status) == ("completed", "fault_found")
    r = record.result.remediation
    assert seen == [] and services.patches == [] and (r.outcome, r.decision) == ("failed", None)
    assert r.detail.startswith(f"the remediator stopped before the fix tool ran: {why}")


def test_the_agents_time_after_the_resume_counts_against_what_was_left(env):
    # 2 s of agent time: the investigation takes 1.5 s, the recheck after the approval 1 s more
    env(run_max_seconds="2")

    def slow(seconds: float, reply):
        async def turn(_messages):
            await asyncio.sleep(seconds)
            return reply

        return turn

    model = investigator()
    model.script[0] = slow(1.5, model.script[0])
    model.script[2] = slow(1.0, model.script[2])
    record = fix(approver(delay=1), model=model)  # the wait itself counts on its own clock
    r = record.result.remediation
    assert (record.result.diagnosis.status, record.forced_reason) == ("fault_found", None)
    assert (r.outcome, r.verified) == ("applied", False)
    assert "the run stopped in the recheck: The run hit its 2 s time budget" in r.recheck_note


def test_the_recheck_counts_toward_the_run_wide_budget(env):
    # 3 model calls a round and a run: the investigation takes 2, the recheck gets 1 more
    env(max_model_calls="3", run_max_model_calls="3")
    r = fix(approver()).result.remediation
    assert (r.outcome, r.verified) == ("applied", False)
    assert "run-wide model-call budget (3, AGENT_RUN_MAX_MODEL_CALLS)" in r.recheck_note


# --- what reaches the remediator, and what it may request ---------------------------------


@pytest.mark.parametrize(
    ("d", "routed"),
    [
        (PROPOSED, True),
        (diagnosis("E1"), False),  # no fix proposed
        (diagnosis("E1", proposed_fix=selector_fix(namespace="shop-orders")), False),
        (diagnosis("E1", fault_category="bad_config", proposed_fix=selector_fix()), False),
    ],
)
def test_only_a_selector_fix_in_an_allowed_namespace_is_routed(d, routed):
    assert fixable(Diagnosis(**d), get_settings()) is routed


def test_a_remediator_that_finds_no_ready_pod_to_select_declines_and_nobody_is_asked():
    services, seen, why = FakeServices(), [], "No ready pod matches app=checkout."
    services.pods = {name: (labels, False) for name, (labels, _) in services.pods.items()}
    rem = remediator(READS, reason=why)
    r = fix(approver(seen=seen), services=services, remediator_model=rem).result.remediation
    assert r.steps[1].output.startswith("0 of 2 pods ready, matching app=checkout")  # it saw
    assert seen == [] and services.patches == []
    assert (r.outcome, r.detail, r.decision) == ("not_applied", why, None)


def test_a_request_other_than_the_proposed_fix_is_rejected_without_asking():
    seen, services = [], FakeServices()
    rem = remediator(fix_call(selector={"app": "checkout", "tier": "web"}))
    r = fix(approver(seen=seen), services=services, remediator_model=rem).result.remediation
    assert seen == [] and services.patches == []
    assert not r.decision.approved and "differs from the proposed fix" in r.decision.reason


@pytest.mark.parametrize(
    "bad", [{"resource_version": None}, {"selector": [{"key": "app", "value": "checkout"}]}]
)
def test_a_malformed_request_is_rejected_without_asking(bad):
    seen, services = [], FakeServices()
    call_ = fix_call()
    call_.tool_calls[0]["args"] = {k: v for k, v in (call_.tool_calls[0]["args"] | bad).items()
                                   if v is not None}  # fmt: skip
    record = fix(approver(seen=seen), services=services, remediator_model=remediator(call_))
    r = record.result.remediation
    assert seen == [] and services.patches == []
    assert record.result.diagnosis.status == "fault_found"  # the diagnosis stands
    assert not r.decision.approved and r.decision.reason.startswith("the request is malformed:")
    assert r.outcome == "not_applied"


def test_only_the_remediator_holds_the_write_tool():
    model, judge, rem = investigator(), critic(), remediator(READS, fix_call())
    asyncio.run(
        investigate("q", settings=get_settings(), approve=approver(), model=model,
                    critic_model=judge, remediator_model=rem, mcp_target=FakeCluster().server,
                    remediator_kube=FakeServices().kube)
    )  # fmt: skip
    # every binding, the recheck's included: exactly the read tools (an empty list fails too)
    assert {frozenset(tools) for tools in model.bound_tools} == {READ_ONLY_TOOLS}
    assert judge.bound_tools == []
    assert {frozenset(t) for t in rem.bound_tools} == {REMEDIATOR_READ_TOOLS | {FIX_TOOL}}


def test_the_remediators_reads_have_the_investigators_safety_cap(env):
    env(tool_output_max_chars="60")
    services = FakeServices()
    services.pods = {f"checkout-{i}": ({"app": "checkout"}, True) for i in range(20)}
    reads = {t.name: t for t in remediator_tools(services.kube, get_settings())}
    kept, cut = reads["list_pods"].invoke(NS).split("\n[… ")
    assert kept.startswith("20 of 20 pods ready, in total\ncheckout-0") and len(kept) == 60
    assert cut.endswith(" characters cut …]")


def test_the_fix_is_requested_at_most_once():
    services, seen = FakeServices(), []
    rem = remediator(fix_call(), fix_call())  # asks again after the reject
    r = fix(approver(yes=False, seen=seen), services=services, remediator_model=rem)
    assert len(seen) == 1 and services.patches == []
    assert "Do not call 'patch_service_selector' again" in r.result.remediation.steps[-1].output


# --- the fix tool ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"namespace": "shop-orders"}, "fixes are allowed only in shop-checkout"),
        ({"selector": {"app": "nothing-has-this"}}, "no ready pod matches app=nothing-has-this"),
        ({"selector": {"app": " "}}, "non-empty keys and values"),
        ({"service": "other"}, "Service shop-checkout/other not found"),
    ],
)
def test_the_tool_refuses_what_it_must_not_apply(fields, why):
    services = FakeServices()
    result = apply_fix(services.kube(), args(**fields), get_settings())
    assert (result.outcome, services.patches) == (FixOutcome.invalid, [])
    assert why in result.detail


def test_the_exact_selector_is_applied_and_old_keys_do_not_survive():
    services = FakeServices(selector={"app": "check-out", "tier": "web"})
    result = apply_fix(services.kube(), args(), get_settings())
    assert (result.outcome, result.before) == (
        FixOutcome.applied,
        {"app": "check-out", "tier": "web"},
    )
    assert services.selector == {"app": "checkout"}


def test_a_service_changed_during_the_pause_is_stale_and_nothing_is_applied():
    services = FakeServices()
    r = fix(approver(then=services.edit), services=services).result.remediation
    assert (r.outcome, services.patches, services.selector) == ("stale", [], {"app": "check-out"})
    assert "version 100 → 101" in r.detail


def test_a_service_changed_just_before_the_patch_is_stale_too():
    # The tool's own version check passes, then the Service changes: the JSON Patch's test fails.
    services = FakeServices()
    patch = services.patch_namespaced_service

    def changed_first(*a, **kw):
        services.edit()
        return patch(*a, **kw)

    services.patch_namespaced_service = changed_first
    result = apply_fix(services.kube(), args(), get_settings())
    assert (result.outcome, services.selector) == (FixOutcome.stale, {"app": "check-out"})


@pytest.mark.parametrize("read", ["read_namespaced_service", "list_namespaced_pod"])
def test_no_answer_from_the_api_server_before_the_patch_is_a_failure_and_nothing_is_applied(
    monkeypatch, read
):
    services = FakeServices()
    monkeypatch.setattr(services, read, broken_connection)
    result = apply_fix(services.kube(), args(), get_settings())
    assert (result.outcome, services.patches) == (FixOutcome.failed, [])
    assert result.detail.startswith("no answer from the API server: ")


def test_no_answer_from_the_api_server_to_the_patch_is_a_failure_that_says_check(monkeypatch):
    services = FakeServices()
    monkeypatch.setattr(services, "patch_namespaced_service", broken_connection)
    result = apply_fix(services.kube(), args(), get_settings())
    assert result.outcome is FixOutcome.failed and "check whether" in result.detail


def test_once_applied_a_fix_stays_applied_whatever_the_endpoint_count_meets(monkeypatch):
    services = FakeServices()
    monkeypatch.setattr(services, "list_namespaced_endpoint_slice", broken_connection)
    result = apply_fix(services.kube(), args(), get_settings())
    assert (result.outcome, services.selector) == (FixOutcome.applied, {"app": "checkout"})
    assert "couldn't count the endpoints" in result.detail


def test_ready_endpoints_are_counted_once_each_and_an_unset_ready_is_ready():
    def endpoint(uid: str | None, ip: str, conditions):  # noqa: ANN001
        ref = k.V1ObjectReference(kind="Pod", uid=uid) if uid else None
        return k.V1Endpoint(addresses=[ip], conditions=conditions, target_ref=ref)

    ready, unset, no = (k.V1EndpointConditions(ready=r) for r in (True, None, False))
    slices = [
        [endpoint("a", "10.0.0.1", ready), endpoint("b", "10.0.0.2", None),
         endpoint("c", "10.0.0.3", unset), endpoint("d", "10.0.0.4", no)],
        [endpoint("a", "10.0.0.1", ready), endpoint(None, "10.0.0.5", ready)],  # a: in both
    ]  # fmt: skip
    listed = k.V1EndpointSliceList(
        items=[k.V1EndpointSlice(address_type="IPv4", endpoints=e) for e in slices]
    )
    discovery = SimpleNamespace(list_namespaced_endpoint_slice=lambda *_a, **_kw: listed)
    assert Kube(None, discovery, timeout=1).ready_endpoints("shop-checkout", "checkout") == 4


@pytest.mark.parametrize("proposes_fix", [True, False])
def test_the_remediators_own_client_is_closed_when_a_run_that_loaded_it_ends(
    monkeypatch, proposes_fix
):
    services, closed = FakeServices(), []
    services.api_client = SimpleNamespace(close=lambda: closed.append(True))  # its Kube's client
    monkeypatch.setattr(Kube, "load", classmethod(lambda _cls, _path, _timeout: services.kube()))
    no_fix = ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(diagnosis("E1"))])
    asyncio.run(
        investigate("q", settings=get_settings(), approve=approver(yes=False),
                    model=investigator() if proposes_fix else no_fix, critic_model=critic(),
                    remediator_model=remediator(READS, fix_call()),
                    mcp_target=FakeCluster().server)
    )  # fmt: skip
    assert closed == ([True] if proposes_fix else [])


def broken_connection(*_args, **_kwargs):
    raise urllib3.exceptions.ReadTimeoutError(None, "/", "read timed out")


def test_the_remediator_uses_its_own_kubeconfig_and_no_other(tmp_path, monkeypatch):
    def kubeconfig(server: str) -> str:
        path = tmp_path / server
        cluster = {"name": "c", "cluster": {"server": f"https://{server}:6443"}}
        user = {"name": "u", "user": {"token": "t"}}
        context = {"name": "x", "context": {"cluster": "c", "user": "u"}}
        path.write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "clusters": [cluster],
                                        "users": [user], "contexts": [context],
                                        "current-context": "x"}))  # fmt: skip
        return str(path)

    monkeypatch.setenv("KUBECONFIG", kubeconfig("admin"))
    kube = Kube.load(tmp_path / kubeconfig("remediator"), timeout=1)
    assert kube.core.api_client.configuration.host == "https://remediator:6443"
    with pytest.raises(KubeError, match="cannot load the remediator kubeconfig"):
        Kube.load(tmp_path / "missing", timeout=1)  # not KUBECONFIG instead


# --- the recheck ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "note"),
    [
        ({"resolved": True, "evidence": [{"ref": "E1", "finding": "old"}]}, "not from the recheck"),
        ({"resolved": False, "evidence": [{"ref": "E3", "finding": "f"}]}, "still there"),
        (None, "no post_fix_check"),
    ],
)
def test_verified_needs_a_resolved_recheck_on_fresh_evidence(check, note):
    record = fix(approver(), model=investigator(PROPOSED | {"post_fix_check": check}))
    r = record.result.remediation
    assert (r.outcome, r.verified) == ("applied", False) and note in r.recheck_note


def test_an_applied_fix_with_no_ready_endpoints_is_not_verified(env):
    env(verify_wait_seconds="0.05", verify_poll_seconds="0.01")
    services = FakeServices()
    services.list_namespaced_endpoint_slice = lambda *_a, **_kw: k.V1EndpointSliceList(items=[])
    r = fix(approver(), services=services).result.remediation  # the recheck says resolved
    assert (r.outcome, r.endpoints_ready, services.selector) == ("applied", 0, {"app": "checkout"})
    assert r.detail.endswith("; 0 ready endpoints")
    assert (r.verified, r.recheck_note) == (False, "the fix tool counted no ready endpoints")


def test_post_fix_check_in_the_investigation_is_sent_back():
    draft = diagnosis("E1", post_fix_check={"resolved": True, "evidence": []})
    model = ScriptedModel(script=[call(("namespaces_list", {})), answer(draft)],
                          then=answer(diagnosis("E1")))  # fmt: skip
    record = asyncio.run(
        investigate("q", settings=get_settings(), model=model, critic_model=critic(),
                    remediator_model=remediator(), mcp_target=FakeCluster().server)
    )  # fmt: skip
    [first, _] = record.result.verification
    assert "post_fix_check is only for a recheck" in first.problems[0]
    assert isinstance(model.received[-1][-1], HumanMessage)  # the feedback round


def test_the_checkpointer_knows_every_type_a_fix_stores(caplog):
    # LangGraph warns once per process per type, so forget what earlier tests triggered.
    jsonplus._warned_unregistered_types.clear()
    jsonplus._warned_blocked_types.clear()
    caplog.set_level(logging.WARNING)
    assert fix(approver()).result.remediation.verified
    assert [r.getMessage() for r in caplog.records if r.name.startswith("langgraph")] == []


@pytest.fixture
def traced(monkeypatch, tmp_path):
    """Tracing to a local MLflow store, no server. MLflow loads a trace only once every span
    it counted is stored, so a span lost on the way fails the read."""
    import mlflow

    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    get_settings.cache_clear()
    tracing.enable(get_settings())

    def read(result):
        tracing.flush()
        trace = mlflow.get_trace(result.trace_id)
        assert trace is not None and all(s.name for s in trace.data.spans)
        session = f"metadata.`mlflow.trace.session` = '{result.thread_id}'"
        assert len(mlflow.search_traces(filter_string=session, return_type="list")) == 1
        return trace

    return read


def test_a_run_with_a_fix_is_one_complete_trace(traced):
    result = fix(approver()).result
    trace = traced(result)
    [approval] = trace.search_spans(name="approval")  # what was shown, then the decision
    assert approval.inputs["args"]["selector"] == {"app": "checkout"}
    assert approval.inputs["facts"]["ready_pods"] == ["checkout-a", "checkout-b"]
    assert (approval.outputs["approved"], approval.outputs["by"]) == (True, "alice")
    [patch] = trace.search_spans(name=FIX_TOOL)
    [recheck] = trace.search_spans(name="recheck")
    assert approval.end_time_ns <= patch.start_time_ns < recheck.start_time_ns


def malformed() -> ScriptedModel:
    call_ = fix_call()
    call_.tool_calls[0]["args"].pop("resource_version")
    return remediator(call_)


@pytest.mark.parametrize(
    ("approve", "make_remediator", "reason", "by"),
    [  # a model is built per run (its script is used up), so the parameter is its factory
        (approver(yes=False), None, "rejected in a test", "alice"),
        (approver(delay=30), None, "the approval timed out after 0.2 s", "k8s-agent"),
        (None, None, "no interactive approval available", "k8s-agent"),
        (approver(), malformed, "the request is malformed: resource_version", "k8s-agent"),
    ],
)
def test_every_decision_is_on_the_trace_with_who_and_when(
    approve, make_remediator, reason, by, traced, env
):
    env(approval_timeout_seconds="0.2")
    result = fix(approve, remediator_model=make_remediator and make_remediator()).result
    [approval] = traced(result).search_spans(name="approval")
    decision = approval.outputs
    assert (decision["approved"], decision["by"]) == (False, by)
    assert decision["reason"].startswith(reason)
    assert result.started_at <= datetime.fromisoformat(decision["at"]) <= result.completed_at
    assert decision == result.remediation.decision.model_dump(mode="json")  # the same record


def test_the_summary_says_what_was_decided_done_and_verified():
    console = Console(width=200, record=True)
    console.print(ui.summary(fix(approver()).result))
    text = console.export_text()
    assert "Fix: approved: approved in a test (by alice, " in text
    assert (
        "Applied: selector app=check-out → app=checkout; 2 ready endpoints. "
        "Verified by a recheck with fresh evidence (E3)." in text
    )
