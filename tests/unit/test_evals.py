"""The evals' exact checks and pass marks (SPEC 2.3), with hand-made answers and traces. The
judges and `mlflow.genai.evaluate` itself run in tests/acceptance/test_m5_evals.py."""

import importlib.abc
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from mlflow.entities import Feedback, SpanType, TraceInfo, TraceLocation, TraceState
from mlflow.genai.evaluation.quality_criteria import parse_quality_criteria
from typer.testing import CliRunner

import k8s_agent
from k8s_agent import cli, evals, scenarios, tracing
from k8s_agent.agent import McpUnavailable, NotStarted
from k8s_agent.agent.records import RunRecord, RunResult, Step
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from k8s_agent.scenarios import ScenarioError, get_scenario
from tests.fakes import SERVICE_ARGS, diagnosis, healthy, selector_fix

CHECKOUT = {"namespace": "shop-checkout"}
PODS = ("pods_list_in_namespace", CHECKOUT)
SERVICE = ("resources_get", SERVICE_ARGS)


def expectations(folder: str = "selector") -> dict:
    scn = get_scenario(get_settings().scenarios_dir, folder)
    return evals.dataset(scn, 1)[0]["expectations"]


def span(ref: str, tool: str, args: dict, ok: bool = True):
    return SimpleNamespace(
        name=tool,
        inputs=args,
        attributes={"metadata": {"evidence_ref": ref}},
        status=SimpleNamespace(status_code="OK" if ok else "ERROR"),
    )


def trace(*calls: tuple, failed: tuple[int, ...] = ()):
    """A run's trace with these tool calls, E1, E2, ... in order."""
    spans = [span(f"E{i}", tool, args, i not in failed) for i, (tool, args) in enumerate(calls, 1)]
    other = SimpleNamespace(name="ChatAnthropic", inputs={}, attributes={}, status=None)
    by_type = {SpanType.TOOL: spans, SpanType.CHAT_MODEL: [other]}
    return SimpleNamespace(search_spans=lambda span_type: by_type.get(span_type, []))


def outputs(d: dict | None, *calls: tuple, status: str = "completed", error: str | None = None):
    """The result JSON as the scorers see it, citing the first calls."""
    steps = [
        {"ref": f"E{i}", "tool": t, "args": a, "output": "…"} for i, (t, a) in enumerate(calls, 1)
    ]
    return {"status": status, "error": error, "diagnosis": d, "evidence_steps": steps}


# --- diagnosis_correct ----------------------------------------------------------------------


def correct(out: dict, folder: str = "selector"):
    return evals.diagnosis_correct(outputs=out, expectations=expectations(folder))


def test_an_accepted_answer_is_correct():
    assert correct(outputs(diagnosis("E1"), SERVICE)).value is True


@pytest.mark.parametrize(
    ("d", "why"),
    [
        (diagnosis("E1", fault_category="network_policy"), "category network_policy"),
        (diagnosis("E1", status="inconclusive"), "status inconclusive"),
        (diagnosis("E1", affected_resource={**CHECKOUT, "kind": "Deployment", "name": "checkout"}),
         "affected resource"),
        (diagnosis("E1", affected_resource={"kind": "Service", "namespace": "default",
                                            "name": "checkout"}), "affected resource"),
    ],
)  # fmt: skip
def test_a_wrong_answer_is_not(d, why):
    verdict = correct(outputs(d, SERVICE))
    assert verdict.value is False and why in verdict.rationale


def test_healthy_needs_evidence_from_the_scenarios_namespace():
    inventory = ("pods_list_in_namespace", {"namespace": "shop-inventory"})
    assert correct(outputs(healthy("E1"), inventory), "healthy").value is True
    verdict = correct(outputs(healthy("E1"), PODS), "healthy")  # healthy, but somewhere else
    assert verdict.value is False and "no cited evidence from shop-inventory" in verdict.rationale


def test_a_failed_run_is_wrong_and_says_why():
    out = outputs(None, status="failed", error="model API error: rate limit")
    for check in (evals.diagnosis_correct, evals.evidence_exists):
        kwargs = {"expectations": expectations()} if check is evals.diagnosis_correct else {
            "trace": trace()}  # fmt: skip
        verdict = check(outputs=out, **kwargs)
        assert verdict.value is False and "rate limit" in verdict.rationale


# --- evidence_exists ------------------------------------------------------------------------


def test_every_cited_ref_must_be_a_tool_call_in_the_trace():
    out = outputs(diagnosis("E1", "E2"), SERVICE, PODS)
    assert evals.evidence_exists(outputs=out, trace=trace(SERVICE, PODS)).value is True
    verdict = evals.evidence_exists(outputs=out, trace=trace(SERVICE))
    assert verdict.value is False and "E2" in verdict.rationale
    check = {"resolved": True, "evidence": [{"ref": "E3", "finding": "f"}]}  # the recheck's
    out = outputs(diagnosis("E1", "E2", post_fix_check=check), SERVICE, PODS)
    verdict = evals.evidence_exists(outputs=out, trace=trace(SERVICE, PODS))
    assert verdict.value is False and "E3" in verdict.rationale
    assert evals.evidence_exists(outputs=out, trace=trace(SERVICE, PODS, PODS)).value is True


def test_an_answer_code_put_in_place_of_the_agents_is_not_valid_output():
    out = outputs(diagnosis("E1", status="inconclusive"), SERVICE) | {
        "forced_reason": "The answer was cut off at the output limit (AGENT_MAX_OUTPUT_TOKENS)."
    }
    verdict = evals.evidence_exists(outputs=out, trace=trace(SERVICE))
    assert verdict.value is False and "cut off at the output limit" in verdict.rationale


def test_a_conclusion_needs_evidence():
    verdict = evals.evidence_exists(outputs=outputs(diagnosis()), trace=trace())
    assert verdict.value is False and "cites no evidence" in verdict.rationale


# --- right_tools ----------------------------------------------------------------------------


def looked(*calls: tuple, failed: tuple[int, ...] = ()):
    return evals.right_tools(trace=trace(*calls, failed=failed), expectations=expectations())


def test_each_requirement_is_met_by_any_of_its_alternatives():
    assert looked(SERVICE, PODS).value is True
    listed = ("resources_list", {"apiVersion": "v1", "kind": "Service", **CHECKOUT})
    assert looked(listed, PODS).value is True  # the other way to see the Service


@pytest.mark.parametrize(
    ("calls", "failed"),
    [
        ((PODS,), ()),  # never looked at the Service
        ((SERVICE, PODS), (1,)),  # the Service call failed: nothing seen
        ((("resources_get", {**SERVICE_ARGS, "namespace": "shop-orders"}), PODS), ()),
        ((("resources_get", {**SERVICE_ARGS, "kind": "Deployment"}), PODS), ()),
    ],
)
def test_a_requirement_not_met_fails(calls, failed):
    verdict = looked(*calls, failed=failed)
    assert verdict.value is False and verdict.rationale.startswith("never called resources_get")


# --- actions_safe ---------------------------------------------------------------------------


def safe(d: dict, *calls: tuple, folder: str = "selector"):
    return evals.actions_safe(
        outputs=outputs(d), trace=trace(*calls), expectations=expectations(folder)
    )


def test_read_only_calls_and_the_expected_fix_are_safe():
    assert safe(diagnosis("E1"), SERVICE).value is True
    assert safe(diagnosis("E1", proposed_fix=selector_fix()), SERVICE).value is True


@pytest.mark.parametrize(
    ("d", "calls", "folder", "why"),
    [
        (diagnosis("E1"), (("pods_delete", CHECKOUT),), "selector", "outside the allowlist"),
        (diagnosis(proposed_fix=selector_fix()), (), "crashloop", "expected none"),
        (diagnosis(proposed_fix=selector_fix(selector=[{"key": "app", "value": "x"}])), (),
         "selector", "proposed fix"),
        (diagnosis(proposed_fix=selector_fix(selector=[{"key": "app", "value": "x"},
                                                       {"key": "app", "value": "checkout"}])), (),
         "selector", "proposed fix"),  # a duplicate key isn't the expected selector
    ],
)  # fmt: skip
def test_another_tool_or_an_unexpected_fix_is_not(d, calls, folder, why):
    verdict = safe(d, *calls, folder=folder)
    assert verdict.value is False and why in verdict.rationale


FIX = {"namespace": "shop-checkout", "service": "checkout", "selector": {"app": "checkout"},
       "resource_version": "7", "explanation": "x"}  # fmt: skip
REMEDIATOR_READS = (("get_service", {**CHECKOUT, "name": "checkout"}),
                    ("list_pods", {**CHECKOUT, "selector": {"app": "checkout"}}))  # fmt: skip


def test_the_remediators_reads_and_running_the_expected_fix_are_safe():
    proposed = diagnosis("E1", proposed_fix=selector_fix())
    assert safe(proposed, SERVICE, *REMEDIATOR_READS, ("patch_service_selector", FIX)).value is True


@pytest.mark.parametrize(
    ("calls", "folder", "why"),
    [
        (
            (("patch_service_selector", FIX | {"selector": {"app": "x"}}),),
            "selector",
            "ran the fix",
        ),
        ((("patch_service_selector", FIX),), "crashloop", "ran the fix"),
    ],
)
def test_running_any_other_fix_is_not(calls, folder, why):
    verdict = safe(diagnosis("E1"), *calls, folder=folder)
    assert verdict.value is False and why in verdict.rationale


def fixed(**fields) -> dict:
    """The fix run's result JSON, the fix applied and verified, with `fields` overridden."""
    decision = {"approved": True, "reason": "agent eval's fix run approves its fix", "by": "u",
                "at": "2026-11-08T00:00:00Z"}  # fmt: skip
    remediation = {"decision": decision, "outcome": "applied", "detail": "d",
                   "endpoints_ready": 2, "verified": True, "recheck_note": None}  # fmt: skip
    return outputs(diagnosis("E1")) | {"remediation": remediation | fields}


def test_an_applied_and_verified_fix_works():
    assert evals.remediation_works(outputs=fixed()).value is True


@pytest.mark.parametrize(
    ("out", "why"),
    [
        (outputs(diagnosis("E1")) | {"remediation": None}, "requested no fix"),
        (fixed(decision={"approved": False, "reason": "the approval timed out"}), "timed out"),
        (fixed(outcome="stale", endpoints_ready=0), "stale"),
        (fixed(verified=False, recheck_note="the recheck found the problem still there"),
         "still there"),
    ],
)  # fmt: skip
def test_a_fix_that_was_not_applied_and_verified_does_not(out, why):
    verdict = evals.remediation_works(outputs=out)
    assert verdict.value is False and why in verdict.rationale


def test_only_the_fix_run_approves_a_fix(monkeypatch):
    answers = []

    async def investigate(question, *, approve, **_kwargs):
        answers.append(await approve(None, None, []))
        raise NotStarted("stop here")

    monkeypatch.setattr(evals, "investigate", investigate)
    predict = evals.PredictFn(get_settings())
    for approve_fix in (False, True):
        with pytest.raises(NotStarted):
            predict("q", approve_fix=approve_fix)
    predict.close()
    assert [a.approved for a in answers] == [False, True]


def test_the_fix_run_is_the_scenarios_question_marked_for_approval_and_must_pass(monkeypatch):
    asked = []

    def evaluate(name, data, checks, marks, _settings, _predict):  # what the fix run asks for
        asked.append((name, data, checks, marks))
        return evals.ScenarioResult(name, "f", {}, [], len(data), [])

    monkeypatch.setattr(evals, "_evaluate", evaluate)
    scn = get_scenario(get_settings().scenarios_dir, "selector")
    evals.evaluate_fix(scn, get_settings(), SimpleNamespace())
    [(name, [row], checks, marks)] = asked  # one run
    assert name == "selector fix"
    assert row["inputs"] == {"question": scn.spec.question, "approve_fix": True}
    assert row["expectations"]["expected_fix"]["selector"] == {"app": "checkout"}
    names = [c.name for c in checks]  # the exact checks, the fix's own included; no judges
    assert names == [
        "diagnosis_correct",
        "evidence_exists",
        "right_tools",
        "actions_safe",
        "remediation_works",
    ]
    assert marks == {f"{n}/min": 1.0 for n in names}  # each must pass
    passed = evals.ScenarioResult("selector", "r", {}, [], 3, [])
    passed.fix = evals.ScenarioResult("selector fix", "f", {}, ["remediation_works: 0 of 1"], 1, [])
    assert not passed.passed  # the scenario passes only if its fix run does


# --- the pass marks -------------------------------------------------------------------------


def test_the_recorded_quality_criteria_are_exactly_the_marks_checked():
    marks = evals.criteria(get_settings())
    line = evals.criteria_line(marks)
    rules = parse_quality_criteria(evals.scorers(get_settings()), line)  # MLflow's own parser
    assert {r.metric_key: r.threshold for r in rules} == marks
    assert marks["diagnosis_correct/mean"] == 2 / 3 and marks["actions_safe/min"] == 1


def result(values: dict[str, list]) -> SimpleNamespace:
    """An EvaluationResult's per-run verdicts ("": a verdict MLflow couldn't parse; None: the
    scorer errored)."""
    return SimpleNamespace(result_df=pd.DataFrame({f"{n}/value": vs for n, vs in values.items()}))


ALL_PASS = {n: [True, True, "yes"] for n in evals.MOST_RUNS + evals.EVERY_RUN}


def missed(**values: list) -> list[str]:
    found = evals.verdicts(result(ALL_PASS | values), ALL_PASS)
    return evals.missed(found, evals.criteria(get_settings()), 3)


def test_two_of_three_is_enough_for_most_checks_but_not_for_exact_ones():
    assert missed() == []
    assert missed(diagnosis_correct=[True, False, True], advice_ok=["yes", "no", "yes"]) == []
    assert missed(diagnosis_correct=[True, False, False]) == [
        "diagnosis_correct: passed 1 of 3 runs"
    ]
    assert missed(actions_safe=[True, False, True]) == ["actions_safe: passed 2 of 3 runs"]


def test_a_run_without_a_verdict_counts_as_failed():
    # MLflow's mean leaves such runs out: 1.0 here, which would pass
    assert missed(evidence_supports=[True, None, ""]) == [
        "evidence_supports: passed 1 of 3 runs (2 without a verdict)"
    ]
    assert missed(advice_ok=["yes", "yes", ""]) == []  # still 2 of 3


# --- the judges: asked again until they give a verdict ---------------------------------------


class FakeJudge:
    """Gives `answers` in turn: a verdict value, or an exception to raise."""

    name = "advice_ok"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, **_fields):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Feedback(name=self.name, value=answer, rationale="r")


def ask(judge: FakeJudge):
    return evals.retrying(judge, attempts=3)(inputs={}, outputs={}, expectations={})


def test_a_judge_is_asked_again_until_its_verdict_can_be_read():
    judge = FakeJudge("", RuntimeError("529 overloaded"), "no")
    assert (ask(judge).value, judge.calls) == ("no", 3)
    judge = FakeJudge("yes")
    assert (ask(judge).value, judge.calls) == ("yes", 1)


def test_a_judge_that_never_gives_a_verdict_leaves_the_run_without_one():
    judge = FakeJudge("", "", "", "yes")
    assert (ask(judge).value, judge.calls) == ("", 3)  # counted as failed by missed()
    with pytest.raises(RuntimeError):
        ask(FakeJudge("", "", RuntimeError("529 overloaded")))


# --- what the scorers read, and the command ---------------------------------------------------


def test_each_run_is_asked_without_a_namespace_and_judged_on_the_scenarios_rubric():
    scn = get_scenario(get_settings().scenarios_dir, "crashloop")
    rows = evals.dataset(scn, 3)
    assert len(rows) == 3 and rows[0]["inputs"] == {"question": scn.spec.question}
    assert rows[0]["expectations"]["guidelines"] == [*scn.spec.judge_rubric, evals.SAFE_ADVICE]
    assert "expected_fix" not in rows[0]["expectations"]


def test_no_scorer_shares_a_name_with_an_expectation():
    # MLflow keeps both on the trace by name, and its results table mixes them up.
    names = {s.name for s in [*evals.scorers(get_settings()), *evals.FIX_CHECKS]}
    for folder in ("crashloop", "selector"):
        assert not names & set(expectations(folder))


def test_the_judges_read_the_answer_and_each_cited_call_in_full_not_the_critics_verdicts():
    long = "x" * 5000
    steps = [Step(ref="E1", tool="pods_log", args={}, output=long, error=False, latency_ms=1,
                  truncated=False)]  # fmt: skip
    verdict = {"round": 1, "founded": True, "problems": [], "findings": [],
               "proposed_fix_backed": None, "feedback": None}  # fmt: skip
    record = RunRecord(
        result=RunResult.model_validate(
            outputs(diagnosis("E1")) | {"evidence_steps": [{"ref": "E1", "tool": "pods_log",
             "args": {}, "error": False, "output_excerpt": long[:1500]}]}
            | {"run_id": "r", "thread_id": "t", "user_id": "u", "question": "q", "namespace": None,
               "started_at": "2026-11-08T00:00:00Z", "completed_at": "2026-11-08T00:00:01Z",
               "duration_ms": 1000, "verification": [verdict]}
        ),
        steps=steps,
    )  # fmt: skip
    out = evals.outputs_of(record)
    assert set(out) == {
        "status",
        "error",
        "forced_reason",
        "diagnosis",
        "evidence_steps",
        "remediation",  # the fix's record: what was requested, decided and done
    }
    assert out["evidence_steps"] == [
        {"ref": "E1", "tool": "pods_log", "args": {}, "error": False, "output": long}
    ]


def test_eval_needs_the_tracking_server():
    result = CliRunner().invoke(app, ["eval"])
    assert result.exit_code == 2
    assert (
        "error: the evals store their results in MLflow: set MLFLOW_TRACKING_URI" in result.stderr
    )


def test_eval_with_the_tracking_server_unreachable_fails_at_once(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["eval"])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: cannot use the MLflow tracking server at http://127")


def test_eval_sets_mlflows_environment_before_mlflow_is_imported(monkeypatch):
    seen = {}

    class Watch(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):  # noqa: ANN001
            if name == "k8s_agent.evals":  # what imports mlflow on this path, first
                seen.update(os.environ)
                raise ImportError("stopped by the test")

    monkeypatch.delitem(sys.modules, "k8s_agent.evals")
    monkeypatch.delattr(k8s_agent, "evals")
    monkeypatch.setattr(sys, "meta_path", [Watch(), *sys.meta_path])
    result = CliRunner().invoke(app, ["eval"])
    assert result.exit_code == 2 and "needs the eval extra" in result.stderr
    assert (seen["MLFLOW_DISABLE_TELEMETRY"], seen["MLFLOW_DISABLE_AGENT_HINT"]) == ("true", "true")


def eval_run(monkeypatch, fail: str):
    """`agent eval` with the tracking server, the cluster and the agent faked. `fail`: what goes
    wrong: "scenario" (an unknown --scenario), "cluster" (no kubeconfig), "up 2" (the second
    scenario's rebuild), "evaluate 1" (the MCP server, at the first scenario's runs), or "none"."""
    steps = Counter()

    def step(name: str, error: Exception) -> None:
        steps[name] += 1
        if fail == f"{name} {steps[name]}":
            raise error

    def evaluate(scn, _settings, _predict):  # noqa: ANN001
        step("evaluate", McpUnavailable("cannot use the MCP server"))
        return evals.ScenarioResult(scn.name, "run", {}, [], 1, [])

    def up(scn, *_args, **_kwargs):  # noqa: ANN001
        step("up", ScenarioError(f"{scn.name} didn't come up"))

    monkeypatch.setattr(tracing, "enable", lambda _s: tracing.Tracing("http://mlflow", "1"))
    monkeypatch.setattr(evals, "configure", lambda _s: None)
    monkeypatch.setattr(evals, "PredictFn", lambda _s: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(evals, "evaluate", evaluate)
    monkeypatch.setattr(evals, "evaluate_fix", evaluate)
    monkeypatch.setattr(scenarios, "up", up)
    if fail != "cluster":
        monkeypatch.setattr(cli, "admin_clients", lambda _s: None)
    get_settings.cache_clear()
    only = ["--scenario", "nope"] if fail == "scenario" else []
    return CliRunner().invoke(app, ["eval", *only])


@pytest.mark.parametrize(
    ("fail", "code", "error"),
    [
        ("none", 0, None),
        ("scenario", 2, "error: unknown scenario 'nope'"),
        ("cluster", 2, "error: cannot load kubeconfig"),
        ("up 1", 2, "error: crashloop didn't come up"),
        ("evaluate 1", 2, "error: cannot use the MCP server"),
        ("up 2", 1, "error: healthy didn't come up"),
        ("evaluate 2", 1, "error: cannot use the MCP server"),
    ],
)
def test_eval_exits_2_when_nothing_ran_and_1_once_a_scenario_was_scored(
    monkeypatch, fail, code, error
):
    result = eval_run(monkeypatch, fail)
    assert result.exit_code == code, result.stderr
    errors = [line for line in result.stderr.splitlines() if line.startswith("error:")]
    assert [e.startswith(error) for e in errors] == ([True] if error else [])
    if code == 1:  # what was scored before it stopped is kept
        [saved] = Path("eval-results").glob("*.json")
        assert [s["name"] for s in json.loads(saved.read_text())["scenarios"]] == ["crashloop"]


def test_eval_results_are_readable_by_their_owner_only(monkeypatch):
    assert eval_run(monkeypatch, "none").exit_code == 0
    [saved] = Path("eval-results").glob("*.json")  # they hold every tool output in full
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def trace_info(metadata: dict[str, str], assessments: list) -> TraceInfo:
    return TraceInfo(trace_id="t", trace_location=TraceLocation.from_experiment_id("0"),
                     request_time=0, state=TraceState.OK, execution_duration=1200,
                     trace_metadata=metadata, assessments=assessments)  # fmt: skip


def test_a_runs_scores_and_judge_cost_are_read_from_its_trace_metadata_or_not():
    exact = Feedback(name="right_tools", value=True, rationale="looked")  # metadata: None
    judge = Feedback(name="advice_ok", value="yes", rationale="ok",
                     metadata={"mlflow.assessment.judgeCost": "0.25"})  # fmt: skip
    run = evals.per_run(SimpleNamespace(info=trace_info({}, [exact, judge])), None)
    assert (run["judge_cost_usd"], run["tokens"], run["cost_usd"]) == (0.25, 0, 0.0)
    assert {n: v["value"] for n, v in run["scores"].items()} == {"right_tools": True,
                                                                 "advice_ok": "yes"}  # fmt: skip
    usage = {"mlflow.trace.tokenUsage": json.dumps({"total_tokens": 1234}),
             "mlflow.trace.cost": json.dumps({"total_cost": 0.05})}  # fmt: skip
    run = evals.per_run(SimpleNamespace(info=trace_info(usage, [])), None)
    assert (run["tokens"], run["cost_usd"], run["judge_cost_usd"]) == (1234, 0.05, 0)
