"""CLI behaviour that needs no cluster. Settings are isolated by conftest.py."""

from typer.testing import CliRunner

from k8s_agent.cli import app


def run(*args: str):
    return CliRunner().invoke(app, list(args))


def test_list_prints_every_scenario_with_its_neutral_namespace():
    result = run("scenario", "list")
    assert result.exit_code == 0
    rows = [line.split()[:2] for line in result.stdout.splitlines()]
    assert rows == [
        ["crashloop", "shop-orders"],
        ["healthy", "shop-inventory"],
        ["readiness", "shop-catalog"],
        ["selector", "shop-checkout"],
    ]


def test_unknown_scenario_is_a_clean_error():
    result = run("scenario", "up", "nope")
    assert result.exit_code == 1
    assert "error: unknown scenario 'nope'" in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_bad_kubeconfig_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KUBECONFIG", str(tmp_path / "missing"))
    for command in (["scenario", "down", "selector"], ["setup"]):
        result = run(*command)
        assert result.exit_code == 1, command
        assert result.stderr.startswith("error: cannot load kubeconfig"), command


def test_invalid_settings_are_a_clean_error(monkeypatch):
    monkeypatch.setenv("AGENT_NAMESPACES", "shop-checkout,kube-system")
    result = run("setup")
    assert result.exit_code == 1
    assert result.stderr.startswith("error: invalid settings")
    assert "may never be granted kube-system" in result.stderr
