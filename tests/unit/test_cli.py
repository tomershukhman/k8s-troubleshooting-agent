"""The `agent` command line: exit codes, stdout and stderr. Fake model and cluster; settings
isolated by conftest.py (the kubeconfig doesn't exist, so no command reaches a cluster). Under
the test runner stdout and stderr aren't terminals: the result is JSON, progress plain text."""

import asyncio
import io
import json
import os
import re
import select
import sys
import threading
from contextlib import contextmanager

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import AIMessage
from rich.console import Console
from typer.testing import CliRunner

from k8s_agent import cli, ui
from k8s_agent.agent import Answer, ApprovalRequest, Reasoning, ToolCalled, core
from k8s_agent.agent.records import EvidenceStep, Facts, RunResult, Step
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from k8s_agent.schemas import Diagnosis
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
    def use(model: ScriptedModel, judge: ScriptedModel | None = None, fixer=None) -> None:
        # the investigator, the critic, then the remediator: it requests the selector fix
        models = iter([model, judge or critic(), fixer or remediator(fix_call())])
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
        "             Deployment (14 lines of YAML)",  # the fake's resources_get, managedFields cut
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
    # each place it went in (the log, the reasoning, the root cause, the finding, the
    # explanation): the sequences gone, the bell replaced
    assert shown.count("✓ approved\ufffd") == 5


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("\x1b[31mERROR\x1b[0m boom", "ERROR boom"),  # colours in a pod's log
        ("\x1b[38;5;196mred\x1b[m", "red"),
        ("\x9b1mbold\x9b0m", "bold"),  # the 8-bit CSI
        ("\x1b]0;a title\x07log", "log"),  # an OSC ended by BEL
        ("\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\", "link"),  # a link, ended by ST
        ("a\x1bb\x07c\x1b]no end", "a\ufffdb\ufffdc\ufffd]no end"),  # no sequence: replaced
        ("tab\tand\nnewline", "tab\tand\nnewline"),
    ],
)
def test_escape_sequences_are_removed_and_other_control_characters_replaced(text, shown):
    assert ui.safe(text) == shown


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


def test_an_unreachable_mcp_server_is_one_clear_message_and_exit_2(monkeypatch):
    # the MCP address is one where nothing listens (tests/unit/conftest.py)
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


@pytest.fixture
def terminal(monkeypatch):
    """stdin as a real terminal (a pty); returns its other end, where the person types."""
    person, tty = os.openpty()
    monkeypatch.setattr(sys, "stdin", os.fdopen(tty, "rb", buffering=0))
    yield person
    os.close(person)


def request(**args) -> ApprovalRequest:
    owners = dict.fromkeys(["checkout-a", "checkout-b"], "ReplicaSet/checkout-1")
    facts = Facts(current_selector={"app": "check-out"}, resource_version="100", owners=owners)
    call = fix_call().tool_calls[0]["args"] | args
    return ApprovalRequest(tool="patch_service_selector", args=call, facts=facts)


# What the approver gets besides the request: the verified diagnosis and its cited tool calls.
VERIFIED = (
    Diagnosis(**diagnosis("E1", proposed_fix=selector_fix())),
    [EvidenceStep(ref="E1", tool="resources_get", args=SERVICE_ARGS, error=False,
                  output_excerpt="kind: Service")],
)  # fmt: skip


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
        explained = request(explanation="[/] Selects [app=x].")
        return await cli._approver(err, spinner)(explained, *VERIFIED)

    reason = f"answered {'yes' if approved else 'no'} in the terminal"
    assert asyncio.run(ask()) == Answer(approved, reason)
    assert spinner.events == ["paused", "answered", "resumed"]  # no spinner over the prompt
    shown = err.export_text()
    # first the diagnosis the fix comes from, as the summary shows it; then the change; then y/N
    order = [
        "Fault found · selector_mismatch · Service/checkout in shop-checkout",
        "The Service selects app=check-out; the pods are labelled app=checkout.",
        "E1   finding E1",
        "resources_get Service/checkout -n shop-checkout",
        "Next step: Set the Service selector to app=checkout.",
        "Approve this fix?",
        "Apply this change? [y/N]",
    ]
    at = [shown.index(text) for text in order]
    assert at == sorted(at)
    assert "Service          shop-checkout/checkout" in shown
    assert "app=check-out  →  app=checkout  (replaces the whole selector)" in shown
    assert "[/] Selects [app=x]." in shown  # the remediator's words, as written: not markup
    assert "2 ready pods match app=checkout: checkout-a (ReplicaSet/checkout-1)," in shown
    assert "the Service is still at version 100" in shown and "Apply this change? [y/N]" in shown


def test_a_prompt_nobody_answers_is_given_up_cleanly(terminal):
    async def unanswered():
        with pytest.raises(TimeoutError):  # the approval's own time limit (approval.py)
            await asyncio.wait_for(cli._approver(Console(), None)(request(), *VERIFIED), 0.2)
        return asyncio.get_running_loop().remove_reader(sys.stdin.fileno())

    assert asyncio.run(unanswered()) is False  # it stopped watching the terminal


@pytest.mark.parametrize(
    ("facts", "warning"),
    [
        (Facts(current_selector={"app": "x"}, resource_version="101", owners={"p": "RS/p"}),
         "The Service changed since the remediator read it (version 101): approving will be "
         "refused as stale."),
        (Facts(error="403 Forbidden"),
         "Couldn't check the Service as the remediator: 403 Forbidden"),
        (Facts(current_selector={"app": "x"}, resource_version="100"),
         "No ready pod matches the new selector: approving will be refused."),
    ],
    ids=["stale", "unreadable", "no ready pod"],
)  # fmt: skip
def test_the_prompt_warns_when_approving_would_be_refused(facts, warning):
    console = Console(record=True, width=200)
    console.print(ui.approval(request().model_copy(update={"facts": facts})))
    assert warning in console.export_text()


class Terminal:
    """A pseudo-terminal, as a person sees it: everything shown on it, and (when it is stdin)
    `answer` typed once the y/N prompt is up."""

    def __init__(self, answer: bytes | None = b"n\n") -> None:
        self.person, self.tty = os.openpty()
        self.answer, self.seen, self.done = answer, b"", threading.Event()
        self.watcher = threading.Thread(target=self.watch)
        self.watcher.start()

    def watch(self) -> None:
        while (ready := select.select([self.person], [], [], 0.05)[0]) or not self.done.is_set():
            if ready:
                self.seen += os.read(self.person, 65536)
            if self.answer and b"Apply this change? [y/N]" in self.seen:
                os.write(self.person, self.answer)
                self.answer = None

    def file(self, mode: str):
        if "b" in mode:
            return open(self.tty, mode, buffering=0, closefd=False)
        return open(self.tty, mode, encoding="utf-8", closefd=False)

    def shown(self) -> str:
        """What it showed, without colours or carriage returns."""
        self.done.set()
        self.watcher.join()
        os.close(self.person)
        os.close(self.tty)
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.seen.decode()).replace("\r", "")


@pytest.fixture
def investigate_in(monkeypatch, env):
    """`agent investigate` in-process (so with the fakes), with stdin and stderr on `terminal`
    and stdout too unless given, as a person runs it."""
    env(approval_timeout_seconds="10")  # a prompt that never shows fails the test, soon
    monkeypatch.setenv("COLUMNS", "120")

    def run(terminal: Terminal, *, stdout=None, as_json: bool = False) -> None:
        out, err = stdout or terminal.file("w"), terminal.file("w")
        with monkeypatch.context() as m:
            m.setattr(sys, "stdin", terminal.file("rb"))
            m.setattr(sys, "stdout", out)
            m.setattr(sys, "stderr", err)
            try:
                cli.investigate("q", user="alice", as_json=as_json, quiet=True)
            finally:
                out.flush()
                err.flush()

    return run


def proposing(fake, fixer=None) -> None:
    proposed = diagnosis("E1", proposed_fix=selector_fix())
    fake(ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(proposed)]),
         fixer=fixer)  # fmt: skip


ROOT_CAUSE = "The Service selects app=check-out; the pods are labelled app=checkout."


@pytest.mark.parametrize(
    "elsewhere", [False, True], ids=["stdout the terminal", "stdout elsewhere"]
)
def test_the_prompt_shows_the_diagnosis_first_and_the_end_repeats_it_only_for_another_stdout(
    elsewhere, fake, investigate_in
):
    proposing(fake)
    terminal = Terminal()
    other = Terminal(answer=None) if elsewhere else None  # its watcher stops once read
    investigate_in(terminal, stdout=other.file("w") if other else None)
    shown = terminal.shown()
    prompt = shown.index("Apply this change? [y/N] n")
    before = shown[:prompt]
    # nothing before the diagnosis (--quiet), and the diagnosis before the change
    assert before.startswith("╭─ Fault found · selector_mismatch · Service/checkout in shop-")
    assert before.index(ROOT_CAUSE) < before.index("Next step:") < before.index("Approve this fix?")
    assert "resources_get Service/checkout -n shop-checkout" in before  # where E1 came from
    assert "Proposed fix:" not in before  # the change panel says it, once
    end = other.shown() if other else shown[prompt:]
    flat = " ".join(end.replace("│", " ").split())  # the panel wraps lines
    assert "Fix: rejected: answered no in the terminal (by alice," in flat
    if other:  # the full summary: that output may be read without the terminal
        assert "Fix:" not in shown
        assert ROOT_CAUSE in flat and "resources_get Service/checkout -n shop-checkout" in flat
        assert "Next step: Set the Service selector to app=checkout." in flat
        assert "Proposed fix: set the selector of Service shop-checkout/checkout" in flat
    else:  # the fix's outcome only, in a panel with the same title
        assert "Fault found · selector_mismatch" in end and ROOT_CAUSE not in end
        assert "Next step:" not in end and "E1 " not in end


def test_with_json_the_prompt_shows_the_diagnosis_and_stdout_is_the_result_json_alone(
    fake, investigate_in, tmp_path
):
    proposing(fake)
    terminal = Terminal()
    with open(tmp_path / "out.json", "w") as stdout:
        investigate_in(terminal, stdout=stdout, as_json=True)
    shown, out = terminal.shown(), (tmp_path / "out.json").read_text()
    assert shown.index(ROOT_CAUSE) < shown.index("Approve this fix?") < shown.index("[y/N] n")
    assert "Fix:" not in shown  # the result went to stdout
    result = RunResult.model_validate_json(out)
    assert out == result.model_dump_json() + "\n"  # exactly the result: nothing added
    assert set(json.loads(out)["remediation"]["request"]) == {"tool", "args", "facts"}
    assert result.remediation.decision.reason == "answered no in the terminal"


@pytest.mark.parametrize(
    "make_fixer",  # a factory: a model's script is used up by one run
    [
        None,  # no fix proposed
        lambda: remediator(fix_call(selector={"app": "other"})),  # code rejects it unasked
    ],
    ids=["no fix proposed", "another fix requested"],
)
def test_a_run_whose_fix_nobody_is_asked_about_shows_only_the_summary(
    make_fixer, fake, investigate_in
):
    if make_fixer is None:
        fake(ScriptedModel(script=[call(("resources_get", SERVICE_ARGS)), answer(diagnosis("E1"))]))
    else:
        proposing(fake, make_fixer())
    terminal = Terminal()
    investigate_in(terminal)
    shown = terminal.shown()
    assert shown.startswith("╭─ Fault found · selector_mismatch")  # the summary, and only it
    assert shown.count(ROOT_CAUSE) == 1 and "Next step:" in shown
    assert "Approve this fix?" not in shown and "[y/N]" not in shown
