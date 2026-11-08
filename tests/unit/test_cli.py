"""The `agent` command line: exit codes, stdout and stderr. Fake model and cluster; settings
isolated by conftest.py (the kubeconfig doesn't exist, so no command reaches a cluster). Under
the test runner stdout and stderr aren't terminals: the result is JSON, progress plain text."""

import json
import re

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import AIMessage
from rich.console import Console
from typer.testing import CliRunner

from k8s_agent import ui
from k8s_agent.agent import core
from k8s_agent.agent.records import RunResult, Step
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from tests.fakes import (
    SERVICE_ARGS,
    FakeCluster,
    ScriptedModel,
    answer,
    call,
    critic,
    diagnosis,
    selector_fix,
    verdict,
)


def agent(*args: str):
    get_settings.cache_clear()
    return CliRunner().invoke(app, list(args))


@pytest.fixture
def fake(monkeypatch):
    def use(model: ScriptedModel, judge: ScriptedModel | None = None) -> None:
        models = iter([model, judge or critic()])  # the investigator, then the critic
        monkeypatch.setattr(core, "make_model", lambda _settings, _name: next(models))
        monkeypatch.setattr(core, "mcp_client", lambda _settings: FakeCluster().server)

    return use


def selector_run(fake) -> None:
    thinking = AIMessage(
        content=[{"type": "thinking", "thinking": "Compare the selector with the pod labels."}],
        tool_calls=call(("resources_get", SERVICE_ARGS)).tool_calls,
        response_metadata={"model_provider": "anthropic"},
    )
    model = ScriptedModel(script=[thinking, answer(diagnosis("E1")), answer(diagnosis("E1"))])
    fake(model, critic(verdict("E1", founded=False, feedback="E1 alone doesn't show the labels.")))


def test_progress_shows_each_agent_at_work_and_the_result_json_goes_to_stdout(fake, env):
    env(model="anthropic:investigator-model", critic_model="anthropic:critic-model")
    selector_run(fake)
    result = agent("investigate", "Why is checkout down?", "--user", "alice")
    assert result.exit_code == 0, result.stderr
    out = json.loads(result.stdout)  # not a terminal: stdout is exactly one JSON document
    assert (out["user_id"], out["diagnosis"]["status"]) == ("alice", "fault_found")
    assert [v["founded"] for v in out["verification"]] == [False, True]
    # not a terminal either: the JSON log lines are on stderr too, between the progress lines
    logs = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert logs[0]["event"] == "run_started" and logs[-1]["event"] == "run_finished"
    lines = [
        re.sub(r"\d+ ms$", "N ms", line) for line in result.stderr.splitlines() if line[:1] != "{"
    ]
    assert lines == [
        "Investigating: Why is checkout down?  (as alice)",
        "node     intake",
        "node     investigate",
        "  agent    investigator · investigator-model",
        "    thinking Compare the selector with the pod labels.",
        "    tool     resources_get(Service/checkout -n shop-checkout)  E1 · N ms",
        "             Deployment (14 lines of YAML)",
        "    draft    fault_found · selector_mismatch · Service/checkout · cites E1",
        "node     verify_evidence",
        "  check    code checks passed",
        "  agent    critic · critic-model",
        "    ✗ E1  r",
        "  → not founded: E1 alone doesn't show the labels.",
        "node     investigate · round 2",
        "  agent    investigator · investigator-model",
        "    draft    fault_found · selector_mismatch · Service/checkout · cites E1",
        "node     verify_evidence · round 2",
        "  check    code checks passed",
        "  agent    critic · critic-model",
        "    ✓ E1  r",
        "  → founded",
        "node     result",
    ]


def test_quiet_silences_progress_but_not_the_result(fake):
    fake(ScriptedModel(script=[call(("namespaces_list", {})), answer(diagnosis("E1"))]))
    result = agent("investigate", "q", "--quiet")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["diagnosis"]["status"] == "fault_found"
    assert [line for line in result.stderr.splitlines() if not line.startswith("{")] == []


def test_the_summary_says_what_is_wrong_the_evidence_and_what_to_do(fake):
    fix = selector_fix()
    model = ScriptedModel(
        script=[call(("resources_get", SERVICE_ARGS)), answer(diagnosis("E1", proposed_fix=fix))]
    )
    fake(model)
    out = json.loads(agent("investigate", "q", "--quiet").stdout)
    console = Console(width=100, record=True)
    console.print(ui.summary(RunResult.model_validate(out)))
    text = console.export_text()
    assert "Fault found · selector_mismatch · Service/checkout in shop-checkout" in text
    assert "The Service selects app=check-out" in text
    assert "E1   finding E1" in text and "resources_get Service/checkout -n shop-checkout" in text
    assert "Next step: Set the Service selector to app=checkout." in text
    assert (
        "Proposed fix: set the selector of Service shop-checkout/checkout to app=checkout." in text
    )
    assert "Nothing has been changed." in text


READER = 'User "system:serviceaccount:k8s-agent:reader"'


@pytest.mark.parametrize(
    ("output", "line"),
    [
        (  # the texts kubernetes-mcp-server returns
            f"failed to list pods in namespace default: pods is forbidden: {READER} cannot list "
            'resource "pods" in API group "" in the namespace "default"',
            "no access: can't list pods in namespace default",
        ),
        (
            f"failed to list namespaces: namespaces is forbidden: {READER} cannot list resource "
            '"namespaces" in API group "" at the cluster scope',
            "no access: can't list namespaces cluster-wide",
        ),
        ('failed to get resource: services "checkout" not found', None),
    ],
)
def test_a_refused_call_is_shown_as_no_access_naming_what_and_where(output, line):
    step = Step(ref="E1", tool="t", args={}, output=output, error=True, latency_ms=1, truncated=0)
    assert ui.refused(step) == line


def test_a_failed_run_prints_its_result_and_exits_1(fake):
    fake(ScriptedModel(script=[ModelRateLimitError("rate limited")]))
    result = agent("investigate", "q", "--quiet")
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert (out["status"], out["error"]) == (
        "failed",
        "model API error: ModelRateLimitError: rate limited",
    )


def test_an_inconclusive_run_still_exits_0(fake, env):
    env(max_model_calls="1")
    fake(ScriptedModel(then=lambda _: call(("namespaces_list", {}))))
    result = agent("investigate", "q", "--quiet")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["diagnosis"]["status"] == "inconclusive"


def test_an_unreachable_mcp_server_is_one_clear_message_and_exit_2(env, monkeypatch):
    env(mcp_url="http://127.0.0.1:9/mcp")
    monkeypatch.setattr(core, "make_model", lambda _settings, _name: ScriptedModel())
    result = agent("investigate", "q")
    assert (result.exit_code, result.stdout) == (2, "")
    [error] = [line for line in result.stderr.splitlines() if line.startswith("error:")]
    assert error.startswith("error: cannot use the MCP server at http://127.0.0.1:9/mcp: ")
    assert error.endswith("Start it with: docker compose up -d k8s-mcp")


def test_invalid_settings_exit_2_for_investigate_and_1_elsewhere(env):
    env(max_tool_calls="0")
    assert agent("investigate", "q").exit_code == 2
    result = agent("setup")
    assert result.exit_code == 1 and result.stderr.startswith("error: invalid settings")


def test_scenario_list_shows_each_scenario_with_its_neutral_namespace():
    result = agent("scenario", "list")
    assert result.exit_code == 0
    assert [line.split()[:2] for line in result.stdout.splitlines()] == [
        ["crashloop", "shop-orders"],
        ["healthy", "shop-inventory"],
        ["readiness", "shop-catalog"],
        ["selector", "shop-checkout"],
    ]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["scenario", "up", "nope"], "error: unknown scenario 'nope'"),
        (["scenario", "down", "selector"], "error: cannot load kubeconfig"),
        (["setup"], "error: cannot load kubeconfig"),
    ],
)
def test_cluster_commands_fail_with_a_clean_error(args, error):
    result = agent(*args)
    assert result.exit_code == 1 and result.stderr.startswith(error)
