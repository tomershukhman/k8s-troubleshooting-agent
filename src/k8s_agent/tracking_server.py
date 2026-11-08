"""The MLflow tracking server in the cluster (SPEC §3.7): MLflow's official Helm chart with the
committed minimal values (deploy/mlflow-values.yaml), installed or upgraded by `agent setup`
through the `helm` CLI (Helm 4)."""

import shutil
import subprocess

from k8s_agent.config import Settings
from k8s_agent.kube import AdminClients, Progress


class InstallError(Exception):
    """The tracking server couldn't be installed; the message says why."""


def install(settings: Settings, c: AdminClients, progress: Progress) -> int | None:
    """Install or upgrade the release, returning once the server is ready. Returns its
    LoadBalancer port, if it has one (OrbStack exposes it on localhost)."""
    helm = shutil.which("helm")
    if helm is None:
        raise InstallError("helm not found: install Helm 4 (https://helm.sh/docs/intro/install/)")
    s = settings
    command = [
        helm, "upgrade", "--install", s.mlflow_release, s.mlflow_chart,
        "--version", s.mlflow_chart_version,
        "--namespace", s.mlflow_namespace, "--create-namespace",
        "--values", str(s.mlflow_values_file),
        # --wait: return only once the server passes its readiness check. A failed attempt is
        # undone (a first install removed), so the next one starts clean instead of failing at
        # once on the failed rollout.
        "--wait", "--rollback-on-failure",
        "--timeout", f"{s.mlflow_install_timeout_seconds}s",
        "--kube-context", c.context,
    ]  # fmt: skip
    if s.kubeconfig:
        command += ["--kubeconfig", str(s.kubeconfig)]
    progress(
        f"installing the MLflow tracking server (Helm release {s.mlflow_release} in namespace "
        f"{s.mlflow_namespace}); the first install pulls a ~1.4 GB image"
    )
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise InstallError(
            f"helm could not install the MLflow tracking server: {done.stderr.strip()}"
        )
    state = [ln for ln in done.stdout.splitlines() if ln.startswith(("STATUS:", "REVISION:"))]
    progress(f"MLflow tracking server ready ({', '.join(state)})")
    services = c.core.list_namespaced_service(
        s.mlflow_namespace, label_selector=f"app.kubernetes.io/instance={s.mlflow_release}"
    ).items
    lb = [svc for svc in services if svc.spec.type == "LoadBalancer"]
    return lb[0].spec.ports[0].port if lb else None
