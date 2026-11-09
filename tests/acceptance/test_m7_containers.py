"""M7: what CI's image build and the eval runs through the containers don't show (PLAN M7).
Needs Docker; no cluster."""

import json
import re
import subprocess

from tests.acceptance.conftest import compose

IMAGE = "ghcr.io/tomershukhman/k8s-troubleshooting-agent:latest"
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


def test_the_agent_container_gets_only_the_remediator_kubeconfig():
    services = json.loads(compose("config", "--format", "json"))["services"]
    admin = {v["target"]: v["source"] for v in services["admin"]["volumes"]}
    agent = services["agent"]["volumes"]
    assert [v["target"] for v in agent] == ["/app/.kube-agent/remediator.kubeconfig"]
    assert agent[0]["source"].endswith("remediator.kubeconfig")
    assert agent[0]["source"] != admin["/kube/config"]
    assert agent[0]["read_only"]


def test_the_image_holds_no_secrets():
    compose("build", "admin")
    config = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE]))[0]
    names = [e.split("=", 1)[0] for e in config["Config"]["Env"]]
    assert not [n for n in names if SECRET_NAME.search(n)], names
    # No settings, credentials or repository files anywhere in the image (the venv aside); as
    # root, to see every folder.
    found = subprocess.check_output(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "find", IMAGE, "/", "-xdev",
         "-path", "/app/.venv", "-prune", "-o", "-path", "/proc", "-prune", "-o",
         "(", "-name", ".env", "-o", "-name", "*.kubeconfig", "-o", "-name", ".kube-agent",
         "-o", "-name", ".kube", "-o", "-name", ".git", ")", "-print"],
        text=True,
    )  # fmt: skip
    assert found.split() == []
