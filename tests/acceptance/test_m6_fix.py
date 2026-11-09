"""M6: the fix on the real cluster, model and tracking server (PLAN.md M6). The selector
scenario's question is asked without a namespace. The first test runs the real command with no
terminal on stdin, so the fix request is rejected; the others decide the request with the test's
own approver, inside the same call, as the CLI does. A test that changes the Service rebuilds the
scenario afterwards. Model answers vary: a run that never reaches the fix request gets one
retry."""

import asyncio
import json
import os
import subprocess

import mlflow
import pytest
from kubernetes import client

from k8s_agent import tracing
from k8s_agent.agent import Answer, RunRecord, investigate
from k8s_agent.agent.records import FixArgs, FixOutcome
from k8s_agent.agent.remediator import Kube, apply_fix
from k8s_agent.config import get_settings
from k8s_agent.policy import FIX_TOOL
from k8s_agent.scenarios import get_scenario

from .conftest import ROOT, agent, stored

pytestmark = pytest.mark.usefixtures("cluster")
ATTEMPTS = 2
NS, NAME, BROKEN = "shop-checkout", "checkout", {"app": "check-out"}
# One event loop for every model call (CLAUDE.md: langchain-anthropic's pooled connections).
LOOP = asyncio.Runner()


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    LOOP.close()


@pytest.fixture(scope="module")
def want():
    return get_scenario(get_settings().scenarios_dir, "selector")


@pytest.fixture(scope="module")
def traced() -> tracing.Tracing:
    s = get_settings()
    if not s.mlflow_tracking_uri:
        pytest.fail("set MLFLOW_TRACKING_URI (see .env.example) to run the fix tests")
    return tracing.enable(s)


@pytest.fixture
def rebuilt():
    """The test changes the Service: the scenario is broken again afterwards."""
    yield
    agent("scenario", "up", "selector")


def service(kube) -> client.V1Service:
    return kube.core.read_namespaced_service(NAME, NS)


def ready_endpoints(kube) -> int:
    """Counted here with the admin account, not with the agent's code."""
    slices = client.DiscoveryV1Api(kube.api).list_namespaced_endpoint_slice(
        NS, label_selector=f"kubernetes.io/service-name={NAME}"
    )
    return sum(bool(e.conditions.ready) for s in slices.items for e in s.endpoints or [])


def until_requested(question: str, approve) -> RunRecord:
    """Ask until a run reaches the fix request, deciding it with `approve`."""
    for _ in range(ATTEMPTS):
        get_settings.cache_clear()
        record = LOOP.run(
            investigate(question, settings=get_settings(), user_id="bob", approve=approve)
        )
        r = record.result
        print(f"\n  {r.status} in {r.duration_ms} ms; trace {r.trace_id}")
        print(f"  {r.remediation.model_dump_json() if r.remediation else 'no fix requested'}")
        if r.remediation and r.remediation.request:
            return record
    pytest.fail(f"no run reached the fix request in {ATTEMPTS} attempts")


def test_the_right_fix_is_proposed_and_requested_and_without_a_terminal_rejected(kube, want):
    before = service(kube)
    for _ in range(ATTEMPTS):
        done = subprocess.run(
            ["uv", "run", "agent", "investigate", want.spec.question, "--json", "--user", "alice"],
            cwd=ROOT, env=dict(os.environ), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=600,
        )  # fmt: skip
        print(done.stderr)
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        if (r := result["remediation"]) and r["request"]:
            break
    else:
        pytest.fail(f"no run reached the fix request in {ATTEMPTS} attempts")
    fix, expected = result["diagnosis"]["proposed_fix"], want.spec.expected_fix
    selector = {label["key"]: label["value"] for label in fix["selector"]}
    assert (fix["namespace"], fix["service"], selector) == (NS, NAME, expected.selector)
    args = r["request"]["args"]  # exactly what the remediator asked to run
    assert (args["namespace"], args["service"], args["selector"]) == (NS, NAME, expected.selector)
    assert args["resource_version"] == before.metadata.resource_version
    assert len(r["request"]["facts"]["ready_pods"]) == 2  # read as the remediator at the pause
    assert (r["decision"]["approved"], r["decision"]["reason"]) == (
        False,
        "no interactive approval available",
    )
    assert r["outcome"] == "not_applied" and r["verified"] is False
    after = service(kube)
    assert (after.spec.selector, after.metadata.resource_version) == (
        BROKEN,
        before.metadata.resource_version,
    )


def test_editing_the_service_during_the_pause_makes_the_fix_refuse(kube, want, rebuilt):
    async def edit_then_approve(_request):
        edit = {"metadata": {"annotations": {"k8s-agent.test/edited": "during-the-pause"}}}
        kube.core.patch_namespaced_service(NAME, NS, edit)
        return Answer(True, "approved after an edit, by the M6 acceptance test")

    r = until_requested(want.spec.question, edit_then_approve).result.remediation
    assert r.decision.approved and r.outcome == "stale" and "nothing was applied" in r.detail
    assert service(kube).spec.selector == BROKEN


def test_the_patch_itself_refuses_a_service_changed_after_the_tools_check(
    kube, want, monkeypatch, rebuilt
):
    # SPEC 6 item 8 on the real API server: the JSON Patch's `test` of the resourceVersion, not
    # just the tool's own check before it. No model: the fix tool, as the remediator.
    s, before = get_settings(), service(kube)
    check = Kube.ready_pods

    def edited_meanwhile(self, namespace, selector):  # after the tool's version check passed
        edit = {"metadata": {"annotations": {"k8s-agent.test/edited": "before-the-patch"}}}
        kube.core.patch_namespaced_service(NAME, NS, edit)
        return check(self, namespace, selector)

    monkeypatch.setattr(Kube, "ready_pods", edited_meanwhile)
    version = before.metadata.resource_version
    args = FixArgs(namespace=NS, service=NAME, selector=want.spec.expected_fix.selector,
                   resource_version=version, explanation="test")  # fmt: skip
    result = apply_fix(Kube.load(s.remediator_kubeconfig, s.remediator_timeout_seconds), args, s)
    print(f"  {result}")
    assert result.outcome is FixOutcome.stale and "nothing was applied" in result.detail
    assert service(kube).spec.selector == BROKEN


def test_an_approved_fix_gets_endpoints_and_is_verified_all_in_one_trace(
    kube, want, traced, rebuilt
):
    at_pause = []

    async def approve(_request):
        at_pause.append((service(kube).spec.selector, ready_endpoints(kube)))
        return Answer(True, "approved by the M6 acceptance test")

    record = until_requested(want.spec.question, approve)
    result, r = record.result, record.result.remediation
    assert at_pause == [(BROKEN, 0)]  # paused before anything changed
    assert (r.outcome, r.verified, r.decision.approved, r.decision.by) == ("applied", True, True,
                                                                           "bob")  # fmt: skip
    assert r.change.before == BROKEN and r.change.after == want.spec.expected_fix.selector
    assert service(kube).spec.selector == want.spec.expected_fix.selector  # exactly, no old key
    assert r.endpoints_ready == ready_endpoints(kube) == 2
    check = result.diagnosis.post_fix_check
    assert check.resolved and {e.ref for e in check.evidence}.isdisjoint(
        e.ref for e in result.diagnosis.evidence
    )  # fresh evidence, from the recheck

    trace = stored(result.trace_id)
    session = f"metadata.`mlflow.trace.session` = '{result.thread_id}'"
    found = mlflow.search_traces(
        locations=[traced.experiment_id], filter_string=session, return_type="list"
    )
    assert [t.info.trace_id for t in found] == [result.trace_id]  # one trace for the whole run
    [approval] = trace.search_spans(name="approval")
    assert approval.inputs["args"]["selector"] == want.spec.expected_fix.selector  # what was shown
    assert (approval.outputs["approved"], approval.outputs["by"]) == (True, "bob")
    [patch] = trace.search_spans(name=FIX_TOOL)
    [recheck] = trace.search_spans(name="recheck")
    assert approval.end_time_ns <= patch.start_time_ns < recheck.start_time_ns
    assert trace.search_spans(name="investigator") and trace.search_spans(name="critic")
    print(f"  trace: {traced.url(result.trace_id)}")
