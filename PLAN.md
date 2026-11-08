# PLAN — build order

Builds what [SPEC.md](SPEC.md) describes, one milestone at a time, most important first. Each milestone says **what** we build, **how** (high level), and **how we check it works**. **After M7 the brief is fully met**; M8 adds the multi-user service.

---

## How every milestone is checked

Following [Claude Code best practices](https://code.claude.com/docs/en/best-practices): give the builder checks that return pass/fail, and require evidence instead of "it works".

1. **`make check`**: lint (ruff) plus unit tests (pytest, no cluster, no LLM). It must stay green after every milestone.
2. **`make accept M=<n>`**: that milestone's acceptance tests (`tests/acceptance/test_m<n>_*.py`), run against the real cluster (and the real model, from M3 on). They stay in the repo, so later milestones re-run the earlier ones (no regressions).

**The tests are part of each milestone's build.** The builder writes the feature *and* the tests that turn its "How we check it works" list into code. M1 starts with no tests and ends with its own; each later milestone adds its own.
3. **Spec review:** at the end of each milestone, a fresh subagent reviews the diff against SPEC.md: "check every requirement for this milestone is implemented, report gaps that affect correctness, skip style".
4. **Evidence in the milestone report:** the exact commands run and their output (and for M5, a Langfuse screenshot). A summary alone doesn't count.
5. **The user reviews the demo, then gives an OK to commit.** The next milestone starts only after that.

Also:
- Before writing code that depends on them, work through the milestone's **verify first** items (SPEC §6) against current docs, and report anything that forces a spec change.
- `CLAUDE.md` (created in M1) stays short: the check commands, how to run one test, and the cluster/env gotchas found while building.
- The core logic (agent, remediation, evals) never imports the interface (CLI or API), so M8 can wrap it without rework.

---

## M1 — Broken scenarios on the cluster
**Why first:** every later milestone is tested against these.

**What we build**
- The project skeleton: uv project on Python 3.14, settings module, `.env.example`, `Makefile` (`check`, `accept`), `CLAUDE.md`.
- 4 scenarios, each in its own namespace: `scn-crashloop`, `scn-selector`, `scn-readiness`, `scn-healthy` (SPEC §2.2).
- `agent scenario up|down|list <name>`.
- **The tests for this milestone:** `tests/acceptance/test_m1_scenarios.py`, which automates the checks listed under "How we check it works" below, plus small unit tests for reading `scenario.yaml`.

**How**
- Each scenario is a folder of plain Kubernetes YAML (small public images pinned by digest) plus `scenario.yaml`, which holds the question, expected answers, required tools, judge rubric and the `ready_when` condition (SPEC §4.4).
- `up` applies the YAML with the official `kubernetes` Python client using your own kubeconfig, then polls until `ready_when` is true (e.g. the crashloop pod has restarted ≥2 times). `down` deletes the namespace and waits until it's gone.

**How we check it works**
- `make accept M=1` runs `up`, asserts the fault, then `down`, for each scenario:
  - **crashloop:** pod in `CrashLoopBackOff`, restarts ≥2, previous log contains "DATABASE_URL not set"
  - **selector:** pods Ready, Service has 0 endpoints
  - **readiness:** pods Running but not Ready, an event contains "Readiness probe failed" and "404"
  - **healthy:** pods Ready, Service has ready endpoints, no warning events
  - **after `down`:** the namespace no longer exists
- `make check` is green (unit tests for `scenario.yaml` parsing and validation).
- **Demo:** `agent scenario up selector`, then `kubectl get pods,endpoints -n scn-selector` shows it live.

---

## M2 — Safe read access: service accounts + MCP server
**Why now:** it holds the biggest technical unknowns (containers reaching the cluster, MCP behavior) and the core safety boundary everything else relies on.

**What we build**
- `agent setup`: the two service accounts and their kubeconfigs (SPEC §3.5).
- The `k8s-mcp` service in `docker-compose.yml`, with a committed `config.toml` (SPEC §3.3).

**How**
- **Verify first:** SPEC §6.1 (which API address containers can reach, with valid TLS) and §6.2 (pin the MCP release, check its tool arguments).
- `setup` uses your admin kubeconfig to create the long-lived pieces (SPEC §3.5):
  - the `k8s-agent` namespace with the `reader` and `remediator` service accounts
  - the three ClusterRoles (read, list namespaces, patch services)
  - short-lived tokens
  - two kubeconfigs in `.kube-agent/` that use the container-reachable address
- **`agent scenario up` (from M1) is extended to create the RoleBindings** inside each scenario namespace: reader everywhere, remediator only in `scn-selector`. Deleting a namespace deletes its bindings, so they must come back with every `up`.
- The MCP server runs in read-only mode with the tool allowlist and Secrets denied, using the reader kubeconfig. Its port is published on localhost, so the agent can reach it both from the host (M3–M6) and from compose (M7+).

**How we check it works**
- `make accept M=2`:
  - **MCP smoke test** (a Python MCP client against the running container): `pods_list_in_namespace` and `pods_log(previous=true)` work in `scn-crashloop`; a Secret read is refused; the tool list contains exactly the 7 allowlisted tools.
  - **Permission tests** (the `SelfSubjectAccessReview` API, the same check as `kubectl auth can-i`):
    - reader can `get pods` in `scn-crashloop`
    - reader **cannot** `get secrets`, `patch services`, or read in `kube-system`
    - remediator **can** `get` and `patch services` in `scn-selector`, and **cannot** patch them in any other namespace or touch any other resource
  - **Recreate:** after `scenario down selector` then `up selector`, both accounts still have exactly those permissions (the bindings came back).
  - **Re-run:** `setup` runs a second time without errors.
- **Demo:** `docker compose up k8s-mcp`, then the smoke script prints pods from `scn-crashloop`, and a Secret read fails.

---

## M3 — Investigator agent (local)
**Why now:** it's the heart of the brief.

**What we build**
- The core `investigate(question, namespace) → Investigation` (diagnosis + recorded steps).
- A temporary dev command, `agent investigate "<question>" --local`, that prints the JSON. M8 replaces it.

**How**
- **Verify first:** SPEC §6.3 (structured output with tools on Sonnet 5.5, no forced tool choice), §6.4 (MCP adapter, parallel tool calls), §6.5 (effort, refusals), §6.7 (timeout).
- LangChain `create_agent` with the MCP tools (allowlisted again on our side), the system prompt (SPEC §3.5a) and the Pydantic output schema (SPEC §4.2).
- Middleware around every tool result adds the `[E<n>]` evidence ID, caps the size and records the step. Built-in limits handle model and tool calls; a timeout wrapper produces `inconclusive`.
- The agent runs on the host in this milestone and reaches the MCP server at its published port (`AGENT_MCP_URL=http://localhost:8080/mcp` in `.env`).

**How we check it works**
- `make check`: unit tests with a **fake model** (no API cost):
  - the evidence-ID middleware numbers results and records steps
  - outputs over the cap are truncated
  - the tool-call limit makes the agent conclude, while the model-call limit and timeout give `inconclusive`
  - the schema rejects `fault_found` with no evidence
  - tools outside the allowlist are never loaded
- `make accept M=3`: for each scenario, a real run against the cluster.
  - **Every run must:** return schema-valid JSON, cite only `E<n>` refs that exist in its steps, and finish within `AGENT_TIMEOUT_SECONDS`.
  - **The diagnosis must match** the accepted `status`, `fault_category` and resource (SPEC §2.2). One retry is allowed, because model output varies; M4 measures this properly over 3 runs. A scenario that fails twice fails the milestone.
- **Demo:** `agent investigate "The checkout service isn't reachable from other pods. Why?" --local` prints the diagnosis, with evidence pointing at the Service selector and pod labels.

---

## M4 — Evals
**Why now:** it proves the agent works, and every later change is measured against it.

**What we build**
- `agent eval [--scenario name] [--runs N]` with a results file and a summary table (SPEC §2.3, §3.6).

**How**
- For each scenario × N runs: recreate the scenario → wait for `ready_when` → `investigate()` → score → tear down.
- Code checks: valid output; diagnosis within the accepted sets; evidence refs exist and the required tool calls were made; safety (allowlist only, no writes, expected `proposed_fix`, no destructive keywords).
- An LLM judge (`AGENT_JUDGE_MODEL`) grades `recommended_action` against the per-scenario rubric.
- Results go to `eval-results/<timestamp>.json`. The command exits non-zero if thresholds fail.

**How we check it works**
- `make check`: unit tests for every scoring function with hand-made good and bad results. For example, a wrong category fails diagnosis, an unknown ref fails evidence, and a `proposed_fix` on scenario 1 fails safety. The judge is tested with a fake model.
- **Planted failure:** run the eval against a deliberately broken agent (a fake that always answers `no_fault`). The eval must report failures and exit non-zero, which proves the eval can actually fail.
- `make accept M=4`: one full run of `agent eval` (4 scenarios × 3) passes the thresholds, and its results file is kept for the README.
- **Demo:** the summary table, scenario by scenario.

---

## M5 — Observability: Langfuse
**Why now:** it's a brief requirement, and it makes M6–M8 easier to debug.

**What we build**
- Langfuse in the optional `observability` compose profile.
- Tracing for every investigation.
- Structured JSON logs.

**How**
- **Verify first:** SPEC §6.6 (the callback captures `create_agent` model and tool spans with latency, and accepts a user ID).
- Headless setup creates the project and keys from `.env`, so no signup is needed.
- LangChain's Langfuse callback is attached to each run and tagged with the investigation ID (and user ID after M8).
- Tracing is best-effort: if Langfuse is down, the run continues with a warning.

**How we check it works**
- `make accept M=5`:
  - Run one investigation, then fetch the trace through Langfuse's public API. It must contain the request, ≥1 model span, ≥1 tool span with input, output and latency, and the final result.
  - Stop Langfuse and run again: the investigation still succeeds and a warning is logged.
- `make check`:
  - a unit test that the callback is attached only when Langfuse settings are present
  - a unit test that every log line is valid JSON carrying the investigation ID
- **Demo:** a screenshot of the trace tree in the Langfuse UI: request → agent → model calls → tool calls → result.

---

## M6 — Remediation for scenario 2
**Why now:** it's a required brief item, built on the working agent.

**What we build**
- The fix flow for scenario 2: validate → explain → approve → apply → verify (SPEC §3.4).
- A temporary local approval prompt (y/N). M8 replaces it with the API.

**How**
- **Verify first:** SPEC §6.8 (a patch carrying an old `resourceVersion` is rejected with 409).
- Core functions:
  - `validate_fix()`: type, allowed namespace, Service exists, new selector matches running pods
  - `explain_fix()`: before and after selector, expected endpoints
  - `apply_fix()`: `patch_service_selector` with the remediator kubeconfig and `resourceVersion`
  - `verify_fix()`: wait for endpoints, then a read-only re-investigation
- The approver is a parameter, ready for M8.

**How we check it works**
- `make check`: unit tests:
  - validation rejects a wrong type, a namespace outside the allowlist, and a selector matching no pods
  - the explanation text is correct
  - a stale `resourceVersion` gives `stale` and no change is applied
- `make accept M=6` on a fresh `scn-selector`:
  - the agent proposes `{app: checkout}`
  - approval is simulated
  - the Service gets ≥1 ready endpoint
  - the status ends `verified`, with fresh evidence from the verify run
  - a **change-in-between test:** edit the Service after the proposal, then approve; the result must be `stale` and the Service unchanged
- `agent eval --scenario selector` includes the remediation check, and it passes.
- **Demo:** the local flow end to end: the explanation shown, y, then `verified`, with `kubectl get endpoints` showing the pods.

---

## M7 — Containerized, documented, submittable
**Why now:** with this, every brief requirement is met.

**What we build**
- The app Dockerfile.
- The full compose setup: the agent, `k8s-mcp`, and the optional Langfuse profile.
- README.md.

**How**
- **Dockerfile:** slim image, non-root user, dependencies from the uv lockfile.
- **Compose:** healthchecks on `k8s-mcp` (and Langfuse), with the agent depending on them, so containers start in the right order.
- **The reviewer needs only Docker.** Every README command is a `docker compose run` command. `make` and `uv` are developer tools only.
- **Admin commands in a container:** `setup` and `scenario` run in the container with your kubeconfig mounted read-only. They need the same container-reachable cluster address that M2 solved.
- **Config:** env-based only, with no secrets baked into the image.
- **Clean errors:** missing API key, cluster unreachable or MCP server down each give one clear message and a non-zero exit.
- **README:** setup, running the agent, reproducing scenarios and running evals, plus the design rationale (framework, tool boundaries, eval approach with the M4 results, safety boundary, sub-agents as the path to scale, the production path).

**How we check it works**
- `make accept M=7`:
  - **Image:** builds, runs as non-root, and contains no `.env` or kubeconfig files (inspect the image).
  - **Startup:** `docker compose up` brings `k8s-mcp` to healthy, and the agent container starts only after it.
  - **Failure modes:** missing `ANTHROPIC_API_KEY`, a wrong cluster address and the MCP server stopped each exit non-zero with a clear message, not a traceback.
- **Fresh-clone test**, done by a subagent that only gets the README: clone into an empty folder, create a new kind cluster, follow the README literally. Every scenario, an investigation, the fix and an eval run must work. Any step it had to guess is a README bug, and gets fixed. **kind isn't installed on this Mac yet,** so installing it needs your OK first.
- **Final spec review:** every brief requirement (PDF sections 1–8 and the deliverables list) maps to working code or a README section.

---

## M8 — Multi-user service: FastAPI + Postgres
**Why last:** it's the user's requirement beyond the brief, and it wraps the finished core.

**What we build**
- The API service (SPEC §4.1).
- Postgres storage (SPEC §4.5).
- Per-user tokens, roles and four-eyes approval (SPEC §3.9).
- The thin CLI client, replacing the temporary `--local` flows.

**How**
- **Database:** Postgres 18 in compose, with SQLAlchemy models and an Alembic migration run on startup. Steps and results move from memory to the database.
- **Auth:** API tokens checked against hashed entries in `AGENT_USERS`.
- **Background runs:** investigations run as background asyncio tasks with a concurrency cap. On startup, interrupted runs are recovered.
- **Approval:**
  - the fix proposal is stored as a row
  - approve and reject are endpoints, with four-eyes, expiry and a race-safe single update
  - the steps endpoint, `/healthz` and `/readyz` round it out
- **CLI:** `investigate`, `show`, `list`, `approve` and `reject` call the API.

**How we check it works**
- `make check`: API tests against a real Postgres (testcontainers, so from here on `make check` needs Docker running), with the model and cluster faked:
  - **Auth:** a missing or wrong token gets 401.
  - **Roles:** a user without the approver role can't approve (403), and the requester can't approve their own fix even as an approver (403).
  - **Single approval:** two approvals at once give exactly one success and one 409.
  - **Expiry:** an expired proposal gets 410.
  - **Recovery:** after a restart, `running` investigations become `failed`.
  - **Readiness:** `/readyz` fails when the database is down.
- `make accept M=8`, the end-to-end flow with the real model and cluster, using the demo users (alice and bob are approvers, carol is an investigator only):
  - alice investigates `scn-selector` through the CLI
  - alice's own approval is refused (four-eyes)
  - carol's approval is refused (wrong role)
  - bob approves
  - the status ends `verified` with `decided_by: bob`
  - two investigations started together both complete
  - the audit table holds every transition
  - with Langfuse on, the trace carries alice's user ID
- **No regressions:** `make accept` for M1–M7 and `agent eval` still pass.
- **Demo:** two terminals, alice and bob, showing the approval flow.

---

## Later (not in this plan)
- **CI:** `make check`, the image build and the eval checks on every PR; live evals on kind on manual trigger (deferred by the user).
- **Sub-agents:** revisit if scope grows (SPEC §3.1).
- **Production path:** a job queue for multiple replicas, SSO, more fix types.
