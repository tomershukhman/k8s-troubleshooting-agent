# PLAN

How we build the system described in [SPEC.md](SPEC.md): 8 milestones, most important first. Each milestone says:

- **What we build:** the result, in one or two lines
- **Why:** why it comes at this point
- **How:** the approach, high level
- **What we test:** the checks that prove it works

After M7 the assignment is fully done. M8 adds the HTTP service for several users.

| # | Milestone | Result |
|---|---|---|
| M1 | Broken scenarios | 4 broken apps you can deploy and remove with one command |
| M2 | Safe cluster access | A read-only MCP server the agent will use to look at the cluster |
| M3 | Investigator agent | Ask a question, get a diagnosis as JSON |
| M4 | Observability | See every step the agent takes, in Langfuse |
| M5 | Evals | A score for how well the agent diagnoses each scenario |
| M6 | The fix | For one scenario, the agent asks to fix it, you approve, it fixes and verifies |
| M7 | Packaged + CI | Everything runs in Docker, with CI and a README a reviewer can follow |
| M8 | HTTP service | Several people talk to the agent over an API; each conversation is a thread |

---

## Rules for every milestone

1. **Settle the open questions first.** Some milestones have things to confirm in the docs before coding (listed in [SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time-not-yet-checked)). Report anything that changes the design.
2. **Write the tests together with the code.** Each milestone adds its own tests:
   - `make check`: lint and fast unit tests. No cluster or model needed; must always pass.
   - `make accept M=<n>`: the milestone's tests against the real cluster (and the real model, from M3). Earlier milestones' tests are re-run too, to catch anything that broke.
3. **Review against the spec.** A fresh reviewer compares the changes to SPEC.md and reports missing requirements, not style.
4. **Show evidence.** The milestone report shows the exact commands and their output, then a demo.
5. **Update README.md** so it describes the system as it is now. It's a normal README, not a progress tracker.
6. **Commit only when the milestone is fully finished:** steps 1–5 done, and you've seen the demo and said OK. No partial commits.
7. **Keep the agent's logic separate from the CLI and the API,** so M8 can wrap it without rewriting it.

---

## M1 — Broken scenarios

**What we build**
Four small apps on the cluster, each broken in one known way, plus a command to deploy and remove them: `agent scenario up|down|list <name>`.

| Scenario | What's broken | What you see |
|---|---|---|
| crashloop | A ConfigMap key is misspelled, so the app can't find its database setting | The pod keeps crashing and restarting |
| selector | The Service looks for the wrong pod label | Pods are healthy, but the Service has no endpoints |
| readiness | The health check calls a URL that returns 404 | Pods run but never become Ready |
| healthy | Nothing (control case) | Everything works; the agent should say "no fault" |

**Why**
Every later milestone is tested against these scenarios, so they must exist first and be broken in exactly the expected way.

**How**
- Each scenario is a folder: plain Kubernetes YAML, plus a `scenario.yaml` holding:
  - the question to ask the agent
  - the correct answers
  - a "fault is live" condition, e.g. "the pod has restarted twice" ([SPEC 4.4, Scenario format](SPEC.md#44-scenario-format))
- `up` deploys the YAML into the scenario's own namespace and waits until the fault is live. `down` deletes the namespace.
- Also sets up the project itself: Python project, settings, `Makefile`, `CLAUDE.md`.

**What we test** (`make accept M=1`)
For each scenario it runs `up`, checks the fault, then runs `down`:
- **crashloop:** the pod keeps crashing (at least 2 restarts, exit code 1, never Ready), and its log says "DATABASE_URL not set"
- **selector:** pods are Ready, but the Service selects the wrong label and has 0 endpoints
- **readiness:** pods are Running but not Ready, and events show "Readiness probe failed… 404"
- **healthy:** pods are Ready, the Service has endpoints, and there are no warnings
- **after `down`:** the namespace is gone

---

## M2 — Safe cluster access

**What we build**
- A read-only identity for the agent on the cluster.
- The Kubernetes MCP server, running in Docker, which the agent will use for every look at the cluster.

**Why**
The agent should only ever see the cluster through this server, so its safety limits must be in place and proven before any agent code exists. It's also the first container that talks to the cluster, so it proves the networking approach early.

**How**
1. **Check the MCP server's details first:** pin its version and read its tool argument names.
2. **`agent setup`** (run once) creates two identities in a `k8s-agent` namespace:
   - **`reader`:** can read pods, logs, events, services, deployments and configmaps. It can never read Secrets or change anything.
   - **`remediator`:** can change only Services; used only by the fix in M6.

   It writes a credentials file for each to `.kube-agent/` (kept out of git).
3. **`scenario up` grants access per namespace.** reader gets access in every scenario namespace, and remediator only in `scn-selector`. This repeats on every `up`, because `down` deletes the grants together with the namespace.
4. **Run the MCP server** with `docker compose up -d k8s-mcp`. It runs with **host networking** ([SPEC 3.10, Container networking](SPEC.md#310-container-networking--host-network)): the container shares your machine's network, so the reader's kubeconfig works unchanged. Without it, `127.0.0.1` inside a container would mean the container itself. It's the official image plus two mounted files:
   - the reader's credentials
   - `mcp/config.toml`: read-only mode, only 7 allowed tools, Secrets blocked ([SPEC 3.3, Cluster tools](SPEC.md#33-cluster-tools--containerskubernetes-mcp-server))

   It listens on `localhost:8080` (configurable).

**What we test** (`make accept M=2`)
- **Through the MCP server:** a small script calls it the way the agent will.

  | Check | Expected |
  |---|---|
  | Which tools exist | Exactly the 7 allowed read tools; nothing that runs, deletes or changes |
  | List pods in `scn-crashloop` | Shows the `orders` pod, which proves the container reaches the cluster |
  | Read the crashing pod's log | Contains "DATABASE_URL not set" |
  | Read a Secret | Refused |
  | List pods in `kube-system` | Refused |

- **Directly at Kubernetes:** each identity checks its own permissions.

  | Identity | Allowed | Refused |
  |---|---|---|
  | reader | Read pods, logs, events, services and configmaps in scenario namespaces | Secrets, any change, `kube-system` |
  | remediator | Read and change Services in `scn-selector` | Services elsewhere, anything else |

- **Robustness:** after `down` and `up` of a scenario, the permissions are still right, and running `setup` twice is harmless.

---

## M3 — Investigator agent

**What we build**
The agent itself: you ask a question about a scenario and get back a diagnosis as JSON. That includes the root cause, the evidence (each item pointing to the tool call that found it), and a recommended action. It runs from a local command: `agent investigate "<question>" [--user NAME]`.

**Why**
It's the core of the assignment. Everything after it (tracing, evals, the fix, the API) builds on it.

**How**
1. **Answer the open questions** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time-not-yet-checked), items 3, 4, 5 and 7):
   - Structured output works together with tools on Sonnet 5.5, without forcing a tool call, which that model rejects.
   - Pick the LangChain MCP adapter, and confirm it runs several tool calls at once.
   - The `effort` setting reaches the model, and a refusal is handled.
   - A timeout keeps the steps recorded so far.
2. A LangChain agent (Claude Sonnet 5.5 by default) gets only the 7 MCP tools, a system prompt ([SPEC 3.5a, System prompt](SPEC.md#35a-system-prompt-short-kept-in-one-file)) and a fixed output format ([SPEC 4.2, Run JSON](SPEC.md#42-run-json)).
3. It runs with a LangGraph checkpointer (in memory for now) and a `thread_id`, and every run records its `user_id`. M6's approval and M8's conversations build on this ([SPEC 3.9, Users and threads](SPEC.md#39-users-and-threads)).
4. Every tool result is tagged with a short ID such as `E3`, so evidence can point to it; it's also size-capped and recorded.
5. **Stopping rule:** at most 15 tool calls (after that, the agent must answer with what it has), at most 20 model calls, and a 3-minute limit. Hitting either of the last two returns "inconclusive". All three are configurable.

**What we test**
- **`make check`, with a fake model** (no cost):
  - evidence IDs are added and recorded
  - large outputs are cut down
  - each stopping limit behaves as described
  - a "fault found" answer without evidence is rejected
  - no tool outside the 7 is ever given to the agent
- **`make accept M=3`, the real model on the real cluster,** once per scenario:
  - the answer is valid JSON
  - every evidence ID really exists
  - it finishes in time
  - the diagnosis matches the expected answer ([SPEC 2.2, Scenarios](SPEC.md#22-scenarios-each-in-its-own-namespace-plain-yaml-that-works-on-any-cluster)). One retry is allowed, because model answers vary.

---

## M4 — Observability

**What we build**
Langfuse (optional, in Docker) shows every agent run step by step: the question, each model call, each tool call with its input, output and timing, and the final answer.

**Why**
We need to see what the agent is doing before we can judge it. When an eval in M5 fails, the trace shows why. It's also a brief requirement.

**How**
1. **Confirm first** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time-not-yet-checked), item 6) that LangChain's Langfuse integration records model and tool calls with timing, and accepts a user ID.
2. Langfuse starts with `docker compose --profile observability up`. Its project and keys are created automatically, so no signup is needed.
3. Each run is traced and tagged with its `user_id`, `thread_id` and `run_id`.
4. If Langfuse is down, the agent still works and just logs a warning.
5. Logs are structured JSON with the same IDs.

**What we test**
- **`make accept M=4`:**
  - Run an investigation, then fetch its trace from Langfuse. It contains the model calls and tool calls with inputs, outputs and timing, tagged with the user.
  - Stop Langfuse and run again: the investigation still succeeds.
- **`make check`:** tracing is on only when Langfuse is configured, and every log line is JSON carrying the run's IDs.
- **Demo:** a screenshot of a trace in Langfuse.

---

## M5 — Evals

**What we build**
`agent eval`: runs every scenario 3 times, scores each answer, and prints a results table. The results are also saved to a file.

**Why**
It proves the agent works across repeated runs, not just once, and gives a score to compare every later change against. With M4 in place, every failing run has a trace to inspect.

**How** ([SPEC 2.3, Definition of done](SPEC.md#23-definition-of-done-evals); [SPEC 3.6, Scenarios and eval runner](SPEC.md#36-scenarios-and-eval-runner))
For each run: reset the scenario, ask the agent, score the answer, remove the scenario.
- **Scored by code:**
  - the diagnosis matches the expected answer
  - every piece of evidence points to a real tool call
  - the agent looked at the right things (each requirement can list alternative tools, e.g. the broken probe is visible on the Deployment or on the pod)
  - nothing unsafe was recommended
- **Scored by an LLM judge:** whether the recommended action is sensible, against a short checklist per scenario.

A scenario passes if the diagnosis and the judge pass in at least 2 of 3 runs, and the evidence and safety checks pass every time.

**What we test**
- **`make check`:** each scoring rule is tested with hand-made good and bad answers. For example, a wrong category must fail and a made-up evidence ID must fail.
- **The eval can fail:** run it against a fake agent that always answers "no fault". The eval must report failures, which proves it can actually catch a bad agent.
- **`make accept M=5`:** one full run (4 scenarios × 3) passes, and its results are kept for the README.

---

## M6 — The fix

**What we build**
For the selector scenario, the agent asks to fix the problem, you see the change and approve it, the fix is applied, and the agent checks again and confirms the problem is gone. Approval is a y/N prompt in the terminal.

**Why**
The assignment requires one approved fix. It builds on the working agent and its checkpointer.

**How** ([SPEC 3.4, Remediation](SPEC.md#34-remediation--one-narrow-tool-behind-human-in-the-loop))
1. **Confirm first** ([SPEC 6, To verify at build time](SPEC.md#6-to-verify-at-build-time-not-yet-checked), items 8 and 9):
   - Kubernetes refuses a change to a Service that was modified in the meantime.
   - LangChain's human-in-the-loop works together with the structured final answer.
2. **One new tool:** `patch_service_selector`. It's the only tool that can change anything, and it changes one field, a Service's selector. It runs with the `remediator` identity (Services only).
3. **LangChain's `HumanInTheLoopMiddleware`** pauses the run before that tool executes. The only choices are approve or reject; the change itself can't be edited at approval.
4. **At the pause, the CLI shows** the change, the agent's explanation, and facts checked by code: "selector app=check-out → app=checkout; 2 ready pods will start receiving traffic".
5. **Approve:** the run resumes on the same thread. The tool checks again that the change is valid, applies it, and waits for the Service to get endpoints. Then the agent re-checks with fresh evidence and reports "verified" or "not verified".
6. **Reject:** nothing changes, and the agent finishes its answer without the fix.
7. Evals: in normal diagnosis runs any fix request is rejected and recorded (it must only happen in the selector scenario); a separate remediation run approves it.

**What we test**
- **`make check`, with a fake model:**
  - the run pauses before the fix tool runs, and nothing runs on reject
  - only approve or reject are accepted
  - the tool refuses a namespace outside the allowed list, and a selector that matches no pods
  - a Service changed after the pause is not overwritten (reported as "stale")
- **`make accept M=6`:**
  - The agent requests exactly the right change.
  - After approval, the Service gets endpoints and the result is "verified", recording who approved.
  - After a reject, the Service is unchanged.
  - Edit the Service while the run is paused, then approve: the fix must refuse to apply.
- The eval's score for the selector scenario now includes the fix.

---

## M7 — Packaged + CI

**What we build**
Everything runs with Docker: the agent, the MCP server and optional Langfuse. A CI pipeline checks every change. A complete README a reviewer can follow from scratch.

**Why**
After this milestone, every requirement of the assignment is met, including CI, which the brief requires.

**How**
- **One app image:** small, runs as a non-root user, and contains no secrets.
- **Docker Compose** starts things in the right order, using health checks. Containers that talk to the cluster use host networking ([SPEC 3.10, Container networking](SPEC.md#310-container-networking--host-network)).
- **The reviewer needs only Docker,** a cluster and an API key; every README command is a `docker compose run …`. The README says how to turn on host networking in Docker Desktop (one setting; it's built in on OrbStack and Linux).
- **Clear errors:** a missing API key, an unreachable cluster or a stopped MCP server each give one clear message, not a stack trace.
- **CI (GitHub Actions):**
  - **On every push and pull request**, with no cluster and no API key: lint and unit tests (`make check`), the image build, and eval validation (scenario files are valid, and the scoring tests pass).
  - **A manual button** runs the real evals on a throwaway kind cluster. It needs your Anthropic key stored as a GitHub secret, so it never runs on its own.
- **README:** setup, running the agent, the scenarios, the evals, and the design reasoning (framework, tool limits, eval approach, safety).

**What we test**
- **`make accept M=7`:**
  - The image builds, runs as non-root, and contains no secrets.
  - Containers start in order.
  - Each failure case gives a clear message.
- **CI:** the push workflow passes on GitHub. A deliberately broken unit test makes it fail, which proves it catches problems. The manual workflow is run once.
- **Fresh-clone test:** a helper that only reads the README sets everything up from scratch on a new kind cluster. Any step it has to guess is a README bug. kind needs installing first, with your OK.
- **Final check:** every requirement in the assignment maps to working code or a README section.

---

## M8 — HTTP service

**What we build**
An API service (FastAPI) so several people can talk to the agent:
- each conversation is a **thread**, and the agent remembers what was said in it
- each message says **who** is talking (`user_id`)
- the agent's runs happen in the background, several at once
- an approval is just the next step in the thread, recording who approved

The CLI becomes a client of this API, and can continue a thread.

**Why**
You want several people to use the agent and the agent to know who it's talking to. It comes last because it wraps the finished agent without changing it.

**How** ([SPEC 4.1, HTTP API](SPEC.md#41-http-api-fastapi-openapi-docs-at-docs-m8); [SPEC 3.8, State](SPEC.md#38-state--postgres-m8); [SPEC 3.9, Users and threads](SPEC.md#39-users-and-threads))
- **Postgres** holds the threads, using LangGraph's own Postgres checkpointer, plus a `runs` table with each run's record.
- **Messages and approvals** are sent to a thread; a run paused for approval waits there until someone approves or rejects.
- **No login:** `user_id` is self-declared, which is fine for a local tool. Single sign-on is the production path, described in the README.

**What we test**
- **`make check`, API tests against a real Postgres** (with a fake model and cluster):
  - a follow-up message on a thread sees the earlier conversation
  - a paused run resumes on approve and stops on reject, and records who decided
  - deciding when nothing is pending is refused
  - two threads run at the same time without mixing up
  - after a restart, threads and paused approvals are still there
- **`make accept M=8`, the full flow:**
  - alice asks about the selector scenario; the run pauses for approval
  - bob approves on the same thread; the result is "verified", with bob recorded
  - alice asks a follow-up ("what did you change?") and the agent answers from the thread
  - the trace shows the right user for each message
- **No regressions:** every earlier milestone's tests and the evals still pass.

---

## Later
- **Sub-agents:** revisit if the agent's scope grows ([SPEC 3.1, Agent topology](SPEC.md#31-agent-topology--one-investigator-orchestration-in-code)).
- **Production path:** login (single sign-on), a job queue for several API servers, more fix types.
