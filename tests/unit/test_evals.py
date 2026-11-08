"""The evals' exact checks and pass marks (SPEC 2.3), with hand-made answers and traces. The
judges and `mlflow.genai.evaluate` itself run in tests/acceptance/test_m5_evals.py."""

from types import SimpleNamespace

import pandas as pd
import pytest
from mlflow.entities import Feedback, SpanType
from mlflow.genai.evaluation.quality_criteria import parse_quality_criteria
from typer.testing import CliRunner

from k8s_agent import evals
from k8s_agent.agent.records import RunRecord, RunResult, Step
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from k8s_agent.scenarios import get_scenario
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
    return evals.missed(result(ALL_PASS | values), evals.criteria(get_settings()), 3)


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
    names = {scorer.name for scorer in evals.scorers(get_settings())}
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
    out = evals.answer(record)
    assert set(out) == {"status", "error", "forced_reason", "diagnosis", "evidence_steps"}
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
