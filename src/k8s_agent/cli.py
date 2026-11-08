"""`agent` command line. Interface only: core modules never import this."""

from typing import Annotated, NoReturn

import typer
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from k8s_agent.config import get_settings
from k8s_agent.kube import KubeError, admin_clients
from k8s_agent.scenarios import cluster
from k8s_agent.scenarios.spec import ScenarioError, get_scenario, load_all

app = typer.Typer(no_args_is_help=True, help="Kubernetes troubleshooting agent.")
scenario_app = typer.Typer(no_args_is_help=True, help="Create or remove the broken scenarios.")
app.add_typer(scenario_app, name="scenario")

NameArg = Annotated[str, typer.Argument(help="Scenario name, e.g. selector")]


def _say(msg: str) -> None:
    typer.echo(msg, err=True)


def _fail(msg: str) -> NoReturn:
    typer.echo(f"error: {msg}", err=True)
    raise typer.Exit(1)


@scenario_app.command("list")
def scenario_list() -> None:
    """List scenarios (read from disk; no cluster access)."""
    try:
        scenarios = load_all(get_settings().scenarios_dir)
    except ScenarioError as e:
        _fail(str(e))
    for scn in scenarios.values():
        typer.echo(f"{scn.name:<12} {scn.spec.namespace:<16} {scn.spec.question}")


@scenario_app.command("up")
def scenario_up(name: NameArg) -> None:
    """Create the scenario namespace, apply its manifests, wait until the fault is live."""
    s = get_settings()
    try:
        scn = get_scenario(s.scenarios_dir, name)
        c = admin_clients(s)
        _say(f"context {c.context}: bringing up {scn.name}")
        cluster.up(
            scn,
            c,
            ready_timeout=s.scenario_ready_timeout_seconds,
            delete_timeout=s.scenario_delete_timeout_seconds,
            poll=s.scenario_poll_seconds,
            progress=_say,
        )
    except (ScenarioError, KubeError, TimeoutError) as e:
        _fail(str(e))
    except (ApiException, HTTPError) as e:
        _fail(f"Kubernetes API error: {e}")
    _say(f"{scn.name} is up in namespace {scn.spec.namespace}")


@scenario_app.command("down")
def scenario_down(name: NameArg) -> None:
    """Delete the scenario namespace and wait until it's gone."""
    s = get_settings()
    try:
        scn = get_scenario(s.scenarios_dir, name)
        c = admin_clients(s)
        existed = cluster.down(
            scn,
            c,
            timeout=s.scenario_delete_timeout_seconds,
            poll=s.scenario_poll_seconds,
            progress=_say,
        )
    except (ScenarioError, KubeError, TimeoutError) as e:
        _fail(str(e))
    except (ApiException, HTTPError) as e:
        _fail(f"Kubernetes API error: {e}")
    if not existed:
        _say(f"namespace {scn.spec.namespace} already absent")
