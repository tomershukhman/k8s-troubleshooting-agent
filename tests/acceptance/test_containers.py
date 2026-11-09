"""What CI's image build and the eval runs through the containers don't show. Needs Docker and
the kubeconfigs `agent setup` writes; no cluster."""

import json
import re
import subprocess

import pytest

from tests.acceptance.conftest import compose

IMAGE = "ghcr.io/tomershukhman/k8s-troubleshooting-agent:latest"
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


@pytest.fixture(scope="module", autouse=True)
def image() -> None:
    """The image from this checkout's code."""
    compose("build", "admin")


def in_agent(*command: str) -> subprocess.CompletedProcess:
    """`command` in the agent container, as compose runs it (no MCP server needed)."""
    return subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            *command[:1],
            "agent",
            *command[1:],
        ],
        capture_output=True,
        text=True,
    )


def test_the_agent_container_holds_only_the_remediator_kubeconfig_read_only():
    assert in_agent("ls", "-A", "/app/.kube-agent").stdout.split() == ["remediator.kubeconfig"]
    assert in_agent("ls", "/kube").returncode != 0  # admin's mount of your kubeconfig: not here
    write = in_agent("touch", "/app/.kube-agent/remediator.kubeconfig")
    assert write.returncode != 0 and "Read-only file system" in write.stderr, write.stderr


def test_the_image_holds_no_secrets():
    config = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE]))[0]
    names = [e.split("=", 1)[0] for e in config["Config"]["Env"]]
    assert not [n for n in names if SECRET_NAME.search(n)], names
    # No settings, credentials or repository files anywhere in the image (the venv aside); as
    # root, to see every folder.
    found = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "find",
            IMAGE,
            "/",
            "-xdev",
            "-path",
            "/app/.venv",
            "-prune",
            "-o",
            "-path",
            "/proc",
            "-prune",
            "-o",
            "(",
            "-name",
            ".env",
            "-o",
            "-name",
            "*.kubeconfig",
            "-o",
            "-name",
            ".kube-agent",
            "-o",
            "-name",
            ".kube",
            "-o",
            "-name",
            ".git",
            ")",
            "-print",
        ],
        text=True,
    )
    assert found.split() == []
