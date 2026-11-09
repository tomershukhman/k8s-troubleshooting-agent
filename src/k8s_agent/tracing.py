"""MLflow tracing of agent runs (SPEC §3.7). Off unless MLFLOW_TRACKING_URI is set; mlflow is
imported only when it's on.

- `enable()`: one check that the tracking server answers (it also creates the experiment), then
  `mlflow.langchain.autolog()` traces every graph stage, model call and tool call. The caller
  decides what an unreachable server means: `investigate` warns and runs untraced.
- `run()`: one trace per run, under a root span with the request and the result. The user and
  session are MLflow's own trace fields; the run ID and model settings are trace metadata
  (MLflow adds the git commit itself, via GitPython).
- `flush()`: sends what's left before exit, bounded by the HTTP timeout and retries.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from k8s_agent.config import Settings


class TracingUnavailable(Exception):
    """Tracing is configured, but the tracking server can't be used."""


@dataclass(frozen=True)
class Tracing:
    uri: str
    experiment_id: str

    def url(self, trace_id: str) -> str:
        """The trace in the MLflow UI."""
        base = f"{self.uri.rstrip('/')}/#/experiments/{self.experiment_id}/traces"
        return f"{base}?selectedEvaluationId={trace_id}"

    def run_url(self, run_id: str) -> str:
        """An evaluation run in the MLflow UI: its scores, and each scored trace."""
        base = f"{self.uri.rstrip('/')}/#/experiments/{self.experiment_id}/evaluation-runs"
        return f"{base}?selectedRunUuid={run_id}"


_on: Tracing | None = None  # mirrors MLflow's own process-wide state (autolog, tracking URI)


def enable(settings: Settings) -> Tracing | None:
    """Turn tracing on if MLFLOW_TRACKING_URI is set; None if it isn't. Raises
    TracingUnavailable if the tracking server doesn't answer."""
    global _on
    _on = None
    uri = settings.mlflow_tracking_uri
    if not uri:
        return None
    # MLflow reads these from the environment; set before the import so `.env` works too.
    os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] = str(settings.mlflow_http_request_timeout)
    os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] = str(settings.mlflow_http_request_max_retries)
    os.environ["MLFLOW_DISABLE_TELEMETRY"] = str(settings.mlflow_disable_telemetry).lower()
    # The full mlflow package (installed for the evals) logs a hint for coding agents on import.
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "true")
    import mlflow

    mlflow.set_tracking_uri(uri)
    try:
        experiment = mlflow.set_experiment(settings.mlflow_experiment_name)
    except Exception as e:
        reason = (str(e).strip().splitlines() or [type(e).__name__])[0]
        raise TracingUnavailable(f"cannot use the MLflow tracking server at {uri}: {reason}") from e
    mlflow.langchain.autolog()
    _on = Tracing(uri, experiment.experiment_id)
    return _on


@contextmanager
def run(
    name: str, inputs: dict[str, Any], *, user: str, session: str, metadata: dict[str, str]
) -> Iterator[Any]:
    """The run's root span (an MLflow `LiveSpan`), or None when tracing is off. Everything
    traced inside it lands in its trace."""
    if _on is None:
        yield None
        return
    import mlflow
    from mlflow.entities import NoOpSpan, SpanStatus, SpanStatusCode, SpanType

    with (
        mlflow.tracing.context(session_id=session, user=user, metadata=metadata),
        mlflow.start_span(name, span_type=SpanType.AGENT) as span,
    ):
        if isinstance(span, NoOpSpan):  # MLflow couldn't start the trace (it logs why)
            yield None
            return
        span.set_inputs(inputs)
        try:
            yield span
        except BaseException as e:
            if not isinstance(e, Exception):  # MLflow records exceptions, not a Ctrl-C or cancel
                span.set_status(
                    SpanStatus(SpanStatusCode.ERROR, f"interrupted: {type(e).__name__}")
                )
            raise


@contextmanager
def span(name: str, inputs: Any, span_type: str) -> Iterator[Any]:
    """A span of our own inside the current trace (an MLflow `LiveSpan`), or None when tracing
    is off. `span_type`: an MLflow span type, e.g. "GUARDRAIL"."""
    if _on is None:
        yield None
        return
    import mlflow

    with mlflow.start_span(name, span_type=span_type) as live:
        live.set_inputs(inputs)
        yield live


def finish(span: Any, outputs: Any, error: str | None) -> None:
    """Record the run's result on its root span; a failed run marks the trace as an error."""
    if span is None:
        return
    from mlflow.entities import SpanStatus, SpanStatusCode

    span.set_outputs(outputs)
    if error:
        span.set_status(SpanStatus(SpanStatusCode.ERROR, error))


def flush() -> None:
    """Send the spans still queued. Each call to the server is bounded by
    MLFLOW_HTTP_REQUEST_TIMEOUT and MLFLOW_HTTP_REQUEST_MAX_RETRIES, so a stopped server can't
    hold up exit (MLflow's defaults retry for minutes)."""
    if _on is not None:
        import mlflow

        mlflow.flush_trace_async_logging(terminate=True)
