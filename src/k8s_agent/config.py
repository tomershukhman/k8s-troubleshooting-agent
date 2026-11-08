"""All settings, read from env vars / `.env`. Defaults live here and nowhere else."""

from functools import lru_cache
from pathlib import Path

from pydantic import PositiveFloat
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # Admin access, used only by `agent scenario` (and `agent setup` from M2).
    # Unset = the standard kubeconfig lookup ($KUBECONFIG, then ~/.kube/config) and its
    # current context.
    kubeconfig: Path | None = None
    kube_context: str | None = None

    # Scenarios
    scenarios_dir: Path = Path("scenarios")
    scenario_ready_timeout_seconds: PositiveFloat = 180
    scenario_delete_timeout_seconds: PositiveFloat = 120
    scenario_poll_seconds: PositiveFloat = 2

    # Acceptance tests: how long an assertion may keep retrying after `up` returns, for
    # state that flips between polls (e.g. CrashLoopBackOff).
    accept_assert_timeout_seconds: PositiveFloat = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
