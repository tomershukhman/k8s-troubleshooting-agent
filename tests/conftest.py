"""Fixtures for every test, unit and acceptance."""

import pytest

from k8s_agent.config import get_settings


@pytest.fixture
def env():
    """Set AGENT_* settings for one test: env(max_tool_calls="2"). At its end they are undone
    and then the settings cache is cleared, so no later test reads them from the cache."""
    with pytest.MonkeyPatch.context() as patch:

        def set_env(**values: str) -> None:
            for key, value in values.items():
                patch.setenv(f"AGENT_{key.upper()}", value)
            get_settings.cache_clear()

        yield set_env
    get_settings.cache_clear()
