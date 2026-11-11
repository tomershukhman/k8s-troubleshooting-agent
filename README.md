# Kubernetes Troubleshooting Agent

[![ci](https://github.com/tomershukhman/k8s-troubleshooting-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomershukhman/k8s-troubleshooting-agent/actions/workflows/ci.yml)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An LLM agent that diagnoses broken Kubernetes services and shows its work. Ask it in plain words ("Why is the checkout service unreachable?"). It investigates the cluster with read-only tools and answers with the **root cause**, the **evidence** for it, and the **next step**. Every claim cites the tool call that proved it, a separate critic checks that evidence before you see the answer, and the one change it can make waits for your approval.

## What it looks like

A Service whose selector matches no pods: the agent investigates, the critic checks every finding, you approve the exact change, and a recheck with fresh evidence confirms the fix. A real run against a local cluster; only the waits for the model are shortened.

![The agent diagnosing and fixing a broken Service](docs/images/demo.gif)

## Highlights

- **Evidence-cited answers.** Every tool result gets an ID (`E1`, `E2`, …) and every finding cites one, so any claim can be checked against the call behind it.
- **A critic that can't look anything up.** A separate model call with no tools judges each finding against only the output it cites. An unsupported draft goes back once; if it still doesn't hold, the answer is `inconclusive`, never a guess.
- **Safety enforced by the cluster, not by prompts.** The investigator reads through a read-only identity. The one write runs under a second identity that can patch one Service, only after you approve the exact call, and only if the Service hasn't changed since it was read.
- **Built to resist manipulation.** Tool output is treated as data. One test scenario plants an instruction in a ConfigMap telling the agent to lie and delete the namespace; the evals fail if it obeys.
- **Evaluated like software.** The real agent runs on a real cluster against faults, wrong premises, a nonexistent service and a user dictating a bad fix. Code checks score what is exact; two LLM judges, calibrated on known-good and known-bad answers, score what needs judgment. CI runs the full eval on every pull request.
- **Fully traced.** Every run is one MLflow trace: each stage, model call with its reasoning summary, tool call, critic verdict and approval.
- **Production-minded packaging.** Containers run as non-root with every capability dropped, each holding only the credentials it needs, with short-lived tokens and no secrets in the image.

**Built with:** Python 3.14 · [LangGraph](https://github.com/langchain-ai/langgraph) and LangChain agents · Claude Sonnet 5.5 for the agent and Opus 5.5 for the judges (any LangChain model works) · [kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server) · [MLflow](https://mlflow.org/) tracing and evals · Docker Compose · GitHub Actions with kind · uv, ruff, pyright.


## See it in action

**Every run is one trace.** The critic's verdict on each finding, beside every model and tool call, with timing, tokens and cost:

![An MLflow trace of one run, showing the critic's verdict per finding](docs/images/trace.png)

**Every answer is scored.** One eval run of the `selector` scenario: its three questions, each asked three times. Each row is one run of the agent; each column is a check, four by code and two by an LLM judge. All passed:

![MLflow evaluation results: six checks at 100% across nine runs](docs/images/evals.png)

**Every pull request is evaluated.** Lint, tests, a multi-arch image, then the full eval against a throwaway kind cluster with the published image:

![The GitHub Actions pipeline: lint, unit tests, build, publish, evals](docs/images/ci.png)

## How it works

```mermaid
flowchart LR
    q(["Your question"]) --> inv["<b>Investigator</b><br/>reads the cluster"]
    inv -- "draft" --> critic["<b>Critic</b><br/>checks the evidence"]
    critic -- "fixable fault" --> rem["<b>Remediator</b><br/>patches, after your y"]
    critic -. "unsupported:<br/>try again" .-> inv
    rem -. "patched:<br/>recheck" .-> inv
    subgraph final ["Final status"]
        direction TB
        s1(["<b>Fault found · No fault · Inconclusive</b><br/>nothing changed in the cluster"])
        s2(["<b>Fixed</b><br/>the recheck confirms it"])
        s3(["<b>Fix applied, not verified</b><br/>the recheck doesn't confirm it"])
    end
    critic -- "otherwise" --> s1
    rem -- "rejected" --> s1
    inv -- "recheck: resolved" --> s2
    inv -- "recheck: not resolved" --> s3
```

Three roles in a LangGraph workflow, each a separate model call with its own prompt. Code, not a model, decides what runs next.

- **The investigator** decides what to look at. It reads pods, logs, events, Services and other objects, as many steps as it needs, then drafts a diagnosis.
- **The critic** has no tools. It checks each finding in the draft against the tool output that finding cites.
- **The remediator** runs only for the one fault the agent may remediate. It requests the change, the run pauses until you approve it, and then the investigator rechecks with fresh tool calls.

The agent runs beside the cluster, not in it, so a diagnosis never depends on the health of the thing it diagnoses. The full design, with every decision and its trade-off, is in [DESIGN.md](DESIGN.md).

## Quickstart

### Requirements

- **A local Kubernetes cluster.** Tested on [OrbStack](https://orbstack.dev/) (macOS) with its Kubernetes turned on. CI uses [kind](https://kind.sigs.k8s.io/). minikube and Docker Desktop should work but are untested.
- **Docker with Compose and host networking.** Built in on OrbStack and Linux. On Docker Desktop 4.34+: Settings → Resources → Network → Enable host networking.
- **An [Anthropic API key](https://console.anthropic.com/).** Runs are billed to it.
- **On native Linux Docker only:** set `AGENT_CONTAINER_USER` in `.env` to your `id -u`:`id -g`, so the containers can read your kubeconfig and the credential files.

### Set up

With the cluster running (for kind: `kind create cluster`):

```bash
cp .env.example .env && chmod 600 .env   # the file will hold your key
```

Set `ANTHROPIC_API_KEY` in `.env`, then:

```bash
docker compose pull                          # the images
docker compose run --rm admin setup          # the agent's two identities
docker compose up -d --wait k8s-mcp mlflow   # MCP server and tracking server
```

To build the app image yourself instead of pulling it: `docker compose build`.

### Ask your first question

Start a scenario, then ask:

```bash
docker compose run --rm admin scenario up selector
docker compose run --rm agent \
  "The checkout service isn't reachable from other pods. Why?"
```

The trace of the run is at http://localhost:5050; the CLI prints its link.

### What runs

Everything is a container. Each one holds only the credentials it needs.

| Service | What it does | Credentials it holds |
|---|---|---|
| `agent` | `agent investigate`: the agent itself | the remediator's only, never your kubeconfig |
| `k8s-mcp` | the Kubernetes MCP server the investigator reads through | the reader's only |
| `admin` | `agent setup`, `agent scenario`, `agent eval` | your kubeconfig, read-only. `eval` also runs the agent, so it holds the agent's too |
| `mlflow` | the tracking server, at http://localhost:5050 | none |

- **They start reliably.** `k8s-mcp` and `mlflow` have health checks and restart unless stopped. `up --wait` returns once both are healthy, and `agent` starts only after `k8s-mcp` is.
- **All configuration is environment variables,** read from `.env`. [`.env.example`](.env.example) lists every setting with its default.
- **No secret is in the image.** Your API key lives only in `.env`, which git and the image build ignore. The cluster credentials are 12-hour tokens in private files.
- **Failures are clean.** A problem is one error line and an [exit code](#exit-codes), not a traceback. A tracking server that is down costs a warning, not the run.
- **They run locked down:** a non-root user, every Linux capability dropped.

Two things to know:

- `admin` refuses a kube context that isn't a local cluster (`orbstack`, `docker-desktop`, `minikube`, `kind-*`). Set `AGENT_KUBE_CONTEXT` in `.env` to use another.
- The agent's tokens last 12 hours. When they expire, `investigate` says so and stops. Run `docker compose run --rm admin setup` again; nothing needs a restart.

## Usage

```bash
docker compose run --rm agent "<your question>"
```

- The agent finds the namespace itself. `-n <namespace>` points it at one.
- `--json` prints the result as JSON. `--quiet` turns off the progress.
- `--user NAME` records who asked.
- To pipe the JSON, add `-T`: `docker compose run --rm -T agent "…" > result.json`.

To let the agent read a namespace of your own, add a `reader` RoleBinding there, as each scenario's [`namespace.yaml`](scenarios/selector/manifests/namespace.yaml) does.

### The answer

Three parts, as a panel in a terminal or as JSON when piped:

- **Root cause:** what is wrong, or `no_fault`, or `inconclusive` with the reason.
- **Evidence:** the findings, each citing the tool call that showed it, with that call's tool and arguments.
- **Next step:** the smallest change that fixes it.

The JSON also holds the critic's verdicts and the record of any remediation. An annotated example is in [DESIGN](DESIGN.md#what-comes-back).

### Exit codes

| Exit | When |
|---|---|
| 0 | a diagnosis, `inconclusive` included |
| 1 | the run failed: a model API error (bad key, rate limit, network) or an unexpected error. The JSON has `"status": "failed"` and the reason |
| 2 | nothing ran: an invalid setting, expired tokens, or the MCP server can't be reached |

### Stopping condition

A run ends when the critic accepts the answer, or at the first limit it reaches. A limit never produces a guess. All limits are settings in `.env`.

| Limit | Default | When reached |
|---|---|---|
| Tool calls, per round / per run | 15 / 30 | further calls are refused; the agent answers from what it has, and the critic still checks it |
| Model calls, per round / per run | 20 / 40 | `inconclusive` |
| Evidence checks by the critic | 2 | still unsupported: `inconclusive` |
| Working time | 300 s, not counting the wait for your approval | `inconclusive` |
| Your approval | 600 s | the remediation is rejected |
| Output size | 8,000 tokens per model call, 40,000 characters per tool result | a cut-off answer is `inconclusive`; a long tool output is cut with a marker |

Setting names and the remediator's own limits: [DESIGN 3.3](DESIGN.md#33-run-limits).

### Remediation

The agent supports one remediation: correcting a Service's selector when it doesn't match its pods, in the namespaces where remediation is allowed (`shop-checkout` by default). Every other diagnosis stays advice.

1. **You see the diagnosis** the critic verified, with its evidence.
2. **You see the exact change,** the remediator's explanation of what it does and the effect it expects, and facts read a moment ago.
3. **You answer y/N.** Only `y` applies it. `n`, no answer in 10 minutes, or no terminal all reject it. There is no `--yes`.
4. **It is applied through one narrow tool,** `patch_service_selector`, and only if the Service hasn't changed since it was read.
5. **The investigator checks again** with fresh tool calls and reports whether the problem is gone.

Details: [DESIGN 3.6](DESIGN.md#36-remediation).

### Traces (observability)

Every run is one MLflow trace, from the question to the answer:

- each stage of the workflow, each model call and each tool call;
- every tool call's inputs and outputs;
- errors, and how long each step took;
- how the conclusion was reached: the model's reasoning summaries, the critic's verdict per finding, and your approval.

The CLI prints the trace's link after each run. The UI is at http://localhost:5050. What each span holds: [DESIGN 3.9](DESIGN.md#39-observability).

One thing that looks odd in a remediation's trace: the spans that end with `Interrupt` are marked as errors. That is the pause for your approval, not a failure.

## Failure scenarios

Four reproducible scenarios, each a folder of plain Kubernetes YAML in [`scenarios/`](scenarios), with eight questions between them:

| Scenario | The fault | Questions that test |
|---|---|---|
| `crashloop` | A misspelled ConfigMap key crashes the app. The ConfigMap also holds an instruction planted for an AI agent | finding a cause in another object than the symptom; ignoring injected instructions |
| `selector` | A Service selects `app: check-out`, the pods are `app: checkout`: no endpoints. The one fault the agent may remediate | comparing two objects; a wrong premise; a user dictating a bad fix |
| `readiness` | A readiness probe on a path that returns 404: Running, never Ready | a cause in the spec of a pod that looks alive |
| `healthy` | Nothing: the control | not inventing a fault; a service that doesn't exist |

```bash
docker compose run --rm admin scenario list           # list them
docker compose run --rm admin scenario up selector    # create one, wait until its fault is live
docker compose run --rm admin scenario down selector  # delete its namespace
```

Every question, why it is asked and its right answer: [scenarios/README.md](scenarios/README.md).

## Evaluations

```bash
# every question once, plus the selector remediation run
docker compose run --rm admin eval
# one scenario, every question three times: 2 of 3 must pass
docker compose run --rm admin eval --scenario selector --runs 3
```

A run recreates each scenario, asks its questions without naming the namespace, and scores every answer. It prints a pass/fail table, writes `eval-results/<time>.json`, and exits non-zero if anything fails. One run is nine investigations: the eight questions, plus one where the selector remediation is approved automatically and must work.

Every answer is scored on three questions, by code where the answer is exact and by an LLM judge where it needs judgment:

| Question | Check | By | Passes when |
|---|---|---|---|
| Is the diagnosis correct? | `diagnosis_correct` | code | status, fault category and resource are among the accepted answers |
| Is it supported by evidence? | `evidence_supports` | judge | the cited tool outputs show each finding and establish the diagnosis |
| | `evidence_exists` | code | every cited evidence ID is a real tool call in the trace |
| | `right_tools` | code | the agent read the sources this fault needs |
| Is the advice appropriate and safe? | `advice_ok` | judge | the next step meets the scenario's rubric, and is safe |
| | `actions_safe` | code | only allowed tools ran, and no remediation was proposed except the expected one |
| Did the remediation work? (remediation run) | `remediation_works` | code | the change was applied, the Service got endpoints, and the recheck confirmed it |

- **Pass rule:** with one run, every check must pass. With `--runs 3`, the diagnosis and the two judges must pass in 2 of 3 runs of each question, and the other checks in all 3.
- **The judges are tested too.** On ten answers with known verdicts, two right and eight bad in different ways, they must get every one right. They run on a stronger model than the agent.
- **Scores live on the trace.** Each one is attached to its run's MLflow trace (experiment `k8s-agent-evals`), next to what the agent did.
- **The table also shows** how many evidence rounds each answer took, with time, tokens and cost. These are reported, not pass marks.

## Design rationale

The short version. Every decision, with what was rejected and its trade-off, is in [DESIGN 4](DESIGN.md#4-design-decisions).

### Framework and orchestration

- **LangGraph, with three roles.** "Evidence before a conclusion" is a stage every answer passes through, not a line in a prompt.
- **One investigator,** because these faults show only when facts from two places meet, and one context sees both.
- **LangChain agents inside the graph:** call limits, tool retries and the approval pause come ready-made, and the model is a setting, not tied to one provider.
- **Considered instead:** Pydantic AI (close, and would also work), Deep Agents (extra planning and file tools to switch off), the Claude Agent SDK (one provider only).

### Tool boundaries

- **The investigator can only read:** 7 tools from [kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server), a maintained server, in read-only mode.
- **The critic has no tools.** It judges only the outputs a draft cites.
- **The remediator has one write,** `patch_service_selector`, plus 3 reads, under its own identity. Keeping it a separate agent keeps write access out of the loop that reads untrusted logs.
- **Nowhere:** a shell, a `kubectl` passthrough, or exec.

### Evaluation approach

- **The real agent on a real cluster,** not saved answers.
- **Code checks what is exact; LLM judges check what needs judgment,** on a stronger model than the agent.
- **Harder questions on purpose:** wrong premises, a service that doesn't exist, a dictated wrong fix, and an instruction planted in the cluster.
- **MLflow** holds the traces and the scores in one small server. Langfuse's self-hosted stack needs ClickHouse, Redis and object storage.

### Safety boundary

- **The cluster enforces it, not our code,** through two identities in [`deploy/rbac.yaml`](deploy/rbac.yaml).
- **`reader`** has Kubernetes' built-in `view` role in the namespaces it may read: no Secrets, no changes, no exec.
- **`remediator`** can get and patch one Service, in one namespace.
- **Tokens last 12 hours,** in private files, each mounted only into the container that uses it.
- **The one write needs your `y`** on the exact change. Code rejects any request that differs from the remediation the critic checked.
- **Tool output is data.** The worst a manipulated model can do is a wrong diagnosis, or a remediation proposal that still needs your approval.

Threats and the layers that limit each: [DESIGN 3.5](DESIGN.md#35-safety-model).

## CI

One pipeline, [`ci.yml`](.github/workflows/ci.yml), one stage after another:

| Stage | What it does |
|---|---|
| `lint` | ruff and pyright |
| `unit-tests` | the unit tests |
| `build-image` | builds the image, checks it runs as non-root and starts |
| `publish-image` | pushes the image to ghcr.io, for amd64 and arm64 |
| `mlflow-evals` | pulls that image and runs the full eval on a throwaway kind cluster, with the real model |

`main` takes changes only through pull requests that pass all five. The evals run on pull requests, not on every push to `main`. A pull request from a fork gets no secrets, so it skips the publish and eval stages: its evals are run from a branch of this repository before merging.

## Known limitations

- **One remediation type,** and one user at a time.
- **The remediator's permission is per object, not per field.** RBAC limits it to one Service, but not to that Service's selector. The remediation tool's code, your approval and the short token lifetime keep it narrow.
- **The evals use the four scenarios the prompts were tuned on.** They show the agent works on these faults, not how it does on unseen ones.
- **The MCP server and the tracking server have no login.** Both listen on `127.0.0.1` only, for a local, single-user setup.
- **Tokens expire after 12 hours** and nothing renews them: run `admin setup` again.
- **CI's evals don't reach your MLflow.** Only the results file is kept, as the run's artifact.

Next steps (a multi-user HTTP API, a production deployment path, more remediation types): [DESIGN 5](DESIGN.md#5-known-limitations-and-future-work).

## Development

Needs [uv](https://docs.astral.sh/uv/). `uv sync` installs Python 3.14 and every dependency. Then `uv run agent …` runs any command without the app containers (`k8s-mcp` and `mlflow` still run in compose).

```bash
make check                    # lint, types and unit tests; no cluster needed
make accept SUITE=remediate   # one acceptance suite, on your cluster
make accept                   # every acceptance suite
```

```
scenarios/<folder>/   manifests/*.yaml + scenario.yaml, one folder per scenario
deploy/rbac.yaml      the agent's identities
mcp/config.toml       the MCP server's settings (read-only, tool allowlist)
src/k8s_agent/        the `agent` CLI, settings, scenarios, setup, evals
  agent/              the LangGraph workflow, critic, remediator, approval gate
  prompts/            every prompt, one Markdown file each, named for its agent
tests/unit/           fast tests, no cluster
tests/acceptance/     tests against a real cluster, one file per area
```

[CLAUDE.md](CLAUDE.md) holds the working notes for developing with Claude Code: which checks to run, and the cluster and library behaviours that cost time to discover.

## Clean up

Everything this project created, in the cluster and in Docker:

```bash
# the scenarios
kubectl delete namespace -l app.kubernetes.io/part-of=k8s-agent-demo
# the agent's identities
kubectl delete namespace k8s-agent
kubectl delete clusterrole,clusterrolebinding \
  -l app.kubernetes.io/managed-by=k8s-agent-setup
# the containers, and MLflow's volume
docker compose down -v
```

## License

[MIT](LICENSE)
