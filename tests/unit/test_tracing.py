"""Tracing (SPEC §3.7) with no tracking server: off unless configured, and a server that can't be
reached neither fails a run nor holds up exit. The traces themselves are checked on the real
server in tests/acceptance/test_m4_tracing.py."""

import json
import os
import re
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from k8s_agent import tracking_server
from k8s_agent.agent import core
from k8s_agent.cli import app
from k8s_agent.config import get_settings
from tests.fakes import FakeCluster, FakeKube, ScriptedModel, answer, call, critic, diagnosis

STOPPED = "http://127.0.0.1:9"  # nothing listens: every connection is refused


def investigate(monkeypatch):
    models = iter([ScriptedModel(script=[call(("namespaces_list", {})), answer(diagnosis("E1"))]),
                   critic(), ScriptedModel()])  # fmt: skip
    monkeypatch.setattr(core, "make_model", lambda _settings, _name: next(models))
    monkeypatch.setattr(core, "mcp_client", lambda _settings: FakeCluster().server)
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["investigate", "q", "--quiet"])
    assert result.exit_code == 0, result.stderr
    lines = result.stderr.splitlines()  # not a terminal: the JSON logs are on stderr too
    said = [line for line in lines if not line.startswith("{")]
    logged = [json.loads(line)["event"] for line in lines if line.startswith("{")]
    return json.loads(result.stdout), said, logged


def test_without_a_tracking_uri_nothing_is_traced(monkeypatch, tmp_path):
    result, said, logged = investigate(monkeypatch)
    assert result["trace_id"] is None and said == []  # no warning, no trace link
    assert "tracing_unavailable" not in logged
    assert list(tmp_path.iterdir()) == []  # nor a local trace store in the working directory


def test_an_unreachable_tracking_server_is_one_warning_and_the_run_goes_on_untraced(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", STOPPED)
    t0 = time.monotonic()
    result, said, logged = investigate(monkeypatch)
    assert time.monotonic() - t0 < 10  # MLflow's default retries a refused call for minutes
    assert result["diagnosis"]["status"] == "fault_found" and result["trace_id"] is None
    [warning] = said
    assert warning.startswith(f"warning: cannot use the MLflow tracking server at {STOPPED}: ")
    assert warning.endswith("; investigating without tracing")
    assert logged.count("tracing_unavailable") == 1


# The start check passed, then the server went away: the run's spans can't be sent. In a fresh
# process, so the exit (MLflow flushes again at exit) is what's timed.
SERVER_GONE = f"""
from unittest import mock
import mlflow
from mlflow.tracking import fluent
from k8s_agent import tracing
from k8s_agent.config import Settings

def start_check_passed(name):  # what a real set_experiment leaves behind
    fluent._active_experiment_id = "0"
    return mock.Mock(experiment_id="0")

with mock.patch.object(mlflow, "set_experiment", start_check_passed):
    assert tracing.enable(Settings(MLFLOW_TRACKING_URI="{STOPPED}"))
with tracing.run("investigation", {{}}, user="u", session="s", metadata={{}}) as span:
    assert span is not None  # a real trace, so there is something to send
tracing.flush()
"""


def test_a_tracking_server_that_goes_away_cannot_hold_up_exit(tmp_path):
    t0 = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", SERVER_GONE],
        cwd=tmp_path, env=dict(os.environ), capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert time.monotonic() - t0 < 15, done.stderr  # with MLflow's defaults: minutes


@pytest.mark.parametrize(
    ("helm", "run", "error"),
    [
        (None, None, "helm not found: install Helm 4"),
        (
            "/bin/helm",
            subprocess.CompletedProcess([], 1, "", "Error: UPGRADE FAILED: context deadline\n"),
            "helm could not install the MLflow tracking server: Error: UPGRADE FAILED: context",
        ),
    ],
)
def test_a_failed_tracking_server_install_says_why(monkeypatch, helm, run, error):
    monkeypatch.setattr(tracking_server.shutil, "which", lambda _name: helm)
    monkeypatch.setattr(tracking_server.subprocess, "run", lambda *_args, **_kw: run)
    with pytest.raises(tracking_server.InstallError, match=error):
        tracking_server.install(get_settings(), FakeKube().clients, lambda _msg: None)


def test_the_tracking_server_chart_is_pinned_by_digest(monkeypatch):
    ran = []

    def helm(command, **_kwargs):
        ran.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Error: stop here\n")

    monkeypatch.setattr(tracking_server.shutil, "which", lambda _name: "/bin/helm")
    monkeypatch.setattr(tracking_server.subprocess, "run", helm)
    with pytest.raises(tracking_server.InstallError):
        tracking_server.install(get_settings(), FakeKube().clients, lambda _msg: None)
    [command] = ran
    chart = command[command.index("--install") + 2]
    assert re.fullmatch(r"oci://ghcr\.io/mlflow/charts/mlflow@sha256:[0-9a-f]{64}", chart)
    assert command[command.index("--version") + 1] == "3.18.0"  # its digest must match the pin
