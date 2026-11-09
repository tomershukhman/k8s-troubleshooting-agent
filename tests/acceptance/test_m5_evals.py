"""M5: the evals on the real cluster, model, judges and tracking server (PLAN.md M5).

First, that they can fail: a fake agent that always answers "no fault", and each judge given a
bad answer, which it must reject every time it's asked. Then the real thing: one full
`agent eval` (every scenario × 3 runs), which must pass."""

import json
import os
import subprocess
import time
from typing import Any

import pytest

from k8s_agent import evals, tracing
from k8s_agent.config import get_settings
from k8s_agent.scenarios import get_scenario
from tests.fakes import ScriptedModel, answer, call, critic, diagnosis, healthy

from .conftest import ROOT, crash_pod, mcp_call

pytestmark = pytest.mark.usefixtures("cluster")
ATTEMPTS = 2


@pytest.fixture(scope="module")
def judges() -> dict[str, Any]:
    get_settings.cache_clear()
    s = get_settings()
    if not s.mlflow_tracking_uri:
        pytest.fail("set MLFLOW_TRACKING_URI (see .env.example) to run the eval tests")
    tracing.enable(s)  # test runs go to their own experiment (conftest.py)
    evals.configure(s)
    return {j.name: j for j in evals.scorers(s)[4:]}


def judged(judge, out: dict, folder: str, want) -> None:
    """The judge gives `want` for the scenario's question and this result, every time it's
    asked (ATTEMPTS times): a judge that lets a bad answer through now and then would let it
    through a "2 of 3" mark."""
    row = evals.dataset(get_scenario(get_settings().scenarios_dir, folder), 1)[0]
    verdicts = []
    for _ in range(ATTEMPTS):
        verdict = judge(inputs=row["inputs"], outputs=out, expectations=row["expectations"])
        print(f"\n  {judge.name}: {verdict.value} ({verdict.rationale})")
        verdicts.append(verdict.value)
    assert verdicts == [want] * ATTEMPTS


ORDERS = {"namespace": "shop-orders"}
CONFIGMAP = {"apiVersion": "v1", "kind": "ConfigMap", "name": "orders-config", **ORDERS}
MISSPELT = "The ConfigMap key is misspelled: DATABSE_URL instead of DATABASE_URL."
CRASH = diagnosis(
    fault_category="bad_config",
    affected_resource={"kind": "ConfigMap", "name": "orders-config", **ORDERS},
    root_cause=MISSPELT,
    recommended_action="Rename the key to DATABASE_URL in ConfigMap orders-config.",
)


def result(d: dict, steps: list[tuple[str, dict]]) -> dict:
    """The result JSON as the judges see it, each cited call with its real output."""
    cited = []
    for i, (tool, args) in enumerate(steps, 1):
        output = mcp_call(tool, args)[1]
        cited.append({"ref": f"E{i}", "tool": tool, "args": args, "error": False, "output": output})
    return {"status": "completed", "error": None, "forced_reason": None, "diagnosis": d,
            "evidence_steps": cited}  # fmt: skip


def test_the_evidence_judge_fails_a_correct_conclusion_backed_by_irrelevant_citations(judges):
    draft = CRASH | {"evidence": [{"ref": "E1", "finding": MISSPELT}]}
    judged(
        judges["evidence_supports"], result(draft, [("namespaces_list", {})]), "crashloop", False
    )


def test_the_advice_judge_fails_a_destructive_recommendation(judges, kube):
    draft = CRASH | {
        "recommended_action": "Delete the namespace shop-orders and redeploy everything.",
        "evidence": [
            {"ref": "E1", "finding": MISSPELT},
            {"ref": "E2", "finding": "The app exits with 'DATABASE_URL not set'."},
        ],
    }
    steps = [("resources_get", CONFIGMAP), ("pods_log", {"name": crash_pod(kube), **ORDERS})]
    judged(judges["advice_ok"], result(draft, steps), "crashloop", "no")


def always_no_fault(messages):
    """A fake investigator: one look around, then "nothing is wrong", whatever it saw."""
    if messages[-1].type == "tool":
        return answer(healthy("E1"))
    return call(("namespaces_list", {}))


def test_a_fake_agent_that_always_answers_no_fault_fails(judges, env):
    env(eval_runs_per_scenario="1", eval_pass_min_runs="1")  # one run is enough to fail
    s = get_settings()
    scn = get_scenario(s.scenarios_dir, "crashloop")
    predict = evals.PredictFn(s, model=ScriptedModel(then=always_no_fault), critic_model=critic())
    try:
        got = evals.evaluate(scn, s, predict)
    finally:
        predict.close()
    print(f"\n  failures: {got.failures}")
    [run] = got.runs
    assert run["record"]["result"]["diagnosis"]["status"] == "no_fault"  # it ran, and was wrong
    scored = {name for name, score in run["scores"].items() if score["error"] is None}
    assert scored == set(evals.MOST_RUNS + evals.EVERY_RUN)  # every check scored the run
    assert not got.passed
    assert "diagnosis_correct: passed 0 of 1 runs" in got.failures


@pytest.fixture
def env(monkeypatch):
    def set_env(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(f"AGENT_{key.upper()}", value)
        get_settings.cache_clear()

    return set_env


def agent_eval(*args: str, timeout: float) -> tuple[subprocess.CompletedProcess, float]:
    """`agent eval` as a user runs it."""
    t0 = time.monotonic()
    done = subprocess.run(
        ["uv", "run", "agent", "eval", *args],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=timeout,
    )  # fmt: skip
    seconds = time.monotonic() - t0
    print(f"\n$ agent eval {' '.join(args)}  ({seconds:.0f} s, exit {done.returncode})")
    print(done.stdout, done.stderr[-4000:], sep="\n")
    return done, seconds


def test_with_the_tracking_server_stopped_eval_fails_at_once(tracking_server_stopped):
    done, seconds = agent_eval(timeout=120)
    uri = get_settings().mlflow_tracking_uri
    assert done.returncode == 2 and done.stdout == ""
    assert f"error: cannot use the MLflow tracking server at {uri}: " in done.stderr
    assert "Traceback" not in done.stderr
    assert seconds < get_settings().mlflow_http_request_timeout + 15  # start-up and one check


def test_a_full_eval_passes():
    done, _ = agent_eval(timeout=3600)
    [line] = [x for x in done.stderr.splitlines() if x.startswith("results: ")]
    report = json.loads((ROOT / line.removeprefix("results: ")).read_text())
    s = get_settings()
    assert [sc["name"] for sc in report["scenarios"]] == ["crashloop", "healthy", "readiness",
                                                           "selector"]  # fmt: skip
    for sc in report["scenarios"]:
        assert len(sc["runs"]) == s.eval_runs_per_scenario, sc["name"]
        assert all(len(run["scores"]) == 6 and run["record"] for run in sc["runs"]), sc["name"]
    assert done.returncode == 0 and report["passed"], [
        (sc["name"], sc["failures"]) for sc in report["scenarios"] if not sc["passed"]
    ]
