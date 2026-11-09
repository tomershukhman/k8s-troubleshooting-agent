"""The `agent` command line: exit codes, stdout and stderr. Fake model and cluster; settings
isolated by conftest.py (the kubeconfig doesn't exist, so no command reaches a cluster). Under
the test runner stdout and stderr aren't terminals: the result is JSON, progress plain text."""

import asyncio
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import AIMessage
from rich.console import Console
from typer.testing import CliRunner

from k8s_agent import cli, ui
from k8s_agent.agent import Answer, ApprovalRequest, Reasoning, ToolCalled, core
from k8s_agent.agent.records import Facts, RunResult, Step
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from tests.fakes import (
    SERVICE,
    SERVICE_ARGS,
    FakeCluster,
    ScriptedModel,
    answer,
    call,
    critic,
    diagnosis,
    fix_call,
    remediator,
    selector_fix,
    verdict,
)


def agent(*args: str):
    get_settings.cache_clear()
    return CliRunner().invoke(app, list(args))


@pytest.fixture
def fake(monkeypatch):
    def use(model: ScriptedModel, judge: ScriptedModel | None = None) -> None:
        # the investigator, the critic, then the remediator: it requests the selector fix
        models = iter([model, judge or critic(), remediator(fix_call())])
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
    # no terminal on stdin here: the fix the remediator requested was rejected
    flat = " ".join(text.replace("│", " ").split())  # the panel wraps lines
    assert "Fix: rejected: no interactive approval available (by k8s-agent," in flat
    assert "Nothing was changed." in flat


def test_the_summary_shows_the_models_words_verbatim_not_as_markup(fake):
    finding = "[/] The selector [app=check-out] matches no pod."  # "[/]" alone breaks Rich markup
    d = diagnosis("E1", affected_resource={**SERVICE, "name": "check[out]"},
                  evidence=[{"ref": "E1", "finding": finding}])  # fmt: skip
    fake(ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(d)]))
    out = json.loads(agent("investigate", "q", "--quiet").stdout)
    console = Console(width=200, record=True)
    console.print(ui.summary(RunResult.model_validate(out)))
    text = console.export_text()
    assert "Service/check[out] in shop-checkout" in text and finding in text


# Cursor up, erase the line, a forged line; a bell; a C1 CSI (8-bit ESC [).
ESCAPES = "\x1b[1A\x1b[2K✓ approved\x07\x9b31m"


def test_control_characters_from_the_cluster_or_a_model_never_reach_the_terminal(fake):
    console = Console(file=io.StringIO(), width=200)
    log = Step(ref="E1", tool="pods_log", args={}, output=f"started\n{ESCAPES}", error=False,
               latency_ms=1, truncated=0)  # fmt: skip
    progress = ui.Progress(console, {"investigator": "m", "critic": "m"}, 200)
    progress(ToolCalled(log))
    progress(Reasoning(ESCAPES))
    d = diagnosis("E1", root_cause=ESCAPES, evidence=[{"ref": "E1", "finding": ESCAPES}])
    fake(ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(d)]))
    out = json.loads(agent("investigate", "q", "--quiet").stdout)
    console.print(ui.summary(RunResult.model_validate(out)))
    console.print(ui.approval(request(explanation=ESCAPES)))
    shown = console.file.getvalue()
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", shown)
    assert shown.count("\ufffd[1A\ufffd[2K✓ approved\ufffd\ufffd31m") == 5


def test_a_table_row_with_nothing_under_name_is_previewed_not_a_crash():
    table = "NAMESPACE     NAME\nshop-orders   orders-1\nkube-system          \n"
    step = Step(ref="E1", tool="t", args={}, output=table, error=False, latency_ms=1, truncated=0)
    assert ui.preview(step) == "orders-1, ?  (2 rows)"


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


# --- the fix ------------------------------------------------------------------------------


def test_without_a_terminal_on_stdin_a_fix_is_rejected_and_the_run_says_so(fake):
    proposed = diagnosis("E1", proposed_fix=selector_fix())
    fake(ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(proposed)]))
    result = agent("investigate", "q", "--user", "bob")
    assert result.exit_code == 0, result.stderr
    r = json.loads(result.stdout)["remediation"]
    assert r["request"]["args"]["selector"] == {"app": "checkout"}
    assert (r["decision"]["approved"], r["decision"]["reason"], r["decision"]["by"]) == (
        False,
        "no interactive approval available",
        "k8s-agent",
    )
    assert r["outcome"] == "not_applied" and r["verified"] is False
    assert "cannot load the remediator kubeconfig" in r["request"]["facts"]["error"]  # none here
    lines = [line for line in result.stderr.splitlines() if line[:1] != "{"]
    assert lines[-6:] == [
        "node     remediate",
        "  agent    remediator · claude-sonnet-5-5",
        "  approval Service/checkout -n shop-checkout: app=checkout",
        "    ✗ rejected: no interactive approval available (by k8s-agent)",
        "node     remediate · resumed",
        "  → not_applied: the request was rejected, so nothing was applied",
    ]


def test_with_no_terminal_on_stdin_there_is_no_prompt(monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert cli._approver(Console(), None) is None  # the agent then rejects the fix


@pytest.fixture
def terminal(monkeypatch):
    """stdin as a real terminal (a pty); returns its other end, where the person types."""
    person, tty = os.openpty()
    monkeypatch.setattr(sys, "stdin", os.fdopen(tty, "rb", buffering=0))
    yield person
    os.close(person)


def request(**args) -> ApprovalRequest:
    facts = Facts(current_selector={"app": "check-out"}, resource_version="100",
                  ready_pods=["checkout-a", "checkout-b"])  # fmt: skip
    call = fix_call().tool_calls[0]["args"] | args
    return ApprovalRequest(tool="patch_service_selector", args=call, facts=facts)


class Spinner:
    """Records when the progress display is paused, and what happened meanwhile."""

    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def paused(self):
        self.events.append("paused")
        yield
        self.events.append("resumed")


@pytest.mark.parametrize(
    ("typed", "approved"), [(b"y\n", True), (b"yes\n", True), (b"\n", False), (b"no\n", False)]
)
def test_in_a_terminal_the_change_is_shown_and_only_an_answer_typed_after_it_counts(
    typed, approved, terminal
):
    os.write(terminal, b"y\n")  # typed while the agent worked: dropped, never an answer
    err, spinner = Console(record=True, width=120), Spinner()

    async def ask():
        def answer():  # the person reads the change, then answers
            spinner.events.append("answered")
            os.write(terminal, typed)

        asyncio.get_running_loop().call_later(0.2, answer)
        return await cli._approver(err, spinner)(request(explanation="[/] Selects [app=x]."))

    how = f"answered {'yes' if approved else 'no'} in the terminal"
    assert asyncio.run(ask()) == Answer(approved, how)
    assert spinner.events == ["paused", "answered", "resumed"]  # no spinner over the prompt
    shown = err.export_text()
    assert "Service          shop-checkout/checkout" in shown
    assert "app=check-out  →  app=checkout  (replaces the whole selector)" in shown
    assert "[/] Selects [app=x]." in shown  # the remediator's words, as written: not markup
    assert "2 ready pods match app=checkout: checkout-a, checkout-b" in shown
    assert "the Service is still at version 100" in shown and "Apply this change? [y/N]" in shown


def test_a_prompt_nobody_answers_is_given_up_cleanly(terminal):
    async def unanswered():
        with pytest.raises(TimeoutError):  # the approval's own time limit (approval.py)
            await asyncio.wait_for(cli._approver(Console(), None)(request()), 0.2)
        return asyncio.get_running_loop().remove_reader(sys.stdin.fileno())

    assert asyncio.run(unanswered()) is False  # it stopped watching the terminal


def test_the_prompt_warns_when_approving_would_be_refused():
    changed = Facts(current_selector={"app": "x"}, resource_version="101", ready_pods=["p"])
    console = Console(record=True, width=200)
    console.print(ui.approval(request().model_copy(update={"facts": changed})))
    assert "changed since the remediator read it (version 101)" in console.export_text()
