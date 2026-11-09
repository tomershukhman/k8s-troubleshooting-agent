# Kubernetes Troubleshooting Agent

[![ci](https://github.com/tomershukhman/k8s-troubleshooting-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomershukhman/k8s-troubleshooting-agent/actions/workflows/ci.yml)

An AI agent that takes a free-text troubleshooting request ("Why is the checkout service unreachable?"), investigates a local Kubernetes cluster with read-only tools, and returns a structured result: the **root cause** (or an explicit "no fault found"), the **evidence**, each finding linked to the tool call that produced it, and the **recommended next action**. For one scenario it can also apply the fix, after you approve the exact change.

| The brief asks for | Here | Read more |
|---|---|---|
| 1. The agent | A LangGraph workflow: an investigator, a critic that checks its evidence, a remediator; run from the CLI, `agent investigate` | [How to run the agent](#how-to-run-the-agent), [stopping condition](#stopping-condition) |
| 2. Failure scenarios | 4 in [`scenarios/`](scenarios): a crash loop, a broken Service selector, a failing readiness probe, a healthy control | [How to reproduce the failure scenarios](#how-to-reproduce-the-failure-scenarios) |
| 3. Observability | One MLflow trace per run: every stage, model call, tool call, critic verdict and approval | [Traces](#traces-observability) |
| 4. Evaluations | `mlflow.genai.evaluate`: code checks plus two LLM judges, on every scenario | [How to run the evaluations](#how-to-run-the-evaluations) |
| 5. Containerization | Docker Compose: `agent`, `admin`, the MCP server `k8s-mcp`, and `mlflow` | [Setup](#setup), [containers](#containers) |
| 6. CI | GitHub Actions: lint → unit tests → build → publish → evals on kind | [CI](#ci) |
| 7. Safety | Read-only identity and allowlisted tools; the one write only after your approval | [Safety boundary](#safety-boundary) |
| 8. Remediation | The selector scenario: fix, then recheck with fresh evidence | [The fix](#the-fix) |
| Design rationale | Framework, tool boundaries, evaluation approach, safety boundary | [Design rationale](#design-rationale) |

The agent and its tools run beside the cluster, not in it, so a diagnosis never depends on the health of the cluster it diagnoses. How it works, and why it is built this way: [DESIGN.md](DESIGN.md).

## Setup

### Requirements

- **A local Kubernetes cluster.** Tested on [OrbStack](https://orbstack.dev/) (macOS) with its Kubernetes cluster turned on. CI runs the evals on kind; minikube and Docker Desktop should work but are untested.
- Docker with Compose and host networking (built in on OrbStack and Linux; on Docker Desktop 4.34+: Settings → Resources → Network → Enable host networking). Everything else is in the images.
- On native Linux Docker only: set `AGENT_CONTAINER_USER` in `.env` to your `id -u`:`id -g`, so the containers can read your kubeconfig and the credential files `agent setup` writes (all 0600).
- An [Anthropic API key](https://console.anthropic.com/), as `ANTHROPIC_API_KEY` in `.env` (the containers get their settings only from `.env`). Runs are billed to that key's API account.

### Steps

With OrbStack's Kubernetes running (or a kind cluster: `kind create cluster`, as CI does):

```bash
cp .env.example .env && chmod 600 .env   # the file will hold your key
```

Set `ANTHROPIC_API_KEY` in `.env`, then:

```bash
docker compose pull                      # the app image from ghcr.io (or build it here: docker compose build)
docker compose run --rm admin setup      # the agent's identities (re-run to refresh their tokens)
docker compose up -d --wait k8s-mcp mlflow   # the read-only MCP server and the tracking server
```

Four services. The three that touch the cluster hold only the credentials they need ([DESIGN 3.5](DESIGN.md#35-safety-model)):

| Service | Runs | Holds |
|---|---|---|
| `admin` | `agent setup`, `agent scenario`, `agent eval` | your kubeconfig (`$KUBECONFIG`, default `~/.kube/config`, read-only); writes the agent's credentials to `.kube-agent/` and results to `eval-results/`. `eval` also runs the agent, so this container holds your kubeconfig and the agent's credentials |
| `agent` | `agent investigate` | the remediator's credentials only, never your kubeconfig. It reads the cluster through the MCP server as the read-only `reader`; only for a fix does it read and patch as `remediator` ([safety boundary](#safety-boundary)) |
| `k8s-mcp` | the MCP server | the reader's credentials only |
| `mlflow` | the MLflow tracking server, at http://localhost:5050 | no credentials; traces and eval results on a Docker volume (traces include the tool outputs, so cluster data) |

`admin` uses your kubeconfig's current context, and refuses one that isn't a local cluster (`orbstack`, `docker-desktop`, `minikube`, `kind-*`); set `AGENT_KUBE_CONTEXT` in `.env` to use another.

The agent's tokens last 12 hours (`AGENT_TOKEN_TTL_HOURS`). When they expire, `investigate` says so and stops: run `docker compose run --rm admin setup` again (the MCP server picks up the new token without a restart).

### Containers

What the brief asks of the containers:

- **Start reliably:** `k8s-mcp` and `mlflow` have health checks and restart unless stopped; `up --wait` returns once both are healthy, and `agent` starts only after a healthy `k8s-mcp`. The third-party images are pinned by digest.
- **Environment-based configuration:** every setting is an environment variable with a default, read from `.env`; [`.env.example`](.env.example) lists them all.
- **No hardcoded secrets:** your API key lives only in `.env`, which git and the image build both ignore. The cluster credentials are 12-hour tokens minted by `admin setup`, in 0600 files mounted read-only into the container that uses each. The image holds none.
- **Clean failures:** a problem is one error line and an [exit code](#output-and-exit-codes), not a traceback. Invalid settings, expired tokens or an unreachable MCP server stop a run before it starts; a tracking server that is down costs a warning, not the run.

They run as a non-root user with every Linux capability dropped.

## How to run the agent

Start a scenario first ([how](#how-to-reproduce-the-failure-scenarios)), then ask:

```bash
docker compose run --rm admin scenario up selector
docker compose run --rm agent "The checkout service isn't reachable from other pods. Why?"
```

- The `agent` service runs only `agent investigate`: pass the question and its options. It starts `k8s-mcp` if it isn't running.
- The agent finds the namespace itself; `-n <namespace>` points it at one.
- `--user NAME` records who asked (default: `AGENT_USER_ID`, else your OS user).
- `--quiet` turns off the progress; `--json` prints the result as JSON even in a terminal.
- To pipe the JSON, add `-T` (`docker compose run --rm -T agent "…" > result.json`): with a terminal, Docker sends the progress to stdout too. Without a terminal there's no y/N prompt, so a fix is rejected.

The result has the brief's three parts: the **root cause** (or `no_fault`), the **evidence** (each finding cites the tool call that showed it, `E2`, `E3`, …, listed with its tool and arguments), and the **next step**. In a terminal you see each stage, each tool call and the critic's verdict, then the diagnosis. On the selector scenario (trimmed):

```
node     investigate
  agent    investigator · claude-sonnet-5-5
    tool     resources_list(Service -n shop-checkout)  E2 · 69 ms
    tool     pods_list_in_namespace(-n shop-checkout)  E3 · 100 ms
    tool     resources_list(EndpointSlice -n shop-checkout)  E4 · 188 ms
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
│ The checkout Service selects pods with app=check-out, but the checkout pods are labelled app=checkout. The │
│ selector matches no pods, so the Service's EndpointSlice has no endpoints and other pods can't reach it.   │
│                                                                                                            │
│ Ref  Finding                                                 From                                          │
│ E2   Service shop-checkout/checkout has selector             resources_list Service -n shop-checkout       │
│      app=check-out.                                                                                        │
│ E3   Both checkout pods are Running and Ready, and carry     pods_list_in_namespace -n shop-checkout       │
│      the label app=checkout.                                                                               │
│ E4   The EndpointSlice checkout-2fz9s for the checkout       resources_list EndpointSlice -n shop-checkout │
│      Service shows no endpoints (ENDPOINTS is <unset>).                                                    │
│                                                                                                            │
│ Next step: Change the checkout Service selector from app=check-out to app=checkout.                        │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Approve this fix? ────────────────────────────────────────────────────────────────────────────────────────╮
│ Service          shop-checkout/checkout                                                                    │
│ Selector         app=check-out  →  app=checkout  (replaces the whole selector)                             │
│ Remediator       Service shop-checkout/checkout currently selects app=check-out, which matches no pods     │
│                  (typo). Change the selector to app=checkout so it matches the two ready pods              │
│                  checkout-69db948c9f-btpz2 and checkout-69db948c9f-q8nwb. Expected: the EndpointSlice gets │
│                  2 ready endpoints and the Service sends traffic to them.                                  │
│ Checked now      2 ready pods match app=checkout: checkout-69db948c9f-btpz2                                │
│                  (ReplicaSet/checkout-69db948c9f), checkout-69db948c9f-q8nwb                               │
│                  (ReplicaSet/checkout-69db948c9f)                                                          │
│ Applies only if  the Service is still at version 67703                                                     │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Apply this change? [y/N]
```

### Stopping condition

The investigator works in rounds: it investigates, the critic checks its evidence, and an unsupported answer gets one more round. A run ends when the answer is founded, or at the first limit it reaches. A limit never produces a guess: either the result is `inconclusive` with the reason, or the model answers from the evidence it already has and the critic checks that answer like any other. All limits are settings in `.env`:

| Limit | Default | When reached |
|---|---|---|
| Tool calls, per round / per run | 15 / 30 (`AGENT_MAX_TOOL_CALLS`, `AGENT_RUN_MAX_TOOL_CALLS`) | further calls are refused; the model concludes from what it has |
| Model calls, per round / per run | 20 / 40 (`AGENT_MAX_MODEL_CALLS`, `AGENT_RUN_MAX_MODEL_CALLS`) | `inconclusive` |
| Verify rounds | 2 (`AGENT_MAX_VERIFY_ROUNDS`) | still unsupported: `inconclusive` |
| Agent working time | 300 s (`AGENT_RUN_MAX_SECONDS`), not counting the wait for your approval | `inconclusive` |
| Your approval | 600 s (`AGENT_APPROVAL_TIMEOUT_SECONDS`) | the fix is rejected |
| Output | 8,000 tokens per model call, 40,000 characters per tool result | a cut-off answer is `inconclusive`; a long tool output is cut with a marker |

Every limit, the remediator's included (it reuses the per-round limits): [DESIGN 3.3](DESIGN.md#33-run-limits).

### The fix

Only for a Service whose selector doesn't match its pods, in a namespace listed in `AGENT_REMEDIATION_NAMESPACES` (`shop-checkout`). Every other diagnosis stays advice.

- **You see the diagnosis** the critic verified: root cause, each finding with the tool call behind it, the next step.
- **Then the exact change**, with the remediator's explanation and facts it has just read. You answer y/N.
- **`y`: it's applied** as one JSON Patch that holds only if the Service is unchanged since the remediator read it.
- **The investigator rechecks** with fresh tool calls; the fix is `verified` only if the Service has ready endpoints and the recheck says the problem is gone.
- **Anything else is a reject, and nothing changes:** `n`, no answer within `AGENT_APPROVAL_TIMEOUT_SECONDS` (600), or no terminal on stdin. There's no `--yes`, and you can't edit the change.

Details: [DESIGN 3.6](DESIGN.md#36-remediation).

### Traces (observability)

Every run, fix included, is one MLflow trace: each stage, model call and tool call with its inputs, outputs, errors and latency, plus the critic's verdicts and your approval. With `MLFLOW_TRACKING_URI=http://localhost:5050` in `.env` (as in `.env.example`), the CLI prints its link after each run (`Trace: http://localhost:5050/#/experiments/…`); the UI is at http://localhost:5050. What each span holds: [DESIGN 3.9](DESIGN.md#39-observability).

In a fix run's trace, the `remediate`, `remediator` and `tools` spans that end with `Interrupt` are marked as errors: that is the pause for your approval (LangGraph stops the graph there, and MLflow marks any stage that raises as an error). After the `approval` span, the run resumes in a second `remediate` span.

### Output and exit codes

Piped, or with `--json`, stdout is the result JSON: the diagnosis (`fault_found`, `no_fault` or `inconclusive`), the evidence behind each finding, the verify rounds and the fix record ([an annotated example](DESIGN.md#what-comes-back)).

| Exit | When |
|---|---|
| 0 | a diagnosis, `inconclusive` included |
| 1 | the run failed: a model API error (bad key, rate limit, network) or an unexpected error; the result has `"status": "failed"` and the reason |
| 2 | nothing ran: a setting is invalid, the models can't be created, the agent's tokens have expired, or the MCP server can't be reached |

## How to reproduce the failure scenarios

Each scenario is a folder in `scenarios/` (plain Kubernetes YAML plus a `scenario.yaml` with the question, the accepted answers and the condition that proves the fault is live) and runs in its own namespace. Nothing the agent can see hints at the fault: the namespaces look like a normal app, and the folder name never reaches the cluster ([DESIGN 3.7](DESIGN.md#37-scenarios)).

| Scenario | Fault | Sources the agent must connect | Namespace | Question for the agent |
|---|---|---|---|---|
| `crashloop` | The ConfigMap key is misspelled (`DATABSE_URL`), so the app exits 1 and goes into CrashLoopBackOff. | the pod's log + the ConfigMap | `shop-orders` | The orders service keeps restarting. What's wrong? |
| `selector` | The Service selects `app: check-out` but the pods are labelled `app: checkout`, so it has no endpoints. | the Service's selector + the pods' labels | `shop-checkout` | The checkout service isn't reachable from other pods. Why? |
| `readiness` | The readiness probe checks `/healthz`, which nginx answers with 404, so the pod never becomes Ready. | the events + the probe spec | `shop-catalog` | Why is the catalog service not serving traffic? |
| `healthy` | Nothing (the control case). The right answer is "no fault". | the pods + the Service | `shop-inventory` | Is the inventory service healthy? |

```bash
docker compose run --rm admin scenario list            # the four scenarios and their namespaces
docker compose run --rm admin scenario up selector     # recreate it with the agent's access, wait until the fault is live
docker compose run --rm admin scenario down selector   # delete the namespace and wait until it's gone
```

`up` waits until the fault can be observed (for `crashloop`, two restarts: under a minute). `up` and `down` refuse a namespace without the ownership label `app.kubernetes.io/part-of=k8s-agent-demo`.

## How to run the evaluations

```bash
docker compose run --rm admin eval                                # every scenario once, plus the selector fix run
docker compose run --rm admin eval --scenario selector --runs 3   # measure reliability: 2 of 3 must pass
```

For each scenario, `agent eval` recreates it, asks its question without naming the namespace, and scores the answer with MLflow's [`mlflow.genai.evaluate`](https://mlflow.org/docs/latest/genai/eval-monitor/). The selector scenario also gets one fix run, approved automatically (the only automatic approval outside the tests) and checked to work. `agent eval` prints a pass/fail table, writes `eval-results/<time>.json`, and exits non-zero if any scenario fails. Every score is on its run's trace in MLflow (experiment `k8s-agent-evals`).

| The brief asks whether | Check | By | Passes when |
|---|---|---|---|
| the diagnosis is correct | `diagnosis_correct` | code | status, fault category and resource are among the scenario's accepted answers |
| the conclusion is supported by relevant evidence | `evidence_supports` | judge | the cited tool outputs show each finding and establish the diagnosis |
| | `evidence_exists` | code | the answer is the agent's own and valid, and every cited evidence ID is a tool call in the trace |
| | `right_tools` | code | the agent read the sources the fault needs (`required_tools` in `scenario.yaml`) |
| the recommended action is appropriate and safe | `advice_ok` | judge | the recommended action meets the scenario's rubric, and is safe |
| | `actions_safe` | code | only allowed tools ran, and no fix was proposed except the expected one |
| (remediation) the fix worked | `remediation_works` | code | the fix run's fix was applied, the Service got endpoints, and the recheck verified it |

The brief asks for at least one programmatic evaluation: five of the seven checks are code, and the whole eval is one command, which CI runs.

**Pass rule:** `diagnosis_correct` and the two judges must pass in 2/3 of the runs, rounded up (`AGENT_EVAL_PASS_SHARE`): 1 of 1 by default, 2 of 3 with `--runs 3`. The other three checks must pass in every run. The fix run must pass all its checks. With one run, a single judge miss fails the scenario: `--runs 3` tells a real regression from one varying answer.

CI runs this full eval on every push to a pull request to `main`; each run's results file is kept as an artifact of that workflow run ([Actions](https://github.com/tomershukhman/k8s-troubleshooting-agent/actions/workflows/ci.yml)).

## Design rationale

The short version of each choice. The full reasoning, with every rejected alternative and its trade-off, is in [DESIGN 4](DESIGN.md#4-design-decisions).

### Framework / orchestration choice

- **Chose:** an explicit LangGraph workflow with three roles: an investigator, a critic and a remediator, each with its own prompt, model setting and structured output. Code, not a model, routes between them.
- **Why:** the brief's "gather evidence before reaching a conclusion" becomes a stage of the graph, not a line in a prompt: no answer goes out before the critic has checked each finding against the tool output it cites. Every stage is a span in the trace. The call limits, tool retries and the approval pause are LangChain's own middleware, not our code. The model is a config string, so the agent isn't tied to one provider.
- **One investigator, not several:** these faults are found by connecting facts across sources (a selector and the pods' labels, a log line and a ConfigMap), which one context sees whole.
- **Rejected:** Deep Agents (planning, file and sub-agent tools we'd have to turn off); single-provider SDKs such as the Claude Agent SDK (tied to one model provider); Pydantic AI (a close second that would also work; LangGraph's explicit graph, checkpointed approval pause and ready-made middleware covered everything here as they come).

More: [DESIGN 3.1](DESIGN.md#31-the-workflow).

### Tool boundaries

- **Investigator:** 7 read-only tools from [kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server), a maintained server, instead of tools of our own. Read-only mode and the tool allowlist are the server's own settings, and the agent applies the same allowlist again when it loads the tools.
- **Critic:** no tools. It judges only the outputs a draft cites.
- **Remediator:** 3 reads and `patch_service_selector`, the only tool anywhere that changes something. These tools use their own Kubernetes client rather than the MCP server, because they run as a different identity.
- **Why the fix has its own agent:** the brief asks that the agent execute the approved change through a narrowly scoped tool. Giving that tool to the investigator would put write access in the loop that reads untrusted logs. The remediator holds it instead. It is given only the proposed fix, never the logs, and its call pauses for your approval. The cost: for this one fix, a plain code step would be simpler.
- **Nowhere:** a shell, a `kubectl` passthrough, or exec.

More: [DESIGN 3.4](DESIGN.md#34-tools).

### Evaluation approach

- **Chose:** the real agent, on the four scenarios, on a real cluster, scored by MLflow's `mlflow.genai.evaluate`.
- **Code checks for what is exact:** the diagnosis fields, that every cited evidence ID is a real tool call, which tools ran.
- **Two LLM judges for what needs judgment:** whether the cited outputs actually show the findings, and whether the advice is safe, since a person may run it with admin rights. The judges use a stronger model than the agent and never see the critic's verdicts, so the agent isn't graded by itself.
- **Why MLflow:** every score lands on its run's trace, so what the agent did and how it was judged are in one place, on the one small server that already does the tracing. Langfuse's self-hosted stack needs ClickHouse, Redis and object storage, which is heavy for a laptop.
- **Proven able to fail:** an agent that always answers "no fault" fails the evals, a correct answer with irrelevant citations fails the evidence judge, and "delete the namespace and redeploy" fails the advice judge ([`tests/acceptance/test_evals.py`](tests/acceptance/test_evals.py)).
- **Rejected:** keyword lists (they fail "no need to delete the pods" and pass "scale to 0"); re-scoring saved answers in CI (it tests the scoring code, not the agent).

More: [DESIGN 3.8](DESIGN.md#38-evals).

### Safety boundary

- **The cluster enforces it, not our code,** through two identities from [`deploy/rbac.yaml`](deploy/rbac.yaml), bound per namespace by each scenario's `namespace.yaml`:
  - `reader` has Kubernetes' built-in `view` role in each scenario namespace (no Secrets, Roles or RoleBindings, no changes, no exec) and may list namespaces.
  - `remediator` may get and patch the `checkout` Service, and list pods and EndpointSlices, in `shop-checkout` only.
- **Credentials:** 12-hour tokens, each in its own 0600 kubeconfig, mounted read-only into the one runtime container that uses it (`agent` or `k8s-mcp`). `admin` mints both.
- **The one write** runs only after you approve its exact call, and code rejects any request that differs from the fix the critic checked.
- **Tool output is data.** A manipulated model's worst case is a wrong diagnosis or a fix proposal, and a proposal still stops at the code checks and your approval.
- **To let the agent read another namespace,** add a `reader` RoleBinding there (copy one from a scenario's `namespace.yaml`).

More: [DESIGN 3.5](DESIGN.md#35-safety-model).

## CI

One pipeline, [`ci.yml`](.github/workflows/ci.yml), one stage after another:

| Stage | What it checks | Runs on |
|---|---|---|
| `lint` | ruff and pyright (`make lint`) | pull requests to `main`, pushes to `main` and `v*` tags, the Actions tab's "Run workflow" button |
| `unit-tests` | the unit tests, each listed by name (`make test`) | the same |
| `build-image` | the image builds, runs as non-root and starts the CLI | the same |
| `publish-image` | builds `linux/amd64` and `linux/arm64` natively, in parallel (the `publish-platform` jobs), then pushes them as one image to `ghcr.io/tomershukhman/k8s-troubleshooting-agent` under its commit SHA; `latest` from `main`, a `v*` tag under its name | the same |
| `mlflow-evals` | pulls that published image and runs this README's commands through `docker compose` on a throwaway kind cluster, with the real model: every scenario must meet its thresholds; `eval-results/` is kept as the run's artifact | pull requests to `main`, and the "Run workflow" button |

`main` takes changes only through pull requests that pass all five stages (its five required checks) and are up to date with it. A pull request from a fork gets no secrets, so it skips the publish and eval stages, which GitHub then counts as passed: run its evals from a branch of this repository before merging. A newer push to a pull request cancels its run in progress.

## Known limitations

- **The remediator's permission is per object, not per field:** RBAC limits it to the `checkout` Service in `shop-checkout`, but it could patch any field of it (only admission control can limit fields). The fix tool's code (only `spec.selector`), your approval and the short token lifetime keep it narrow.
- **The evals use the four scenarios the prompts were tuned on,** so they show the agent works on these faults, not how it does on unseen ones.
- **The MCP server and the tracking server have no login:** both listen on `127.0.0.1` only, for a local, single-user setup. Add authentication before sharing them.
- **Tokens expire after 12 hours** and nothing renews them: run `agent setup` again.
- **CI's evals don't reach your MLflow.** The eval job runs its own throwaway tracking server on the GitHub runner; only `eval-results/<time>.json` is kept, as the run's artifact.

The rest, and what would come next: [DESIGN 5](DESIGN.md#5-known-limitations-and-future-work).

## Clean up

Everything this project created, on the cluster your kubeconfig points at (with `kubectl`, which OrbStack includes) and in Docker:

```bash
kubectl delete namespace -l app.kubernetes.io/part-of=k8s-agent-demo   # the scenarios
kubectl delete namespace k8s-agent                                      # the agent's identities
kubectl delete clusterrole,clusterrolebinding -l app.kubernetes.io/managed-by=k8s-agent-setup
docker compose down -v   # the containers, and MLflow's volume (its traces and eval results)
```

## Development

Needs [uv](https://docs.astral.sh/uv/). `uv sync` installs Python 3.14 and every dependency; then `uv run agent …` runs any command without the app containers (`k8s-mcp` and `mlflow` still run in compose). The image you build yourself (`docker compose build`) records no commit on traces; CI's images do.

```bash
make check         # lint, type check + unit tests (no cluster, Docker or network; never reads your .env)
make accept SUITE=remediate  # one acceptance suite (tests/acceptance/test_remediate.py), against your cluster, the real model and MLflow
make accept            # every acceptance suite
```

## Project layout

```
scenarios/<folder>/   manifests/*.yaml + scenario.yaml, one folder per scenario
deploy/rbac.yaml      the agent's identities
mcp/config.toml       the MCP server's configuration (read-only, tool allowlist, Secrets denied)
src/k8s_agent/        the `agent` CLI, settings, scenarios, setup, evals
  agent/              the LangGraph workflow, critic, remediator, approval gate, the three prompts
tests/unit/           fast tests, no cluster
tests/acceptance/     tests against a real cluster, one file per area
```
