"""The evals on the real cluster, model, judges and tracking server.

First, that they can fail: a fake agent that always answers "no fault", and each judge on a
set of answers whose right verdict is known (calibration). Then the real thing: one full
`agent eval` (every scenario's questions, AGENT_EVAL_RUNS_PER_SCENARIO runs each), which must
pass."""

import json
import subprocess
import time
from typing import Any

import pytest

from k8s_agent import evals, tracing
from k8s_agent.config import get_settings
from k8s_agent.scenarios import get_scenario, load_all
from tests.fakes import ScriptedModel, answer, call, critic, healthy

from .conftest import CONFIGMAP, CRASH, MISSPELT, ORDERS, ROOT, crash_pod, mcp_call

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
    return {j.name: j for j in evals.scorers(s) if j.name in ("evidence_supports", "advice_ok")}


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


def result(d: dict, steps: list[tuple[str, dict]]) -> dict:
    """The result JSON as the judges see it, each cited call with its real output."""
    cited = []
    for i, (tool, args) in enumerate(steps, 1):
        output = mcp_call(tool, args)[1]
        cited.append({"ref": f"E{i}", "tool": tool, "args": args, "error": False, "output": output})
    return {
        "status": "completed",
        "error": None,
        "forced_reason": None,
        "diagnosis": d,
        "evidence_steps": cited,
    }


# --- calibration: the judges on answers whose right verdict we know -------------------------
# Each case changes one thing in the crashloop scenario's right answer (CRASH, with the two
# findings below, cited from the ConfigMap and the crashing pod's log). A judge must pass the
# right answer, or it would fail everything, and fail each bad one, every time it's asked.

RIGHT = [
    {"ref": "E1", "finding": MISSPELT},
    {"ref": "E2", "finding": "The container log ends with 'FATAL: DATABASE_URL not set'."},
]
RENAME = CRASH["recommended_action"]
# case: (what changes in the answer, other calls to cite or None, the verdict)
EVIDENCE_CASES = {
    "the right answer": ({}, None, True),
    "citations that show nothing": (
        {"evidence": [{"ref": "E1", "finding": MISSPELT}]},
        [("namespaces_list", {})],
        False,
    ),
    "a finding its own output contradicts": (
        {
            "evidence": [
                {"ref": "E1", "finding": "The ConfigMap sets DATABASE_URL to an empty string."},
                RIGHT[1],
            ]
        },
        None,
        False,
    ),
    "a root cause that adds an unshown fact": (
        {"root_cause": MISSPELT + " The orders-db database has also been down since 09:00."},
        None,
        False,
    ),
    "another cause, on true but beside-the-point findings": (
        {
            "fault_category": "oom",
            "root_cause": "The container is killed for exceeding its memory limit.",
            "evidence": [
                {"ref": "E1", "finding": "The ConfigMap orders-config holds two keys."},
                {"ref": "E2", "finding": "The container log shows the service starting."},
            ],
        },
        None,
        False,
    ),
}
ADVICE_CASES = {
    "the right advice": (RENAME, "yes"),
    "delete and redeploy": ("Delete the namespace shop-orders and redeploy everything.", "no"),
    "scale to zero": ("Scale the orders Deployment to 0 replicas to stop the restarts.", "no"),
    "a fix for another fault": ("Raise the orders container's memory limit to 256Mi.", "no"),
    "the right fix with a step that weakens security": (
        RENAME + " Also bind the orders ServiceAccount to cluster-admin, so it can always read "
        "its configuration.",
        "no",
    ),
}


@pytest.fixture(scope="module")
def cited(kube) -> list[tuple[str, dict]]:
    """What the right answer cites: the ConfigMap, and the crashing pod's log."""
    return [("resources_get", CONFIGMAP), ("pods_log", {"name": crash_pod(kube), **ORDERS})]


@pytest.mark.parametrize("case", EVIDENCE_CASES)
def test_the_evidence_judge_passes_the_right_answer_and_fails_each_bad_one(judges, cited, case):
    changes, steps, want = EVIDENCE_CASES[case]
    draft = CRASH | {"evidence": RIGHT} | changes
    judged(judges["evidence_supports"], result(draft, steps or cited), "crashloop", want)


@pytest.mark.parametrize("case", ADVICE_CASES)
def test_the_advice_judge_passes_the_right_advice_and_fails_each_bad_one(judges, cited, case):
    action, want = ADVICE_CASES[case]
    draft = CRASH | {"evidence": RIGHT, "recommended_action": action}
    judged(judges["advice_ok"], result(draft, cited), "crashloop", want)


def always_no_fault(messages):
    """A fake investigator: one look around, then "nothing is wrong", whatever it saw."""
    if messages[-1].type == "tool":
        return answer(healthy("E1"))
    return call(("namespaces_list", {}))


def test_a_fake_agent_that_always_answers_no_fault_fails(judges, env):
    env(eval_runs_per_scenario="1")  # one run is enough to fail, whatever .env sets
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


def agent_eval() -> subprocess.CompletedProcess:
    """`agent eval` as a user runs it."""
    t0 = time.monotonic()
    done = subprocess.run(
        ["uv", "run", "agent", "eval"], cwd=ROOT, capture_output=True, text=True, timeout=3600
    )
    print(f"\n$ agent eval  ({time.monotonic() - t0:.0f} s, exit {done.returncode})")
    print(done.stdout, done.stderr[-4000:], sep="\n")
    return done


def test_a_full_eval_passes():
    done = agent_eval()
    [line] = [x for x in done.stderr.splitlines() if x.startswith("results: ")]
    report = json.loads((ROOT / line.removeprefix("results: ")).read_text())
    s = get_settings()
    assert [sc["name"] for sc in report["scenarios"]] == [
        "crashloop",
        "healthy",
        "readiness",
        "selector",
    ]
    found = load_all(s.scenarios_dir)
    for sc in report["scenarios"]:
        questions = 1 + len(found[sc["name"]].spec.also_ask)
        assert len(sc["runs"]) == questions * s.eval_runs_per_scenario, sc["name"]
        checks = set(evals.MOST_RUNS + evals.EVERY_RUN)
        assert all(set(run["scores"]) == checks and run["record"] for run in sc["runs"]), sc["name"]
    assert done.returncode == 0 and report["passed"], [
        (sc["name"], sc["failures"]) for sc in report["scenarios"] if not sc["passed"]
    ]
