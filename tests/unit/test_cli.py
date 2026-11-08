"""CLI error paths that need no cluster."""

from typer.testing import CliRunner

from k8s_agent.cli import app
from k8s_agent.config import get_settings


def test_list_prints_every_scenario():
    result = CliRunner().invoke(app, ["scenario", "list"])
    assert result.exit_code == 0
    assert [line.split()[0] for line in result.stdout.splitlines()] == [
        "crashloop",
        "healthy",
        "readiness",
        "selector",
    ]


def test_unknown_scenario_is_a_clean_error():
    result = CliRunner().invoke(app, ["scenario", "up", "nope"])
    assert result.exit_code == 1
    assert "error: unknown scenario 'nope'" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_bad_kubeconfig_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KUBECONFIG", str(tmp_path / "missing"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["scenario", "down", "selector"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert result.stderr.startswith("error: cannot load kubeconfig")
