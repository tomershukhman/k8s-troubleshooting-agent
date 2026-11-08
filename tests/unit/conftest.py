"""Every unit test runs isolated from the developer's machine: no AGENT_* variables or
KUBECONFIG from the shell, no `.env` (the working directory is an empty temp folder), and the
scenarios folder by absolute path."""

import os
from pathlib import Path

import pytest

from k8s_agent.config import get_settings

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("AGENT_") or key == "KUBECONFIG":
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_SCENARIOS_DIR", str(REPO / "scenarios"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
