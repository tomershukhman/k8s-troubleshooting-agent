# Kubernetes Troubleshooting Agent

An AI agent that investigates a Kubernetes cluster, finds the root cause of an issue, and recommends the next action.

**Guiding principle: the cluster is the system under investigation; the agent and its tools run beside it.** Only the test scenarios and the agent's identities live in the cluster.

This repository currently contains the failure scenarios the agent is tested against, a CLI to deploy and remove them, and the agent's safe access to the cluster: two narrowly scoped identities and a read-only MCP server running beside the cluster. The design is in [SPEC.md](SPEC.md).

## Requirements

- A local Kubernetes cluster your kubeconfig points at. The examples use [kind](https://kind.sigs.k8s.io/). So far it has been tested on OrbStack only; kind, minikube and Docker Desktop are untested.
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 if needed).
- Docker with Compose and host networking. Host networking is built in on Linux and OrbStack. On Docker Desktop (4.34+) it's one setting: Settings → Resources → Network → Enable host networking.
- On native Linux Docker only: set `AGENT_CONTAINER_USER` in `.env` to your `id -u`:`id -g`, so the MCP container can read the credential file `agent setup` writes (mode 0600).

## Setup

```bash
kind create cluster --name k8s-agent    # or use any local cluster you already have
uv sync
cp .env.example .env                    # optional: override any setting
uv run agent setup                      # the agent's identities (once; re-run to refresh tokens)
docker compose up -d --wait k8s-mcp     # the read-only MCP server
```

The CLI uses your own kubeconfig: `$KUBECONFIG` or `~/.kube/config`, with its current context (`kind create cluster` switches to `kind-k8s-agent`). To use another context, set `AGENT_KUBE_CONTEXT`. Every setting and its default is listed in `.env.example`.

## How the agent reaches the cluster

The agent never uses your credentials. It sees the cluster only through an MCP server that runs as a read-only identity, and the one fix it may ever apply uses a second identity that can only fix and verify a Service in one namespace.

### Identities (`agent setup`)

`agent setup` uses your admin kubeconfig to create, in namespace `k8s-agent`:

| Identity | Can | Can't | Where |
|---|---|---|---|
| `reader` | get/list/watch pods, pod logs, events, services, endpoints, EndpointSlices, deployments, ReplicaSets and ConfigMaps; list namespaces | read Secrets, change anything, exec into pods | each namespace in `AGENT_NAMESPACES` (the four `shop-*` by default); listing namespaces is cluster-wide |
| `remediator` | get and patch Services; get and list pods and EndpointSlices | delete anything, read Secrets or other types, act in other namespaces | only `AGENT_REMEDIATION_NAMESPACES` (`shop-checkout`) |

Each identity gets a short-lived token (`AGENT_TOKEN_TTL_HOURS`, 12 by default), checked with a "who am I" call and written to its own kubeconfig under `.kube-agent/` (gitignored, mode 0600). When the tokens expire, run `agent setup` again. It's safe to re-run, and a running MCP server picks up the new token without a restart.

Access is granted per namespace, never cluster-wide: ConfigMaps in other namespaces, such as `kube-system`, can hold sensitive data. `kube-system`, `kube-public`, `kube-node-lease` and `k8s-agent` can't be configured as agent namespaces at all. Deleting a namespace deletes its grants, so `agent scenario up` grants them again every time it creates a scenario. `agent setup` grants every configured namespace that already exists, and removes the grants of any namespace you've taken out of the settings.

Known limit: Kubernetes grants permissions per object type, so the remediator could change any field of a Service in `shop-checkout`. What keeps the fix narrow is the fix tool's code, a human approval and the short token lifetime.

### MCP server (`docker compose up -d k8s-mcp`)

The agent's only view of the cluster is the community [Kubernetes MCP server](https://github.com/containers/kubernetes-mcp-server) (`quay.io/containers/kubernetes_mcp_server`, release v0.1.2 pinned by digest), running as a container beside the cluster and configured by [`mcp/config.toml`](mcp/config.toml):

- **Read-only mode**, with exactly 7 tools: `namespaces_list`, `pods_list_in_namespace`, `pods_get`, `pods_log`, `events_list`, `resources_get`, `resources_list`. Tools that exec, run, delete or change anything don't exist on the server.
- **Secrets blocked** by the server (`denied_resources`), in addition to the reader's permissions.
- **The reader's kubeconfig only.** The remediator's credentials never enter this container.
- **Host networking**, so the kubeconfig's address (often `127.0.0.1:<port>`) works unchanged inside the container. It listens on `127.0.0.1:8080` only (`AGENT_MCP_PORT`), at `http://localhost:8080/mcp`, because the server has no login of its own.
- **Hardened:** a non-root user, all Linux capabilities dropped, no privilege escalation, and a health check.

## Failure scenarios

Four scenarios, each in its own namespace. Each is a folder in `scenarios/` with plain Kubernetes YAML (public images pinned by digest) and a `scenario.yaml` holding the namespace, the question to ask, the accepted answers, and the condition that proves the fault is live.

Nothing the agent can see hints at the fault. Namespaces and labels look like a normal app (`shop-checkout`, ownership label `app.kubernetes.io/part-of=k8s-agent-demo`), and the folder name, which does describe the fault, never reaches the cluster. The loader rejects a scenario whose namespace, object names, labels or annotations contain its folder name.

| Folder | Namespace | What's broken | Question for the agent |
|---|---|---|---|
| `crashloop` | `shop-orders` | The ConfigMap key is misspelled (`DATABSE_URL`), so the app logs "DATABASE_URL not set", exits 1 and goes into CrashLoopBackOff. | The orders service keeps restarting. What's wrong? |
| `selector` | `shop-checkout` | The Service selects `app: check-out` but the pods are labelled `app: checkout`, so the Service has no endpoints while the pods are healthy. | The checkout service isn't reachable from other pods. Why? |
| `readiness` | `shop-catalog` | The readiness probe checks `/healthz`, which nginx answers with 404, so the pods run but never become Ready. | Why is the catalog service not serving traffic? |
| `healthy` | `shop-inventory` | Nothing (the control case). The right answer is "no fault". | Is the inventory service healthy? |

```bash
uv run agent scenario list               # the four scenarios and their namespaces
uv run agent scenario up selector        # create it, grant the agent access, wait until the fault is live
kubectl get pods,endpointslices -n shop-checkout
uv run agent scenario down selector      # delete the namespace (and the grants) and wait until it's gone
```

`up` waits until the fault can be observed (for `crashloop`, until the pod has restarted twice, about 40 seconds). Running `up` again recreates the scenario from scratch. Both commands refuse to touch a namespace without the ownership label, such as your own `shop-orders`.

## Development

```bash
make check         # lint + unit tests (no cluster, Docker or network; never reads your .env)
make accept M=2    # acceptance tests for milestones 1 to 2 against your cluster (about 4 minutes)
make accept        # every milestone's acceptance tests
```

The acceptance tests deploy and remove the scenarios themselves, and clean up even when a test fails. The M2 tests also run `agent setup`, and start the `k8s-mcp` service if it isn't running (and stop it again afterwards). They call the MCP server through `langchain[mcp]`, the client the agent will use.

## Project layout

```
scenarios/<folder>/      manifests/*.yaml + scenario.yaml
src/k8s_agent/           CLI, scenario tooling, agent identities (access.py)
mcp/config.toml          MCP server configuration (read-only, tool allowlist, Secrets denied)
docker-compose.yml       the k8s-mcp service
tests/unit/              fast tests, no cluster
tests/acceptance/        tests against a real cluster
SPEC.md                  design and decisions
PLAN.md                  build plan
```
