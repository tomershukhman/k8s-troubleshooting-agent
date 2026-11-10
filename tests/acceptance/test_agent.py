"""The agent's parts against the real cluster and model: real tool output through the
middleware, the real critic's verdicts, the MCP server's own error wording, and one real run in a
namespace the agent can't read. Model answers vary, so each real-model check gets one retry;
every attempt is printed."""

import asyncio

import pytest

from k8s_agent.agent import RunRecord, investigate, tool_output
from k8s_agent.agent.core import make_model
from k8s_agent.agent.critic import review, with_verdict
from k8s_agent.agent.records import Step
from k8s_agent.config import get_settings
from k8s_agent.schemas import Diagnosis
from tests.fakes import ScriptedModel, answer, call, critic, diagnosis, selector_fix

from .conftest import CONFIGMAP, CRASH, MISSPELT, ORDERS, crash_pod, mcp_call

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


def test_a_real_deployment_reaches_the_agent_with_status_and_without_managed_fields():
    # A scripted investigator makes the exact call: the real MCP server plus our middleware.
    args = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "checkout",
        "namespace": "shop-checkout",
    }
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
    # The server drops managedFields (often half an object's YAML), so the agent needn't.
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


CRASH_Q = "The orders service keeps restarting. What's wrong?"


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
            and "RoleBinding" in d.recommended_action
        )
        return [] if ok else [d]

    retried(check)
