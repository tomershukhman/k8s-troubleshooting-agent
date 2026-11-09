# Kubernetes Troubleshooting Agent

An AI agent that investigates a Kubernetes cluster, finds the root cause of an issue, and recommends the next action. It can apply one narrow fix, and only after you approve the exact change.

**Guiding principle: the cluster is the system under investigation; the agent and its tools run beside it,** so a diagnosis never depends on the health of the cluster it diagnoses. In the cluster: the test scenarios, the agent's identities, and the MLflow tracking server that stores the traces.

Design, with each decision and the alternatives rejected: [SPEC.md](SPEC.md). Build plan: [PLAN.md](PLAN.md).

## Requirements

- **A local Kubernetes cluster.** Tested on [OrbStack](https://orbstack.dev/) (macOS) with its Kubernetes cluster turned on. kind, minikube and Docker Desktop should work but are untested ([other clusters](#other-clusters-kind-minikube)).
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 if needed).
- [Helm 4](https://helm.sh/docs/intro/install/), which `agent setup` uses to install the MLflow tracking server.
- Docker with Compose and host networking (built in on OrbStack and Linux; on Docker Desktop 4.34+: Settings → Resources → Network → Enable host networking).
- On native Linux Docker only: set `AGENT_CONTAINER_USER` in `.env` to your `id -u`:`id -g`, so the MCP container can read the credential file `agent setup` writes.
- An [Anthropic API key](https://console.anthropic.com/), as `ANTHROPIC_API_KEY` in `.env` or your environment. Runs are billed to that key's API account.

## Setup

With OrbStack's Kubernetes running:

```bash
uv sync                                  # Python 3.14 and every dependency, the evals' included
cp .env.example .env && chmod 600 .env   # then set ANTHROPIC_API_KEY (the file holds your key)
uv run agent setup                       # the agent's identities and the tracking server (re-run to refresh tokens)
docker compose up -d --wait k8s-mcp      # the read-only MCP server
```

`agent setup`, `agent scenario` and `agent eval` use your own kubeconfig and its current context (`orbstack` on OrbStack); set `AGENT_KUBE_CONTEXT` for another. `agent investigate` never uses your kubeconfig: it reads the cluster only through the MCP server, as the read-only `reader` ([safety boundary](#safety-boundary)). Every setting and its default is in [`.env.example`](.env.example).

### Other clusters (kind, minikube)

Untested, but the same steps should work. These clusters give LoadBalancer Services no address, so forward the tracking server before `agent investigate` or `agent eval`:

```bash
kubectl port-forward -n mlflow svc/mlflow-mlflow 5050 &
```

## Reproducing the failure scenarios

Each scenario is a folder in `scenarios/` (plain Kubernetes YAML plus a `scenario.yaml` with the question, the accepted answers and the condition that proves the fault is live) and runs in its own namespace. Nothing the agent can see hints at the fault: the namespaces look like a normal app, and the folder name never reaches the cluster ([SPEC 2.2](SPEC.md#22-scenarios)).

| Scenario | Fault | Namespace | Question for the agent |
|---|---|---|---|
| `crashloop` | The ConfigMap key is misspelled (`DATABSE_URL`), so the app exits 1 and goes into CrashLoopBackOff. | `shop-orders` | The orders service keeps restarting. What's wrong? |
| `selector` | The Service selects `app: check-out` but the pods are labelled `app: checkout`, so it has no endpoints. | `shop-checkout` | The checkout service isn't reachable from other pods. Why? |
| `readiness` | The readiness probe checks `/healthz`, which nginx answers with 404, so the pods never become Ready. | `shop-catalog` | Why is the catalog service not serving traffic? |
| `healthy` | Nothing (the control case). The right answer is "no fault". | `shop-inventory` | Is the inventory service healthy? |

```bash
uv run agent scenario list               # the four scenarios and their namespaces
uv run agent scenario up selector        # recreate it with the agent's access, wait until the fault is live
uv run agent scenario down selector      # delete the namespace and wait until it's gone
```

`up` waits until the fault can be observed (for `crashloop`, two restarts: about 40 seconds). Both commands refuse a namespace without the ownership label `app.kubernetes.io/part-of=k8s-agent-demo`.

## Running the agent

```bash
uv run agent scenario up selector
uv run agent investigate "The checkout service isn't reachable from other pods. Why?"
```

- The agent finds the namespace itself; `-n <namespace>` points it at one.
- `--user NAME` records who asked (default: `AGENT_USER_ID`, else your OS user).
- `--quiet` turns off the progress; `--json` prints the result as JSON even in a terminal.

In a terminal you see each stage, each tool call and the critic's verdict, then the diagnosis. On the selector scenario (trimmed):

```
node     investigate
  agent    investigator · claude-sonnet-5-5
    tool     resources_list(Service -n shop-checkout)  E2 · 34 ms
    tool     pods_list_in_namespace(-n shop-checkout)  E3 · 35 ms
    tool     resources_list(EndpointSlice -n shop-checkout)  E4 · 34 ms
    …
node     verify_evidence
  check    code checks passed
  agent    critic · claude-sonnet-5-5
  → founded
node     remediate
  agent    remediator · claude-sonnet-5-5
    tool     get_service(checkout -n shop-checkout)
    tool     list_pods(-n shop-checkout selector=app=checkout)
╭─ Fault found · selector_mismatch · Service/checkout in shop-checkout ──────────────────────────────────────╮
│ The checkout Service selects pods with app=check-out, but the checkout pods are labeled app=checkout. The  │
│ selector matches no pods, so the Service has no endpoints and nothing can reach it.                        │
│                                                                                                            │
│ Ref  Finding                                                 From                                          │
│ E2   Service shop-checkout/checkout has selector             resources_list Service -n shop-checkout       │
│      app=check-out.                                                                                        │
│ E3   Both checkout pods are Running and Ready, with the      pods_list_in_namespace -n shop-checkout       │
│      label app=checkout, not app=check-out.                                                                │
│ E4   The checkout EndpointSlice has no endpoints (ENDPOINTS  resources_list EndpointSlice -n shop-checkout │
│      <unset>).                                                                                             │
│                                                                                                            │
│ Next step: Change the Service selector from app=check-out to app=checkout so it matches the checkout pods. │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Approve this fix? ────────────────────────────────────────────────────────────────────────────────────────╮
│ Service          shop-checkout/checkout                                                                    │
│ Selector         app=check-out  →  app=checkout  (replaces the whole selector)                             │
│ Checked now      2 ready pods match app=checkout: checkout-69db948c9f-5tx4d                                │
│                  (ReplicaSet/checkout-69db948c9f), checkout-69db948c9f-s77kl                               │
│                  (ReplicaSet/checkout-69db948c9f)                                                          │
│ Applies only if  the Service is still at version 34889                                                     │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Apply this change? [y/N]
```

### The fix

Only for a Service whose selector doesn't match its pods, in a namespace listed in `AGENT_REMEDIATION_NAMESPACES` (`shop-checkout`). Every other diagnosis stays advice.

- **You see the diagnosis** the critic verified: root cause, each finding with the tool call behind it, the next step.
- **Then the exact change**, with facts the remediator has just read, and answer y/N.
- **`y`: it's applied** as one JSON Patch that holds only if the Service is unchanged since the remediator read it.
- **The investigator rechecks** with fresh tool calls; the fix is `verified` only if the Service has ready endpoints and the recheck says the problem is gone.
- **Anything else is a reject, and nothing changes:** `n`, no answer within `AGENT_APPROVAL_TIMEOUT_SECONDS` (600), or no terminal on stdin. There's no `--yes`, and you can't edit the change.

Details: [SPEC 3.4](SPEC.md#34-remediation-a-remediator-agent-behind-human-approval).

### Traces

Every run, fix included, is one MLflow trace: each stage, model call and tool call with its inputs, outputs, errors and latency, plus the critic's verdicts and your approval. The CLI prints its link after each run (`Trace: http://localhost:5050/#/experiments/…`); the UI is at http://localhost:5050. What each span holds: [SPEC 3.7](SPEC.md#37-observability).

### Output and exit codes

Piped, or with `--json`, stdout is the result JSON: the diagnosis (`fault_found`, `no_fault` or `inconclusive`), the evidence behind each finding, the verify rounds and the fix record ([SPEC 4.2](SPEC.md#42-run-json)).

| Exit | When |
|---|---|
| 0 | a diagnosis, `inconclusive` included |
| 1 | a model API error (bad key, rate limit, network): `"status": "failed"` |
| 2 | the MCP server can't be reached, or a setting is invalid; nothing ran |

## Running the evaluations

```bash
uv run agent eval                                # every scenario once, plus the selector fix run
uv run agent eval --scenario selector --runs 3   # measure reliability: 2 of 3 must pass
```

For each scenario, `agent eval` recreates it, asks its question without naming the namespace, and scores the answer with MLflow's [`mlflow.genai.evaluate`](https://mlflow.org/docs/latest/genai/eval-monitor/). The selector scenario also gets one fix run, approved automatically (the only automatic approval anywhere) and checked to work. It prints a pass/fail table, writes `eval-results/<time>.json`, and exits non-zero if any scenario fails. Every score is on its run's trace in MLflow (experiment `k8s-agent-evals`).

| Check | By | Passes when |
|---|---|---|
| `diagnosis_correct` | code | status, fault category and resource are among the scenario's accepted answers |
| `evidence_supports` | judge | the cited tool outputs show each finding and establish the diagnosis |
| `advice_ok` | judge | the recommended action meets the scenario's rubric, and is safe |
| `evidence_exists` | code | the answer is the agent's own and valid, and every cited evidence ID is a tool call in the trace |
| `right_tools` | code | the agent looked at the right things (`required_tools` in `scenario.yaml`) |
| `actions_safe` | code | only allowed tools ran, and no fix was proposed except the expected one |
| `remediation_works` | code | the fix run's fix was applied, the Service got endpoints, and the recheck verified it |

**Pass rule:** by default each scenario runs once and every check must pass. With `--runs N`, the code checks must pass in every run, and the diagnosis and judges in 2 of 3 (`AGENT_EVAL_PASS_SHARE`).

## Design rationale

### Framework: a LangGraph workflow with `create_agent` inside

- **An explicit LangGraph `StateGraph`** (investigate → verify evidence → result, then the fix and recheck): the stages, the bounded verify loop and the routing are inspectable, each is a span in the trace, and "evidence before conclusions" is checked inside the agent, not only in the evals.
- **LangChain `create_agent` for the investigator and the remediator:** its middleware covers the stopping rule, tool retries and the approval pause, and Claude's native structured output shapes the answer. Custom code is only the evidence checks, routing, prompts, schemas and the fix tool.
- **One investigator, not several:** these faults are found by connecting facts across sources (a selector and the pods' labels, a log line and a ConfigMap), which one context sees whole. A tool-less critic checks the evidence; a separate remediator holds the only write tool.
- **Provider-agnostic:** the model is a config string (`AGENT_MODEL`, default Claude Sonnet 5.5). The path to scale is sub-agents (an agent wrapped as a tool).
- **Rejected:** hand-built loops in raw LangGraph, Deep Agents, Pydantic AI (a close second), single-provider SDKs ([SPEC 3.1](SPEC.md#31-agent-topology-a-langgraph-workflow-with-three-roles), [SPEC 3.2](SPEC.md#32-framework-langgraph-with-create_agent-inside)).

### Tool boundaries

| Agent | Identity | Tools |
|---|---|---|
| investigator | `reader`, through the MCP server | 7 read tools: `namespaces_list`, `pods_list_in_namespace`, `pods_get`, `pods_log`, `events_list`, `resources_get`, `resources_list` |
| critic | none | none: it reads only the draft and the tool outputs it cites |
| remediator | `remediator`, through its own Kubernetes client | 3 reads and `patch_service_selector`, the only tool anywhere that changes something |

- **No shell, no `kubectl` passthrough, no exec**, anywhere.
- **The read allowlist is enforced twice:** by the MCP server (read-only mode, `enabled_tools` in [`mcp/config.toml`](mcp/config.toml)), and again when the agent loads its tools.
- **The evals check it on every run's trace** (`actions_safe`) ([SPEC 3.3](SPEC.md#33-cluster-tools-an-mcp-server-beside-the-cluster)).

### Evaluation approach

- **MLflow's own evaluation, not a custom harness:** every score sits on its run's trace, so you see what the agent did and how it was judged.
- **Code checks what can be checked exactly:** the diagnosis against the accepted answers, every cited evidence ID against the trace, and which tools ran.
- **Two judges for what needs judgment:** whether the cited outputs support the conclusion, and whether the advice is appropriate and safe, since a person may run it with admin rights.
- **Independent of the agent:** the judges run on Claude Opus 5.5 and never see the critic's verdicts.
- **The evals can fail:** a fake agent that always answers "no fault" fails, and the judges reject an irrelevant citation and "delete the namespace and redeploy".
- **Per scenario, only data** in `scenario.yaml`: accepted answers, required tools, a rubric. No keyword lists.

Details: [SPEC 2.3](SPEC.md#23-definition-of-done-evals), [SPEC 3.6](SPEC.md#36-scenarios-and-evals).

### Safety boundary

The cluster enforces it, through two identities from [`deploy/rbac.yaml`](deploy/rbac.yaml), bound per namespace by each scenario's `namespace.yaml`:

| Identity | Can | Can't | Where |
|---|---|---|---|
| `reader` | Kubernetes' built-in `view` role (pods, logs, events, Services, EndpointSlices, ConfigMaps, workloads…); list namespaces | read Secrets, Roles or RoleBindings; change anything; exec into pods | each namespace with a `k8s-agent-reader` RoleBinding (the four `shop-*`) |
| `remediator` | get and patch Services; get and list pods and EndpointSlices | delete anything, read Secrets or other types, act in other namespaces | only where a `k8s-agent-remediator` RoleBinding exists (`shop-checkout`) |

- **Short-lived tokens** (`AGENT_TOKEN_TTL_HOURS`, 12), one per identity, in its own kubeconfig under `.kube-agent/`; re-run `agent setup` to refresh them.
- **Access per namespace, never cluster-wide.** To let the agent read another namespace, add a `reader` RoleBinding there (copy one from a scenario's `namespace.yaml`).
- **The MCP server** ([kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server), pinned by digest): read-only, exactly the 7 tools, Secrets denied, listening on `127.0.0.1` only.
- **The fix** runs only after a person approves its exact call, and code rejects anything but the proposed fix.
- **Tool output is data:** logs, events and ConfigMaps can hold text written by anyone; a manipulated model's worst case is a wrong diagnosis or a fix proposal, which still stops at the code checks and your approval.
- **Known limit:** Kubernetes grants permissions per object type, so the remediator could patch any field of a Service in `shop-checkout`. The fix tool's code (only `spec.selector`), your approval and the short token lifetime keep it narrow.
- **Local, single-user setup:** the MLflow tracking server has no login, so add authentication or a NetworkPolicy for anything shared. `.env` holds your API key: keep it `chmod 600`.

Details: [SPEC 3.5](SPEC.md#35-safety-boundary-cluster-enforced).

## Development

```bash
make check         # lint, type check + unit tests (no cluster, Docker or network; never reads your .env)
make accept M=6    # milestone 6's acceptance tests, against your cluster, the real model and MLflow
make accept        # every milestone's acceptance tests
```

## Project layout

```
scenarios/<folder>/   manifests/*.yaml + scenario.yaml, one folder per scenario
deploy/               the agent's identities (rbac.yaml), the tracking server's Helm values
mcp/config.toml       the MCP server's configuration (read-only, tool allowlist, Secrets denied)
src/k8s_agent/        the `agent` CLI, settings, scenarios, setup, evals
  agent/              the LangGraph workflow, critic, remediator, approval gate, the three prompts
tests/unit/           fast tests, no cluster
tests/acceptance/     tests against a real cluster, one file per milestone
```
