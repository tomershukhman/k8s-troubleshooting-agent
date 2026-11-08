# PLAN

How we build the system described in [SPEC.md](SPEC.md): 8 milestones, most important first. Each milestone says:

- **What we build:** the result, in one or two lines
- **Why:** why it comes at this point
- **How:** the approach, high level
- **What we test:** the checks that prove it works

**M1–M7 deliver exactly the assignment** (M7 is tagged `assignment-complete`). M8 is a clearly labelled extra beyond the brief ([SPEC 3.11, Scope](SPEC.md#311-scope-the-brief-first-then-one-labelled-extra)).

| # | Milestone | Result |
|---|---|---|
| M1 | Broken scenarios | 4 broken apps you can deploy and remove with one command |
| M2 | Safe cluster access | A read-only MCP server, beside the cluster, that the agent uses to look at it |
| M3 | Investigator agent | Ask a question, get a diagnosis as JSON |
| M4 | Observability | See every step the agent takes, in Langfuse |
| M5 | Evals | A score for how well the agent diagnoses each scenario |
| M6 | The fix | For one scenario, the agent asks to fix it, you approve, it fixes and verifies |
| M7 | Packaged + CI | Everything runs in Docker, with CI and a README a reviewer can follow |
| M8 | Extra: HTTP service | Several people talk to the agent over an API; each conversation is a thread |

The design follows one principle: **the cluster is the system under investigation; the agent and its tools run beside it** (see the top of [SPEC.md](SPEC.md)).

---

## Rules for every milestone

1. **Settle the open questions first.** Some milestones have things to confirm in the docs before coding (listed in [SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time)). Report anything that changes the design.
2. **Write the tests together with the code.** Each milestone adds its own tests:
   - `make check`: lint and fast unit tests. No cluster, no model, no network; must always pass, and never reads your real `.env`.
   - `make accept M=<n>`: acceptance tests for milestones 1 to n against the real cluster (and the real model, from M3), so nothing that worked earlier silently breaks. Each test cleans up after itself, even when it fails.
3. **Tests check behaviour, not files.** A test that only re-reads a config file proves nothing; test what the running system does.
4. **Review against the spec.** A fresh reviewer compares the changes to SPEC.md and reports missing requirements, not style.
5. **Show evidence.** The milestone report shows the exact commands and their output, then a demo.
6. **Update README.md** so it describes the system as it is now. It's a normal README, not a progress tracker.
7. **Commit only when the milestone is fully finished:** steps 1–6 done, and you've seen the demo and said OK. No partial commits.
8. **Keep the agent's logic separate from the CLI** (and from the M8 API), and keep M1–M7 free of M8 work.

---

## M1 — Broken scenarios ✅ (needs the neutral-names change, done in M2)

**What we build**
Four small apps on the cluster, each broken in one known way, plus `agent scenario up|down|list <name>`.

| Folder | Namespace | What's broken | What you see |
|---|---|---|---|
| crashloop | `shop-orders` | A ConfigMap key is misspelled, so the app can't find its database setting | The pod keeps crashing and restarting |
| selector | `shop-checkout` | The Service looks for the wrong pod label | Pods are healthy, but the Service has no endpoints |
| readiness | `shop-catalog` | The health check calls a URL that returns 404 | Pods run but never become Ready |
| healthy | `shop-inventory` | Nothing (control case) | Everything works; the agent should say "no fault" |

**Why**
Every later milestone is tested against these scenarios, so they must exist first and be broken in exactly the expected way.

**How**
- Each scenario is a folder: plain Kubernetes YAML, plus a `scenario.yaml` with the question, the correct answers, and a "fault is live" condition ([SPEC 4.4, Scenario format](SPEC.md#44-scenario-format)).
- **Namespaces and labels never hint at the fault** ([SPEC 2.2, Scenarios](SPEC.md#22-scenarios)): neutral names like `shop-checkout`, and the ownership label `app.kubernetes.io/part-of=k8s-agent-demo`. The folder name is never shown to the agent.
- `up` recreates the namespace, applies the YAML and waits until the fault is live. `down` deletes the namespace.

**What we test** (`make accept M=1`)
For each scenario: `up`, check the fault, `down`:
- **crashloop:** the pod keeps crashing (≥2 restarts, exit code 1, never Ready), and its log says "DATABASE_URL not set"
- **selector:** pods are Ready, but the Service selects the wrong label and has 0 endpoints
- **readiness:** pods Running but not Ready; events show "Readiness probe failed… 404"
- **healthy:** pods Ready, the Service has endpoints, no warnings
- **after `down`:** the namespace is gone
- **no hints:** no namespace or label the agent can see contains the folder name

---

## M2 — Safe cluster access

**What we build**
- The agent's two identities on the cluster: a read-only one, and one that can only fix and verify.
- The Kubernetes MCP server, running as a Docker container **beside the cluster** as the read-only identity. The agent will use it for every look at the cluster.
- The M1 changes from the review: neutral namespace names and labels.

**Why**
The agent should only ever see the cluster through this server, so its safety limits must be in place and proven before any agent code exists. It runs beside the cluster, not in it, so the diagnostic tool doesn't depend on the health of the thing it diagnoses, and it works the same on kind, minikube or OrbStack ([SPEC 3.3, Cluster tools](SPEC.md#33-cluster-tools--containerskubernetes-mcp-server-beside-the-cluster)).

**How**
1. **`agent setup`** (run once) creates two identities in a `k8s-agent` namespace ([SPEC 3.5, Safety boundary](SPEC.md#35-safety-boundary-cluster-enforced)):
   - **`reader`:** reads pods, logs, events, services, EndpointSlices, deployments and configmaps in the scenario namespaces. Never Secrets, never any change.
   - **`remediator`:** what fixing and verifying need, nothing more: read and patch Services, read pods and EndpointSlices, only in `shop-checkout`. Used by the fix in M6.

   Each gets a short-lived token, checked with a "who am I" call, and written to `.kube-agent/` (kept out of git).
2. **`scenario up` grants access per namespace,** on every `up`, because `down` deletes the grants with the namespace.
3. **The MCP server** runs with `docker compose up -d k8s-mcp`: the official image pinned by digest, host networking so the kubeconfig works unchanged ([SPEC 3.10, Networking](SPEC.md#310-networking)), listening only on `127.0.0.1:8080`. It gets two read-only files: the reader's credentials, and `mcp/config.toml` (read-only mode, 7 allowed tools, Secrets blocked).
4. **Neutral names:** scenario namespaces become `shop-*`, set in each `scenario.yaml` instead of derived from the folder name, with a neutral ownership label.

**What we test** (`make accept M=2`, which also re-runs M1)
- **Through the MCP server,** with the same MCP client library the agent will use (`langchain[mcp]`):

  | Check | Expected |
  |---|---|
  | Which tools exist | Exactly the 7 allowed read tools |
  | List pods in `shop-orders` | Shows the `orders` pod |
  | Read the crashing pod's log | Contains "DATABASE_URL not set" |
  | Read a planted Secret | Refused, and its value never appears in any output |
  | List pods in `kube-system` | Refused |
  | List namespaces | Shows no name or label that hints at a fault |

- **Directly at Kubernetes:** each identity asks Kubernetes what it may do.

  | Identity | Allowed | Refused |
  |---|---|---|
  | reader | Read pods, logs, events, services, EndpointSlices, configmaps in scenario namespaces | Secrets, any change, `kube-system` |
  | remediator | Read and patch Services; read pods and EndpointSlices; in `shop-checkout` only | Anything in other namespaces; deleting; Secrets; other resource types |

- **Robustness:** permissions are right after `down` + `up`; `setup` twice is harmless; a refreshed token is picked up without restarting the server.
- **`make check`:** unit tests for what `setup` generates (roles, bindings, kubeconfigs), with a fake cluster.

---

## M3 — Investigator agent

**What we build**
The agent: ask a question, get back a diagnosis as JSON, with the root cause (or "no fault", or "couldn't tell"), evidence linked to the tool calls that found it, and a recommended action. Runs from `agent investigate "<question>" [-n namespace] [--user NAME] [--quiet]`, showing its progress live as it works.

**Why**
It's the core of the assignment. Everything after it builds on it.

**How**
1. **Confirm first** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time), items 3, 4, 5 and 7): the final structured answer after the tool loop is valid on the real model; `langchain[mcp]` handles parallel calls on one connection; the async tool hook and step streaming work; reasoning summaries pass through; refusals are detectable; the timeout keeps partial steps.
2. **The agent:** LangChain `create_agent` with Claude Sonnet 5.5, only the 7 MCP tools, a system prompt ([SPEC 3.5a, System prompt](SPEC.md#35a-system-prompt-short-kept-in-one-file)), and the output format set explicitly as `response_format=ProviderStrategy(Diagnosis)` ([SPEC 4.2, Run JSON](SPEC.md#42-run-json)). In-memory checkpointer, a `thread_id`, and the `user_id` recorded.
3. **Tool output** ([SPEC 3.3, Cluster tools](SPEC.md#33-cluster-tools--containerskubernetes-mcp-server-beside-the-cluster)): strip noise (`managedFields`), apply a 40,000-character safety cap that normal outputs never reach, tag each result with an evidence ID such as `E3`, record the step. Both values are in config.
4. **Three answers, all with evidence:** `fault_found`, `no_fault` (also needs evidence), or `inconclusive` with a reason (e.g. access refused).
5. **Stopping rule:** max 15 tool calls (then it must conclude), max 20 model calls and 180 s (then `inconclusive`, also when the limit leaves no final answer), and a per-call output cap. Together they bound tokens. All configurable.
6. **Namespace is optional.** Without `-n`, the agent finds it; the evals never pass it.
7. **Output:** live progress lines on stderr (each tool call as it happens, plus short reasoning summaries; off with `--quiet`); the result JSON on stdout, including the tool calls its evidence cites. No run files: the core function returns the full run record in memory for the evals ([SPEC 3.7, Observability](SPEC.md#37-observability)).
8. **Failures handled cleanly, and logged** (structured JSON logs tagged with run, thread and user IDs):
   - MCP server unreachable → one clear message before any model call, non-zero exit
   - model API error → `failed` result with the reason
   - refusal or no final answer → `inconclusive`

**What we test**
- **`make check`, with a fake model and fake tools** (no cost):
  - the output format is set to native structured output (`ProviderStrategy`)
  - tool output: `managedFields` removed while `spec` and `status` stay; output under the cap unchanged; output over the cap cut at exactly the limit with the marker; both settings read from config
  - evidence IDs added and recorded; the result JSON includes the cited steps
  - each stopping limit behaves as described, including a run that ends with no final answer → `inconclusive`
  - `fault_found` or `no_fault` without evidence is rejected; inconsistent answers (e.g. `no_fault` with a fault category) are rejected
  - no tool outside the 7 is ever loaded
  - each failure case gives the documented result and exit code, and is logged as one JSON line with the IDs
  - progress lines go to stderr, the result to stdout; `--quiet` silences progress
- **`make accept M=3`, the real model on the real cluster** (also re-runs M1 and M2):
  - once per scenario, asking the scenario's question **without a namespace**: valid JSON, every evidence ID exists, finished in time, diagnosis matches the expected answer ([SPEC 2.2, Scenarios](SPEC.md#22-scenarios)), the right namespace found. One retry allowed, because model answers vary.
  - a real `resources_get` of a Deployment reaches the agent with `status` present and no `managedFields`
  - asking about a namespace the agent can't access returns `inconclusive` with a reason, not a guess
  - with the MCP server stopped, the CLI fails with the clear message, not a traceback
- **Demo:** watch the progress lines as the agent investigates the selector scenario, then the final JSON.

---

## M4 — Observability

**What we build**
Langfuse (optional, in Docker) shows every agent run step by step: the question, each model call with the model's reasoning summary, each tool call with input, output and timing, and the final answer.

**Why**
We need to see what the agent does before we can judge it; when an eval fails in M5, the trace shows why. The brief requires it, including "how the final conclusion was reached".

**How**
1. **Confirm first** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time), item 6): the Langfuse callback records model and tool calls with timing, user ID and reasoning summaries.
2. `docker compose --profile observability up` starts Langfuse; project and keys are created automatically.
3. Each run is traced, tagged with `user_id`, `thread_id` and `run_id`, the same IDs as the logs from M3.
4. If Langfuse is down, the agent still works and logs a warning.

**What we test**
- **`make accept M=4`:** run an investigation, fetch its trace from Langfuse: it holds the model calls (with reasoning summaries) and tool calls with inputs, outputs and timing, tagged with the user. Stop Langfuse and run again: still succeeds.
- **`make check`:** tracing only when configured.
- **Demo:** a screenshot of a trace.

---

## M5 — Evals

**What we build**
`agent eval`: runs every scenario 3 times, scores each answer, prints a results table and saves it to a file.

**Why**
It proves the agent works across repeated runs, and gives a score to compare every later change against. Every failing run has a trace from M4.

**How** ([SPEC 2.3, Definition of done](SPEC.md#23-definition-of-done-evals); [SPEC 3.6, Scenarios and eval runner](SPEC.md#36-scenarios-and-eval-runner))
For each run: reset the scenario, ask the agent, score, remove the scenario.
- **Code:** diagnosis against the accepted answers; cited evidence exists; the agent looked at the right things (alternatives allowed); the agent's actions were safe (only allowed tools; no fix request outside the selector scenario).
- **LLM judge:**
  - does the cited evidence actually support the conclusion (it sees the cited tool outputs)?
  - is the recommended action appropriate and safe as advice to a human operator?
- Save the run records of one real eval run as test fixtures, for CI.

**What we test**
- **`make check`:** each scoring rule with hand-made good and bad answers; the recorded real runs re-scored with the same results.
- **The eval can fail:**
  - a fake agent that always answers "no fault" must fail
  - the judge must fail a correct conclusion backed by irrelevant citations
  - the judge must fail a destructive recommendation ("delete the namespace and redeploy")
- **`make accept M=5`:** one full run (4 scenarios × 3) passes; results kept for the README.

---

## M6 — The fix

**What we build**
For the selector scenario: the agent asks to fix the problem, you see the exact change and approve it, the fix is applied, and the agent checks again and confirms the problem is gone. Approval is a y/N prompt in the terminal.

**Why**
The assignment requires one approved fix through a narrowly scoped tool, verified with fresh evidence.

**How** ([SPEC 3.4, Remediation](SPEC.md#34-remediation--one-narrow-tool-behind-human-in-the-loop))
1. **Confirm first** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time), items 8 and 9): the JSON Patch with a version test is atomic and rejected when the Service changed; human-in-the-loop works with the structured final answer.
2. **One new tool,** `patch_service_selector`, using only the `remediator` credentials (it fails rather than falling back to any other kubeconfig).
3. **LangChain's `HumanInTheLoopMiddleware`** pauses the run before the tool executes; only approve or reject.
4. **At the pause the CLI shows** the change, the agent's explanation, and facts checked by code: "selector app=check-out → app=checkout; 2 ready pods will match".
5. **Approve:** the tool re-validates, applies a JSON Patch that checks the Service's version and replaces the selector exactly, and waits for endpoints. The agent then re-checks with fresh evidence and fills `post_fix_check`. "Verified" needs both.
6. **Reject:** nothing changes; the agent finishes without the fix.
7. **Evals:** diagnosis runs reject any fix request and record it; a separate remediation run approves.

**What we test**
- **`make check`, with a fake model:**
  - the run pauses before the fix tool, and nothing runs on reject
  - only approve or reject are accepted
  - the tool refuses a namespace outside the allowed list and a selector that matches no pods
  - the exact selector is applied, extra old keys don't survive
  - a version change after the pause gives "stale" and nothing applied
  - the tool never uses any kubeconfig but the remediator's
- **`make accept M=6`:** the agent requests exactly the right change; after approval the Service gets endpoints and the result is "verified", recording who approved; after a reject the Service is unchanged; editing the Service during the pause makes the fix refuse.
- The eval's selector score now includes the fix.

---

## M7 — Packaged + CI (assignment complete)

**What we build**
Everything runs with Docker; CI checks every change; a complete README a reviewer can follow from scratch. Tagged `assignment-complete`.

**Why**
After this, every requirement in the brief is met.

**How**
- **One app image:** small, non-root, no secrets.
- **Compose services,** each with only the credentials it needs ([SPEC 3.5, Safety boundary](SPEC.md#35-safety-boundary-cluster-enforced)):
  - `agent`: runs `investigate`; holds the remediator credentials only
  - `admin`: runs `setup`, `scenario` and `eval`; holds your admin kubeconfig
  - `k8s-mcp`: holds the reader credentials only
  - optional Langfuse
- **Host networking** for containers that talk to the cluster ([SPEC 3.10, Networking](SPEC.md#310-networking)); health checks so containers start in order.
- **The reviewer needs only Docker,** a local cluster and an API key. Every README command is a `docker compose run …`. The README's main example uses kind (as the brief suggests), and notes the one Docker Desktop setting.
- **Clear errors:** a missing API key, an unreachable cluster or a stopped MCP server each give one clear message.
- **CI (GitHub Actions):**
  - **every push and PR,** no cluster and no API key: `make check`, the image build, and eval validation (scenario files valid, recorded runs re-scored)
  - **a manual button:** real evals on a throwaway kind cluster on GitHub's runner (`helm/kind-action`), using your Anthropic key from a GitHub secret
- **README:** setup, running the agent, the scenarios, the evals with results, and the design rationale: the guiding principle, framework, tool boundaries, eval approach, safety boundary and its known limit, sub-agents as the path to scale.

**What we test**
- **`make accept M=7`:**
  - the image builds, runs as non-root, contains no secrets
  - containers start in order
  - each failure case gives a clear message
  - the `agent` container has no admin kubeconfig mounted
- **CI:** the push workflow passes on GitHub; a deliberately broken unit test makes it fail; the manual workflow is run once and passes.
- **Fresh-clone test:** a helper that only reads the README sets everything up from scratch on a new kind cluster. Any step it has to guess is a README bug. (kind needs installing first, with your OK.)
- **Final check:** every requirement in the brief maps to working code or a README section.

---

## M8 — Extra: HTTP service

**What we build**
An API service (FastAPI) so several people can talk to the agent: each conversation is a thread the agent remembers, each message says who is talking, runs happen in the background, and an approval is the next step in the thread. The CLI becomes its client. Presented in the README as "Beyond the brief".

**Why**
You want several people to use the agent and the agent to know who it's talking to. It comes last because it wraps the finished agent without changing it.

**How** ([SPEC 4.1, HTTP API](SPEC.md#41-http-api-m8-extra-fastapi-openapi-docs-at-docs); [SPEC 3.8, State](SPEC.md#38-state); [SPEC 3.9, Users and threads](SPEC.md#39-users-and-threads))
- **Postgres** (a container beside the cluster) holds the threads, through LangGraph's Postgres checkpointer, and a `runs` table.
- **Messages and decisions** go to a thread; a run paused for approval waits there.
- **No login:** `user_id` is self-declared. SSO is the production path.

**What we test**
- **`make check`, API tests against a real Postgres** (fake model and cluster): a follow-up sees the earlier conversation; a paused run resumes on approve and stops on reject, recording who decided; deciding with nothing pending is refused; two threads run at once without mixing; threads and paused approvals survive a restart.
- **`make accept M=8`:** alice asks about the selector scenario and the run pauses; bob approves on the same thread and the result is "verified" with bob recorded; alice asks a follow-up and the agent answers from the thread; traces show the right user.
- **No regressions:** all earlier acceptance tests and the evals still pass.

---

## Later
- **Sub-agents:** revisit if the agent's scope grows ([SPEC 3.1, Agent topology](SPEC.md#31-agent-topology--one-investigator-orchestration-in-code)).
- **Production path:** the MCP server in the cluster behind authentication, login (SSO), a job queue for several API servers, more fix types with per-fix permissions and admission policies.
