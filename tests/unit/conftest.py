"""Every unit test runs isolated from the developer's machine: no AGENT_* variables,
KUBECONFIG, model API key or tracing settings (MLFLOW_*) from the shell, no `.env` (the working
directory is an empty temp folder), a kubeconfig that doesn't exist (so nothing can reach a
cluster), and the scenarios folder and RBAC file by absolute path. Tracing and logging state a
test turns on is undone after it."""

import logging
import os
from pathlib import Path

import pytest

from k8s_agent import tracing
from k8s_agent.config import get_settings
from k8s_agent.logs import LOGGER

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(
            ("AGENT_", "MLFLOW_", "LANGSMITH_", "LANGCHAIN_", "LANGFUSE_")
        ) or key in (
            "KUBECONFIG",
            "ANTHROPIC_API_KEY",
        ):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_KUBECONFIG", str(tmp_path / "no-kubeconfig"))  # never a cluster
    monkeypatch.setenv("AGENT_SCENARIOS_DIR", str(REPO / "scenarios"))
    monkeypatch.setenv("AGENT_RBAC_FILE", str(REPO / "deploy" / "rbac.yaml"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    logger = logging.getLogger(LOGGER)  # the CLI's setup_logging() points it at a stream
    logger.handlers.clear()  # the test runner then closes
    logger.propagate = True
    if tracing._on is not None:  # tracing.enable() turned on MLflow's LangChain autolog
        import mlflow

        mlflow.langchain.autolog(disable=True)
    tracing._on = None  # tracing.enable() sets process-wide state: MLflow's, and these
    for key in [k for k in os.environ if k.startswith("MLFLOW_")]:
        del os.environ[key]  # then monkeypatch restores the shell's own


@pytest.fixture
def env(monkeypatch):
    """Set AGENT_* settings for one test: env(max_tool_calls="2")."""

    def set_env(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(f"AGENT_{key.upper()}", value)
        get_settings.cache_clear()

    return set_env
