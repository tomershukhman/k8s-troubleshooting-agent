"""`agent` command line. Interface only: core modules never import this."""

import asyncio
import logging
import math
import os
import sys
import termios
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import dataclass
from typing import Annotated, NoReturn

import typer
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text
from urllib3.exceptions import HTTPError

from k8s_agent import access, scenarios, tracing, tracking_server, ui
from k8s_agent.agent import (
    Answer,
    ApprovalRequest,
    McpUnavailable,
    NotStarted,
    RunStatus,
)
from k8s_agent.agent import investigate as run_investigation
from k8s_agent.agent.records import EvidenceStep
from k8s_agent.config import Settings, get_settings
from k8s_agent.kube import KubeError, admin_clients
from k8s_agent.logs import LOGGER, setup_logging
from k8s_agent.schemas import Diagnosis

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


async def _read_line(fd: int) -> str:
    """A line typed in the terminal, read without blocking the event loop, so the approval's
    own time limit can cancel the wait (a blocking input() in a thread can't be)."""
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()

    def ready() -> None:
        if not line.done():
            line.set_result(os.read(fd, 4096).decode(errors="replace"))

    loop.add_reader(fd, ready)
    try:
        return await line
    finally:
        loop.remove_reader(fd)


@dataclass
class Prompt:
    """The y/N prompt, on stderr, so `--json | jq` still asks: first the verified diagnosis
    (as the summary shows it), then the change, then the question. `shown`: it was asked."""

    err: Console
    progress: ui.Progress | None
    shown: bool = False

    async def __call__(
        self, request: ApprovalRequest, diagnosis: Diagnosis, evidence_steps: list[EvidenceStep]
    ) -> Answer:
        with self.progress.paused() if self.progress else nullcontext():
            # Drop anything typed while the agent worked: only an answer typed after seeing
            # the change counts.
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            self.err.print(ui.diagnosis(diagnosis, evidence_steps))
            self.err.print(ui.approval(request))
            self.shown = True
            self.err.print(Text("Apply this change? [y/N] ", "bold"), end="")
            try:
                reply = await _read_line(sys.stdin.fileno())
            except asyncio.CancelledError:  # the approval timed out
                self.err.print()
                raise
        yes = reply.strip().lower() in ("y", "yes")
        return Answer(yes, f"answered {'yes' if yes else 'no'} in the terminal")


def _approver(err: Console, progress: ui.Progress | None) -> Prompt | None:
    """The y/N prompt, when stdin is a terminal. None otherwise (piped, scripted, CI): the agent
    then rejects the fix, "no interactive approval available". There is no --yes."""
    return Prompt(err, progress) if sys.stdin.isatty() else None


def _one_terminal() -> bool:
    """stdout is the terminal the prompt used (stderr's): neither is redirected elsewhere."""
    return os.path.samestat(os.fstat(sys.stdout.fileno()), os.fstat(sys.stderr.fileno()))


@contextmanager
def _clean_errors(code: int = EXIT_FAILED) -> Iterator[None]:
    """A cluster or scenario problem is one error line and exit `code`, not a traceback."""
    try:
        yield
    except CLUSTER_ERRORS as e:
        _fail(str(e), code)


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
    stdout. For a broken Service selector (in AGENT_REMEDIATION_NAMESPACES) the agent can fix it:
    it shows you the diagnosis and the exact change and asks y/N; it runs only if you approve.
    Without a terminal on stdin the fix is rejected. Exit codes:

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
    models = {
        "investigator": s.model,
        "critic": s.effective_critic_model,
        "remediator": s.effective_remediator_model,
    }
    progress = None if quiet else ui.Progress(err, models, s.progress_summary_chars)
    prompt = _approver(err, progress)
    try:
        with progress or nullcontext():
            record = asyncio.run(
                run_investigation(
                    question,
                    settings=s,
                    namespace=namespace,
                    user_id=user_id,
                    on_event=progress or (lambda _: None),
                    approve=prompt,
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
    else:  # the diagnosis the prompt showed is still on screen above: not again
        shown = prompt is not None and prompt.shown and _one_terminal()
        Console().print(ui.summary(result, diagnosis_shown=shown))
    if result.status is RunStatus.failed:
        raise typer.Exit(EXIT_FAILED)


@app.command("eval")
def eval_(
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Only this one (default: every scenario)")
    ] = None,
    runs: Annotated[
        int | None,
        typer.Option(
            "--runs", min=1, help="Runs per scenario (default: AGENT_EVAL_RUNS_PER_SCENARIO)"
        ),
    ] = None,
) -> None:
    """Run the evals with mlflow.genai.evaluate and print the results.

    For each scenario: recreate it, ask its question --runs times (no namespace given), and
    score every run with the exact checks and the two judges. Scores land on each run's trace
    in MLflow; the results also go to AGENT_EVAL_RESULTS_DIR. Needs the tracking server.
    Exit codes:

    0: every scenario passed.
    1: a scenario missed its pass marks, or the evals stopped after one was scored.
    2: nothing ran (settings, tracking server, cluster, scenarios, MCP server, or the eval
    extra missing)."""
    s = _settings(EXIT_NOT_STARTED)
    if runs:  # the same share of runs must pass: 2 of 3 is 7 of 10, and 1 of 1
        least = math.ceil(runs * s.eval_pass_min_runs / s.eval_runs_per_scenario)
        s = s.model_copy(update={"eval_runs_per_scenario": runs, "eval_pass_min_runs": least})
    setup_logging(s.log_level, s.log_file, enabled=bool(s.log_file) or not sys.stderr.isatty())
    tracing.mlflow_env(s)  # before evals imports mlflow, which reads some of it on import
    try:
        from k8s_agent import evals  # the full mlflow package: the `eval` extra
    except ImportError as e:
        _fail(f"agent eval needs the eval extra (uv sync --extra eval): {e}", EXIT_NOT_STARTED)
    try:
        trace = tracing.enable(
            s.model_copy(update={"mlflow_experiment_name": s.eval_mlflow_experiment_name})
        )
    except tracing.TracingUnavailable as e:
        _fail(str(e), EXIT_NOT_STARTED)
    if trace is None:
        _fail("the evals store their results in MLflow: set MLFLOW_TRACKING_URI", EXIT_NOT_STARTED)
    evals.configure(s)
    with _clean_errors(EXIT_NOT_STARTED):
        found = scenarios.load_all(s.scenarios_dir)
        if scenario:
            found = {scenario: scenarios.get_scenario(s.scenarios_dir, scenario)}
        suite = evals.Suite(s, admin_clients(s), trace.run_url, _say)
    try:
        with redirect_stdout(sys.stderr):  # MLflow prints its own summaries; stdout: the table
            results = suite.run(found.values())
    except (NotStarted, *CLUSTER_ERRORS) as e:
        code = EXIT_FAILED if suite.scored else EXIT_NOT_STARTED  # until then, nothing ran
        if isinstance(e, McpUnavailable):
            _fail(f"{e}. Is it running? Start it with: docker compose up -d k8s-mcp", code)
        _fail(str(e), code)
    finally:
        tracing.flush()
    out = Console()
    out.width = max(out.width, 140)  # the table, unwrapped, when piped (80 columns by default)
    out.print(ui.eval_table(results))
    _say(f"results: {suite.path}")
    if not all(r.passed for r in results):
        raise typer.Exit(EXIT_FAILED)


@app.command()
def setup() -> None:
    """Create the agent's identities and install the tracking server (safe to re-run).

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
