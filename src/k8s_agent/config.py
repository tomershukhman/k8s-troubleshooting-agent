"""All settings, read from env vars / `.env`. Defaults live here and nowhere else."""

import getpass
import math
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Shortest lifetime the TokenRequest API accepts (expirationSeconds >= 600).
MIN_TOKEN_TTL_HOURS = 600 / 3600

# Comma-separated in env vars.
NameList = Annotated[list[Annotated[str, Field(min_length=1)]], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    # Admin access, used only by `agent scenario`, `agent setup` and `agent eval` (it rebuilds
    # the scenarios). Unset = the standard kubeconfig lookup ($KUBECONFIG, then ~/.kube/config)
    # and its current context.
    kubeconfig: Path | None = None
    kube_context: str | None = None
    kube_timeout_seconds: PositiveFloat = 30  # per Kubernetes API call with the admin kubeconfig

    # Scenarios
    scenarios_dir: Path = Path("scenarios")
    scenario_ready_timeout_seconds: PositiveFloat = 180
    scenario_delete_timeout_seconds: PositiveFloat = 120
    scenario_poll_seconds: PositiveFloat = 2

    # Agent identities (SPEC §3.5)
    rbac_file: Path = Path("deploy/rbac.yaml")
    token_ttl_hours: float = Field(default=12, ge=MIN_TOKEN_TTL_HOURS)
    reader_kubeconfig: Path = Path(".kube-agent/reader.kubeconfig")
    remediator_kubeconfig: Path = Path(".kube-agent/remediator.kubeconfig")
    # Where the agent may propose a fix (the remediator's RoleBinding grants the same).
    remediation_namespaces: NameList = ["shop-checkout"]

    # The fix (SPEC 3.4). The wait for a person's answer has its own clock: it isn't agent
    # working time (AGENT_RUN_MAX_SECONDS), and when it runs out the fix is rejected.
    approval_timeout_seconds: PositiveFloat = 600
    # The Service's ready endpoints after a fix: the longest wait, and how often to check.
    fix_endpoints_wait_seconds: PositiveFloat = 20
    fix_endpoints_poll_seconds: PositiveFloat = 1
    remediator_timeout_seconds: PositiveFloat = 30  # per Kubernetes API call as the remediator

    # MCP server. Host networking (SPEC §3.10): the same URL works from the host and from
    # containers. Unset AGENT_MCP_URL = http://localhost:<AGENT_MCP_PORT>/mcp.
    mcp_port: int = Field(default=8080, ge=1, le=65535)
    mcp_url: str = ""
    mcp_timeout_seconds: PositiveFloat = 30  # per MCP call

    # The agents (SPEC §2.1 stopping rule, §4.6). The API key is read from ANTHROPIC_API_KEY
    # (no AGENT_ prefix); unset = the provider's own lookup.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    user_id: str | None = None  # who is talking; unset = the OS user
    model: str = "anthropic:claude-sonnet-5-5"  # the investigator; LangChain model string
    critic_model: str | None = None  # the evidence critic; unset = AGENT_MODEL
    remediator_model: str | None = None  # the remediator (the one fix); unset = AGENT_MODEL
    # high: at medium, Sonnet 5.5 often skips thinking, so traces get no reasoning summaries.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Per investigate round: tool calls beyond the limit are refused and the model concludes;
    # at the model-call limit the run stops.
    max_model_calls: PositiveInt = 20
    max_tool_calls: PositiveInt = 15
    # Per run, across verify rounds (the investigator's calls; the critic adds one model call
    # per verify round). As per round, tool calls beyond the budget are refused and the model
    # concludes; a spent model-call or time budget ends the run inconclusive, keeping what was
    # gathered.
    run_max_model_calls: PositiveInt = 40
    run_max_tool_calls: PositiveInt = 30
    run_max_seconds: PositiveFloat = 300
    max_verify_rounds: PositiveInt = 2  # critic verdicts per run; then unfounded = inconclusive
    recursion_limit: PositiveInt = 250  # LangGraph steps per run: a backstop the limits beat
    max_output_tokens: PositiveInt = 8000  # per model call, thinking included
    model_max_retries: int = Field(default=2, ge=0)  # provider SDK retries (429, 5xx, connection)
    tool_max_retries: int = Field(default=2, ge=0)  # failed MCP calls, except refused or not found
    tool_retry_delay_seconds: float = Field(default=1.0, ge=0)  # first wait; doubles each retry
    tool_output_max_chars: PositiveInt = 40000  # safety cap per tool result
    tool_output_strip_fields: NameList = ["managedFields"]  # noise removed (never spec/status)
    pod_log_tail_lines: PositiveInt = 100  # max lines pods_log returns
    evidence_excerpt_chars: PositiveInt = 1500  # output shown per cited step in the result
    progress_summary_chars: PositiveInt = 200  # reasoning summary length in progress lines
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"  # structured JSON logs
    # Where the JSON logs go. Unset: stderr, except in an interactive terminal (the progress
    # display shows the same events there).
    log_file: Path | None = None

    # MLflow tracing (SPEC §3.7), under MLflow's own variable names. No tracking URI = no
    # tracing. MLflow's own HTTP defaults retry for minutes, so a stopped server would hold up
    # exit: these fail fast instead (tracing never decides how a run ends).
    mlflow_tracking_uri: str | None = Field(default=None, validation_alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_name: str = Field(
        default="k8s-agent", validation_alias="MLFLOW_EXPERIMENT_NAME"
    )
    mlflow_http_request_timeout: PositiveInt = Field(
        default=3, validation_alias="MLFLOW_HTTP_REQUEST_TIMEOUT"
    )  # seconds per call to the tracking server (it runs on this machine's cluster)
    mlflow_http_request_max_retries: int = Field(
        default=0, ge=0, validation_alias="MLFLOW_HTTP_REQUEST_MAX_RETRIES"
    )
    # MLflow's usage telemetry to its own servers (on by default in MLflow); traces never go.
    mlflow_disable_telemetry: bool = Field(
        default=True, validation_alias="MLFLOW_DISABLE_TELEMETRY"
    )
    # The code's git commit, recorded on traces and eval runs (it pins the prompts). Unset = the
    # working directory's .git; the image has none, so its build sets this.
    git_commit: str | None = None

    # Evals (SPEC 2.3, 3.6): each scenario's question, asked this many times. One run checks
    # nothing broke; more runs measure how reliable the agent is. The diagnosis and the two
    # judges must pass in at least this share of the runs (1 of 1, 2 of 3, 7 of 10); the exact
    # checks (evidence exists, the right tools, safe actions) in every run.
    eval_runs_per_scenario: PositiveInt = 1
    eval_pass_share: float = Field(default=2 / 3, gt=0, le=1)
    # The two judges, as an MLflow model URI: stronger than the agent, so it doesn't grade itself.
    judge_model: str = "anthropic:/claude-opus-5-5"
    # Tries per judge verdict: a judge call that fails or gives no readable verdict is asked
    # again. Still none: that run fails the check.
    judge_attempts: PositiveInt = 3
    eval_mlflow_experiment_name: str = (
        "k8s-agent-evals"  # so MLFLOW_EXPERIMENT_NAME shows real runs
    )
    eval_results_dir: Path = Path("eval-results")  # one JSON file per `agent eval`

    # Acceptance tests: how long an assertion may keep retrying for state that flips between
    # polls (e.g. CrashLoopBackOff).
    accept_assert_timeout_seconds: PositiveFloat = 30
    # M2+ acceptance setup keeps scenarios already up (ours, manifests re-applied cleanly, fault
    # live) and leaves them up afterwards; a test that changes one (M6's, on the selector
    # Service) rebuilds it. False = fresh, removed after.
    accept_reuse_scenarios: bool = True
    # Test runs trace here, so the experiment users open (MLFLOW_EXPERIMENT_NAME) shows real runs.
    accept_mlflow_experiment_name: str = "k8s-agent-tests"

    @field_validator("remediation_namespaces", "tool_output_strip_fields", mode="before")
    @classmethod
    def _comma_separated(cls, v: Any) -> Any:
        return [part.strip() for part in v.split(",") if part.strip()] if isinstance(v, str) else v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _per_round_within_run(self) -> Settings:
        if self.max_model_calls > self.run_max_model_calls:
            raise ValueError("AGENT_MAX_MODEL_CALLS can't exceed AGENT_RUN_MAX_MODEL_CALLS")
        if self.max_tool_calls > self.run_max_tool_calls:
            raise ValueError("AGENT_MAX_TOOL_CALLS can't exceed AGENT_RUN_MAX_TOOL_CALLS")
        return self

    @property
    def eval_pass_min_runs(self) -> int:
        """Runs in which a "most runs" check must pass: the share of the runs, rounded up."""
        return math.ceil(self.eval_runs_per_scenario * self.eval_pass_share - 1e-9)

    @property
    def mcp_endpoint(self) -> str:
        return self.mcp_url.strip() or f"http://localhost:{self.mcp_port}/mcp"

    @property
    def effective_critic_model(self) -> str:
        return self.critic_model or self.model

    @property
    def effective_remediator_model(self) -> str:
        return self.remediator_model or self.model

    @property
    def effective_user_id(self) -> str:
        return self.user_id or getpass.getuser()

    @property
    def model_settings(self) -> dict[str, str]:
        """What a trace records about the models (with the git commit, it pins the prompts)."""
        return {
            "model": self.model,
            "critic_model": self.effective_critic_model,
            "remediator_model": self.effective_remediator_model,
            "effort": self.effort,
            "max_output_tokens": str(self.max_output_tokens),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
