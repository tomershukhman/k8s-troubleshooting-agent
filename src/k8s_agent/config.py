"""All settings, read from env vars / `.env`. Defaults live here and nowhere else."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, PositiveFloat, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from k8s_agent.policy import FORBIDDEN_NAMESPACES

# Shortest lifetime the TokenRequest API accepts (expirationSeconds >= 600).
MIN_TOKEN_TTL_HOURS = 600 / 3600

NamespaceList = Annotated[list[Annotated[str, Field(min_length=1)]], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # Admin access, used only by `agent scenario` and `agent setup`.
    # Unset = the standard kubeconfig lookup ($KUBECONFIG, then ~/.kube/config) and its
    # current context.
    kubeconfig: Path | None = None
    kube_context: str | None = None

    # Scenarios
    scenarios_dir: Path = Path("scenarios")
    scenario_ready_timeout_seconds: PositiveFloat = 180
    scenario_delete_timeout_seconds: PositiveFloat = 120
    scenario_poll_seconds: PositiveFloat = 2

    # Agent identities (SPEC §3.5). Comma-separated in env vars.
    namespaces: NamespaceList = ["shop-orders", "shop-checkout", "shop-catalog", "shop-inventory"]
    remediation_namespaces: NamespaceList = ["shop-checkout"]
    token_ttl_hours: float = Field(12, ge=MIN_TOKEN_TTL_HOURS)
    reader_kubeconfig: Path = Path(".kube-agent/reader.kubeconfig")
    remediator_kubeconfig: Path = Path(".kube-agent/remediator.kubeconfig")

    # MCP server. Host networking (SPEC §3.10): the same URL works from the host and from
    # containers. Unset AGENT_MCP_URL = http://localhost:<AGENT_MCP_PORT>/mcp.
    mcp_port: int = Field(8080, ge=1, le=65535)
    mcp_url: str = ""

    # Acceptance tests: how long an assertion may keep retrying after `up` returns, for
    # state that flips between polls (e.g. CrashLoopBackOff). Also the per-call MCP timeout.
    accept_assert_timeout_seconds: PositiveFloat = 30

    @field_validator("namespaces", "remediation_namespaces", mode="before")
    @classmethod
    def _comma_separated(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("namespaces", "remediation_namespaces")
    @classmethod
    def _never_system(cls, v: list[str]) -> list[str]:
        if bad := sorted(set(v) & FORBIDDEN_NAMESPACES):
            raise ValueError(f"the agent may never be granted {', '.join(bad)}")
        return v

    @field_validator("mcp_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def mcp_endpoint(self) -> str:
        return self.mcp_url or f"http://localhost:{self.mcp_port}/mcp"


@lru_cache
def get_settings() -> Settings:
    return Settings()
