"""M3: the investigation graph on the real model and the real cluster (PLAN.md M3). Model
answers vary, so each real-model check gets one retry; every attempt is printed."""

import asyncio
import subprocess

import pytest

from k8s_agent import evals
from k8s_agent.agent import RunRecord, RunStatus, investigate, tool_output
from k8s_agent.agent.core import make_model
from k8s_agent.agent.critic import review, with_verdict
from k8s_agent.agent.records import Step
from k8s_agent.config import get_settings
from k8s_agent.policy import READ_ONLY_TOOLS
from k8s_agent.scenarios import Scenario, get_scenario
from k8s_agent.schemas import Diagnosis
from tests.fakes import ScriptedModel, answer, call, critic, diagnosis, selector_fix

from .conftest import FOLDERS, ROOT, compose, crash_pod, mcp_call

pytestmark = pytest.mark.usefixtures("cluster")
ATTEMPTS = 2
# One event loop for every model call here: langchain-anthropic shares one async HTTP client per
# process (lru_cache), and its pooled connections belong to the loop that opened them, so a new
# asyncio.run() per call can pick up dead ones ("Event loop is closed").
LOOP = asyncio.Runner()


@pytest.fixture(scope="module", autouse=True)
def close_loop():
    yield
    LOOP.close()


def run(question: str, **kwargs) -> RunRecord:
    get_settings.cache_clear()
    return report(LOOP.run(investigate(question, settings=get_settings(), **kwargs)))


def report(record: RunRecord) -> RunRecord:
    r = record.result
    print(f"\n  {r.status} in {r.duration_ms} ms; {len(record.steps)} tool calls")
    for s in record.steps:
        print(f"    [{s.ref}] {s.tool} {s.args}{' ERROR' if s.error else ''}")
    for v in r.verification:
        print(f"    verify {v.round}: founded={v.founded} {v.problems or v.feedback or ''}")
    print("  " + (r.diagnosis.model_dump_json() if r.diagnosis else f"error: {r.error}"))
    return record


def retried(check):
    """Run `check` (returns a list of problems) up to ATTEMPTS times; pass on a clean one."""
    attempts = []
    for _ in range(ATTEMPTS):
        attempts.append(check())
        if not attempts[-1]:
            return
    pytest.fail(f"no attempt passed: {attempts}")


def problems(record: RunRecord, scn: Scenario) -> list[str]:
    """Why this run doesn't match the scenario's expected answer (empty = it does)."""
    r = record.result
    if r.status is not RunStatus.completed:
        return [f"run {r.status}: {r.error}"]
    d, found = r.diagnosis, []
    if missing := set(d.refs) - {s.ref for s in record.steps}:
        found.append(f"cited refs that don't exist: {missing}")
    if outside := {s.tool for s in record.steps} - READ_ONLY_TOOLS:
        found.append(f"called tools outside the allowlist: {outside}")
    if r.duration_ms > get_settings().run_max_seconds * 1000:
        found.append(f"took {r.duration_ms} ms")
    # status, category, resource, and for no fault evidence from its namespace: the evals' own
    # check, so M3 and M5 can't drift apart (the evals' other checks read the run's trace)
    out, expected = evals.outputs_of(record), evals.expectations(scn)
    if not (correct := evals.diagnosis_correct(outputs=out, expectations=expected)).value:
        found.append(correct.rationale)
    fix, want_fix = d.proposed_fix, scn.spec.expected_fix
    got = (fix.namespace, fix.service, fix.selector_dict()) if fix else None
    if got != ((want_fix.namespace, want_fix.service, want_fix.selector) if want_fix else None):
        found.append(f"proposed fix {got}, expected {want_fix}")
    return found


@pytest.fixture(scope="module")
def first_attempts(cluster) -> dict[str, RunRecord]:
    """Every scenario's question, asked once, all at the same time: they're independent (each
    in its own namespace), so the wait is the slowest run, not the sum."""
    get_settings.cache_clear()
    s = get_settings()
    questions = [get_scenario(s.scenarios_dir, f).spec.question for f in FOLDERS]

    async def all_at_once():
        return await asyncio.gather(*(investigate(q, settings=s) for q in questions))

    return dict(zip(FOLDERS, LOOP.run(all_at_once()), strict=True))


@pytest.mark.parametrize("folder", FOLDERS)
def test_the_graph_diagnoses_each_scenario_without_a_namespace(folder, first_attempts):
    scn = get_scenario(get_settings().scenarios_dir, folder)
    # no namespace given: it must find it. A failed attempt is retried, alone.
    attempts = [problems(report(first_attempts[folder]), scn)]
    while attempts[-1] and len(attempts) < ATTEMPTS:
        attempts.append(problems(run(scn.spec.question), scn))
    assert not attempts[-1], f"no attempt passed: {attempts}"


def test_a_real_deployment_reaches_the_agent_with_status_and_without_managed_fields(cluster):
    # A scripted investigator makes the exact call: the real MCP server plus our middleware.
    args = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "checkout",
        "namespace": "shop-checkout",
    }
    deployments = cluster.dynamic.resources.get(api_version="apps/v1", kind="Deployment")
    assert deployments.get(
        name="checkout", namespace="shop-checkout"
    ).metadata.managedFields  # not a no-op
    model = ScriptedModel(script=[call(("resources_get", args)), answer(diagnosis("E1"))])
    run("Why is checkout down?", model=model, critic_model=critic())
    [seen] = [m for m in model.received[1] if m.type == "tool"]
    assert seen.status == "success" and seen.content.startswith(
        "[E1] apiVersion: apps/v1\nkind: Deployment"
    )
    assert (
        "\nspec:\n" in seen.content
        and "\nstatus:\n" in seen.content
        and "availableReplicas: 2" in seen.content
    )
    assert "managedFields" not in seen.content


# --- the critic, with the real model, on real tool outputs --------------------------------


def step(ref: str, tool: str, args: dict) -> Step:
    _, output = mcp_call(tool, args)
    return Step(
        ref=ref, tool=tool, args=args, output=output, error=False, latency_ms=1, truncated=False
    )


def verdict_on(draft: dict, steps: list[Step], question: str):
    s = get_settings()
    real = with_verdict(make_model(s, s.effective_critic_model))
    verdict = LOOP.run(review(real, question, Diagnosis.model_validate(draft), steps))
    print(f"\n  verdict: {verdict.model_dump_json()}")
    return verdict


ORDERS = {"namespace": "shop-orders"}
CONFIGMAP = {"apiVersion": "v1", "kind": "ConfigMap", "name": "orders-config", **ORDERS}
MISSPELT = "The ConfigMap key is misspelled: DATABSE_URL instead of DATABASE_URL."
CRASH_Q = "The orders service keeps restarting. What's wrong?"
CRASH = diagnosis(
    fault_category="bad_config",
    affected_resource={"kind": "ConfigMap", "namespace": "shop-orders", "name": "orders-config"},
    root_cause=MISSPELT,
    recommended_action="Rename the key to DATABASE_URL in ConfigMap orders-config.",
)


def test_the_critic_rejects_an_irrelevant_citation():
    draft = CRASH | {"evidence": [{"ref": "E1", "finding": MISSPELT}]}
    steps = [step("E1", "namespaces_list", {})]

    def check():
        v = verdict_on(draft, steps, CRASH_Q)
        return [] if not v.founded and [f.supported for f in v.findings] == [False] else [v]

    retried(check)


def test_the_critic_accepts_the_finding_when_the_citations_establish_it(cluster):
    # The control: the rejection above is about the citation, not a critic that rejects all.
    deployment = {**CONFIGMAP, "apiVersion": "apps/v1", "kind": "Deployment", "name": "orders"}
    steps = [
        step("E2", "resources_get", CONFIGMAP),
        step("E3", "pods_log", {"name": crash_pod(cluster), **ORDERS}),
        step("E4", "resources_get", deployment),
    ]
    draft = CRASH | {
        "evidence": [
            {"ref": "E2", "finding": MISSPELT},
            {"ref": "E3", "finding": "The app exits with 'DATABASE_URL not set'."},
            {"ref": "E4", "finding": "The container loads its environment from orders-config."},
        ]
    }

    def check():
        v = verdict_on(draft, steps, CRASH_Q)
        return [] if v.founded and all(f.supported for f in v.findings) else [v]

    retried(check)


def test_the_critic_rejects_a_fix_the_evidence_does_not_back():
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "name": "checkout",
        "namespace": "shop-checkout",
    }
    fix = selector_fix(selector=[{"key": "app", "value": "checkout-v2"}])
    draft = diagnosis(proposed_fix=fix) | {
        "evidence": [{"ref": "E1", "finding": "The Service selects app=check-out."}]
    }
    steps = [step("E1", "resources_get", service)]

    def check():
        v = verdict_on(draft, steps, "The checkout service isn't reachable from other pods. Why?")
        return (
            []
            if v.proposed_fix is not None and not v.proposed_fix.backed and not v.founded
            else [v]
        )

    retried(check)


CHECKOUT = {"namespace": "shop-checkout"}


@pytest.mark.parametrize(
    ("tool", "args"),
    [  # one of each error a retry can't fix, as the real server words it
        ("pods_list_in_namespace", {"namespace": "kube-system"}),  # refused
        ("resources_list", {**CHECKOUT, "apiVersion": "v1", "kind": "Secret"}),  # MCP's denylist
        ("pods_log", {**CHECKOUT, "name": "nope"}),  # no such pod
        ("resources_get", {**CHECKOUT, "apiVersion": "v1", "kind": "Service", "name": "nope"}),
        ("resources_list", {**CHECKOUT, "apiVersion": "v1", "kind": "Foo"}),  # no such kind
        ("pods_list_in_namespace", {}),  # a required argument is missing
    ],
)
def test_the_real_servers_permanent_errors_are_recognised_so_not_retried(tool, args):
    # The MCP server gives no error code; tool_output.PERMANENT reads its text. A server
    # upgrade that rewords these fails here (they'd be retried: slower, never wrong).
    err, text = mcp_call(tool, args)
    assert err and tool_output.PERMANENT.search(text), text
    if "forbidden" in text:
        assert tool_output.REFUSED.search(text), text  # the progress line and the reason


def test_a_namespace_the_agent_cannot_read_gives_inconclusive_not_a_guess():
    def check():
        record = run("Is the payments service healthy?", namespace="default")
        d = record.result.diagnosis
        refused = any(s.error and "forbidden" in s.output for s in record.steps)
        # the agent's own answer (not a limit), the refusal on record, and how to grant access
        ok = (
            d
            and d.status == "inconclusive"
            and not record.forced_reason
            and refused
            and "RoleBinding" in d.root_cause
        )
        return [] if ok else [d]

    retried(check)


def test_with_the_mcp_server_stopped_the_cli_fails_with_the_clear_message(request):
    request.addfinalizer(lambda: compose("up", "-d", "--wait", "k8s-mcp"))
    compose("stop", "k8s-mcp")
    # the real command, so a traceback would show on its stderr
    result = subprocess.run(
        ["uv", "run", "agent", "investigate", "Is the inventory service healthy?"],
        cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    print(result.stderr)
    assert (result.returncode, result.stdout) == (2, "")
    assert f"error: cannot use the MCP server at {get_settings().mcp_endpoint}: " in result.stderr
    assert "docker compose up -d k8s-mcp" in result.stderr and "Traceback" not in result.stderr
