"""`agent` command line. Interface only: core modules never import this."""

import asyncio
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Annotated, NoReturn

import typer
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text
from urllib3.exceptions import HTTPError

from k8s_agent import access, scenarios, tracing, tracking_server, ui
from k8s_agent.agent import McpUnavailable, NotStarted, RunStatus
from k8s_agent.agent import investigate as run_investigation
from k8s_agent.config import Settings, get_settings
from k8s_agent.kube import KubeError, admin_clients
from k8s_agent.logs import LOGGER, setup_logging

app = typer.Typer(no_args_is_help=True, help="Kubernetes troubleshooting agent.")
scenario_app = typer.Typer(no_args_is_help=True, help="Create or remove the broken scenarios.")
app.add_typer(scenario_app, name="scenario")

NameArg = Annotated[str, typer.Argument(help="Scenario name, e.g. selector")]
EXIT_FAILED, EXIT_NOT_STARTED = 1, 2
CLUSTER_ERRORS = (
    scenarios.ScenarioError,
    tracking_server.InstallError,
    KubeError,
    TimeoutError,
    ApiException,
    HTTPError,
)


def _say(msg: str) -> None:
    typer.echo(msg, err=True)


def _fail(msg: str, code: int = 1) -> NoReturn:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(code)


def _settings(error_code: int = 1) -> Settings:
    try:
        return get_settings()
    except ValidationError as e:
        _fail(f"invalid settings:\n{e}", error_code)


def _tracing(s: Settings) -> tracing.Tracing | None:
    """Tracing, if configured. Best-effort for `investigate`: a tracking server that doesn't
    answer at start is one warning, and the run goes on untraced."""
    try:
        return tracing.enable(s)
    except tracing.TracingUnavailable as e:
        logging.getLogger(LOGGER).warning("tracing_unavailable", extra={"error": str(e)})
        _say(f"warning: {e}; investigating without tracing")
        return None


@contextmanager
def _clean_errors() -> Iterator[None]:
    """A cluster or scenario problem is one error line and exit 1, not a traceback."""
    try:
        yield
    except CLUSTER_ERRORS as e:
        _fail(str(e))


@app.command()
def investigate(
    question: Annotated[str, typer.Argument(help="What's wrong, in plain words")],
    namespace: Annotated[
        str | None,
        typer.Option("-n", "--namespace", help="Where to look (default: the agent finds it)"),
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="Who is asking (default: AGENT_USER_ID or OS user)")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON (always, when piped)")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="No progress on stderr")] = False,
) -> None:
    """Investigate a problem with read-only tools and report the diagnosis.

    In a terminal: live progress, then a summary. Piped or with --json: the result JSON on
    stdout. Exit codes:

    0: a diagnosis, "inconclusive" included.
    1: the run failed; the result says why.
    2: nothing ran (invalid settings, or the MCP server can't be reached)."""
    s = _settings(EXIT_NOT_STARTED)
    err = Console(stderr=True)
    # JSON logs are for machines: kept off an interactive terminal unless AGENT_LOG_FILE is set.
    setup_logging(s.log_level, s.log_file, enabled=bool(s.log_file) or not err.is_terminal)
    trace = _tracing(s)
    user_id = user or s.effective_user_id
    if not quiet:
        err.print(
            Text.assemble(("Investigating: ", "bold"), question, (f"  (as {user_id})", "dim"))
        )
    models = {"investigator": s.model, "critic": s.effective_critic_model}
    progress = (
        nullcontext(lambda _: None) if quiet else ui.Progress(err, models, s.progress_summary_chars)
    )
    try:
        with progress as on_event:
            record = asyncio.run(
                run_investigation(
                    question, settings=s, namespace=namespace, user_id=user_id, on_event=on_event
                )
            )
    except McpUnavailable as e:
        _fail(f"{e}. Is it running? Start it with: docker compose up -d k8s-mcp", EXIT_NOT_STARTED)
    except NotStarted as e:
        _fail(str(e), EXIT_NOT_STARTED)
    finally:
        tracing.flush()  # before the link, so the trace it opens is complete
    result = record.result
    if trace and result.trace_id:
        url = trace.url(result.trace_id)
        err.print(Text.assemble(("Trace: ", "bold"), (url, f"link {url}")), soft_wrap=True)
    if as_json or not sys.stdout.isatty():
        typer.echo(result.model_dump_json(indent=2 if sys.stdout.isatty() else None))
    else:
        Console().print(ui.summary(result))
    if result.status is RunStatus.failed:
        raise typer.Exit(EXIT_FAILED)


@app.command()
def setup() -> None:
    """Create the agent's identities and kubeconfigs, and install the tracking server. Safe to
    re-run.

    Applies deploy/rbac.yaml (the read-only `reader` and the fix-only `remediator` in
    namespace k8s-agent) and writes a short-lived kubeconfig for each; re-run to refresh
    expired tokens. Then installs or upgrades the MLflow tracking server with Helm
    (deploy/mlflow-values.yaml), returning once it's ready."""
    s = _settings()
    with _clean_errors():
        c = admin_clients(s)
        _say(f"context {c.context}: setting up the agent identities")
        paths = {access.READER: s.reader_kubeconfig, access.REMEDIATOR: s.remediator_kubeconfig}
        creds = access.setup(
            c,
            rbac_file=s.rbac_file,
            token_ttl_seconds=round(s.token_ttl_hours * 3600),
            kubeconfigs=paths,
            progress=_say,
        )
        port = tracking_server.install(s, c, _say)
    expires = min(cred.expires for cred in creds)
    _say(f"setup done; tokens expire {expires.isoformat()} (re-run `agent setup` to refresh)")
    if port:
        _say(
            f"traces: set MLFLOW_TRACKING_URI to the server's LoadBalancer address, port {port} "
            f"(OrbStack: http://localhost:{port})"
        )


@scenario_app.command("list")
def scenario_list() -> None:
    """List scenarios (read from disk; no cluster access)."""
    with _clean_errors():
        found = scenarios.load_all(_settings().scenarios_dir)
    for scn in found.values():
        typer.echo(f"{scn.name:<12} {scn.namespace:<16} {scn.spec.question}")


@scenario_app.command("up")
def scenario_up(name: NameArg) -> None:
    """Recreate the scenario's namespace, the agent's access and the app, and wait until the
    fault is live."""
    s = _settings()
    with _clean_errors():
        scn = scenarios.get_scenario(s.scenarios_dir, name)
        c = admin_clients(s)
        _say(f"context {c.context}: bringing up {scn.name}")
        scenarios.up(
            scn,
            c,
            ready_timeout=s.scenario_ready_timeout_seconds,
            delete_timeout=s.scenario_delete_timeout_seconds,
            poll=s.scenario_poll_seconds,
            progress=_say,
        )
    _say(f"{scn.name} is up in namespace {scn.namespace}")


@scenario_app.command("down")
def scenario_down(name: NameArg) -> None:
    """Delete the scenario namespace (and with it the agent's access) and wait until it's gone."""
    s = _settings()
    with _clean_errors():
        scn = scenarios.get_scenario(s.scenarios_dir, name)
        existed = scenarios.down(
            scn,
            admin_clients(s),
            timeout=s.scenario_delete_timeout_seconds,
            poll=s.scenario_poll_seconds,
            progress=_say,
        )
    _say(f"namespace {scn.namespace} {'deleted' if existed else 'already absent'}")
