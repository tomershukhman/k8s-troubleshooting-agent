# Kubernetes Troubleshooting Agent

An AI agent that investigates a Kubernetes cluster, finds the root cause of an issue, and recommends the next action.

This repository currently contains the failure scenarios the agent is tested against, and a CLI to deploy and remove them. The design is in [SPEC.md](SPEC.md).

## Requirements

- A Kubernetes cluster your kubeconfig points at (kind, minikube, OrbStack, Docker Desktop and others all work)
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 if needed)

## Setup

```bash
uv sync
cp .env.example .env      # optional: override any setting
```

The CLI uses your own kubeconfig: `$KUBECONFIG` or `~/.kube/config`, with its current context. To use another context, set `AGENT_KUBE_CONTEXT`. Every setting and its default is listed in `.env.example`.

## Failure scenarios

Four scenarios, each in its own namespace. Each is a folder in `scenarios/` with plain Kubernetes YAML (public images pinned by digest) and a `scenario.yaml` holding the question to ask, the accepted answers, and the condition that proves the fault is live.

| Scenario | Namespace | What's broken | Question for the agent |
|---|---|---|---|
| `crashloop` | `scn-crashloop` | The ConfigMap key is misspelled (`DATABSE_URL`), so the app logs "DATABASE_URL not set", exits 1 and goes into CrashLoopBackOff. | The orders service keeps restarting. What's wrong? |
| `selector` | `scn-selector` | The Service selects `app: check-out` but the pods are labelled `app: checkout`, so the Service has no endpoints while the pods are healthy. | The checkout service isn't reachable from other pods. Why? |
| `readiness` | `scn-readiness` | The readiness probe checks `/healthz`, which nginx answers with 404, so the pods run but never become Ready. | Why is the catalog service not serving traffic? |
| `healthy` | `scn-healthy` | Nothing (the control case). The right answer is "no fault". | Is the inventory service healthy? |

```bash
uv run agent scenario list               # the four scenarios
uv run agent scenario up selector        # create it and wait until the fault is live
kubectl get pods,endpointslices -n scn-selector
uv run agent scenario down selector      # delete the namespace and wait until it's gone
```

`up` waits until the fault can be observed (for `crashloop`, until the pod has restarted twice, about 40 seconds). Running `up` again recreates the scenario from scratch. Both commands refuse to touch a `scn-*` namespace they didn't create.

## Development

```bash
make check     # lint + unit tests (no cluster needed)
make accept    # acceptance tests against your cluster (deploys and removes every scenario, about 2 minutes)
```

## Project layout

```
scenarios/<name>/        manifests/*.yaml + scenario.yaml
src/k8s_agent/           CLI and scenario tooling
tests/unit/              fast tests, no cluster
tests/acceptance/        tests against a real cluster
SPEC.md                  design and decisions
PLAN.md                  build plan
```
