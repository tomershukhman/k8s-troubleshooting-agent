# k8s-agent

Design: `DESIGN.md`.

## Checks
- `make check` = `make lint` (ruff lint + format check + pyright, standard mode, `src` only) +
  `make test` (unit tests; no cluster, no LLM); CI runs them as separate stages. Keep green. Narrow a Kubernetes field the API server always sets with
  `kube.present()`; a `# pyright: ignore[rule]` only where a library's annotation is wrong, with
  the reason beside it.
- `make accept SUITE=<name>`: one acceptance suite (`tests/acceptance/test_<name>.py`: scenarios,
  access, agent, tracing, evals, remediate, containers), against the real cluster; `make accept` runs all.
- **Run only the relevant tests.** Before committing: `make check` plus the acceptance suites whose
  area the change touches. Skip `scenarios` unless a scenario manifest changed (it takes ~2 min).
- Acceptance suites other than `scenarios` keep running scenarios, re-applying their manifests so the cluster matches them
  (recreated only if missing, a roleRef changed, or the fault isn't live), and leaves them up.
  `AGENT_ACCEPT_REUSE_SCENARIOS=false` = fresh and removed (needed after deleting an object from a
  manifest). The `scenarios` suite always deploys and deletes: that's its test.
- One test: `uv run pytest tests/unit/test_agent.py::test_founded_evidence_passes_straight_through -q`
- **Tuning prompts (`src/k8s_agent/prompts/`), with Claude Code reading the answers itself:** `uv run agent scenario list` for the
  questions (each scenario's `also_ask` has more), then `uv run agent investigate "<question>" --json` per scenario (agent only, ~$0.09 a
  run), or `uv run agent eval --runs 1` (adds the judges): `eval-results/<time>.json` holds each run's
  full record (every tool call and output, the critic's verdicts) beside every score and judge reason.
- Run from the repo root (`AGENT_SCENARIOS_DIR` defaults to `./scenarios`). Unit tests run in an empty
  temp dir with AGENT_* cleared and a kubeconfig that doesn't exist (`tests/unit/conftest.py`), so
  they never read `.env` or reach a cluster.

## Cluster / env gotchas
- Acceptance tests use `AGENT_KUBE_CONTEXT`, else the kubeconfig's current context. Dev cluster
  here is OrbStack: `AGENT_KUBE_CONTEXT=orbstack make accept SUITE=scenarios`.
- Scenario namespaces are neutral (`shop-*`, in each scenario's `manifests/namespace.yaml`, with the
  agent's RoleBindings); the folder name describes the fault and must never reach the cluster (a unit
  test checks names/labels). `scenario up|down` refuse a namespace without
  `app.kubernetes.io/part-of=k8s-agent-demo`. The identities are in `deploy/rbac.yaml`.
- Count Service endpoints via EndpointSlices; the v1 Endpoints API is deprecated (warns on 1.33+).
- Images in scenario manifests must be pinned by digest (a unit test checks).
- `make accept SUITE=scenarios` takes ~2 min: crashloop needs 2 restarts (10s + 20s back-off).
- `make accept SUITE=access` needs Docker: it runs `agent setup` and starts `k8s-mcp` if it isn't running.
- Credential files under `.kube-agent/` must be rewritten in place (`access.write_private`): k8s-mcp
  bind-mounts the single file, so a replaced file (new inode) leaves it on the old token. The server
  watches the file and reloads; no restart after `agent setup`.
- The API server caches token checks for ~10s, so a revoked token keeps working briefly.
- MCP client is `langchain[mcp]` (`MCPAdapter`, beta). Invoke a tool with a ToolCall dict to get a
  ToolMessage whose `status == "error"` marks a tool error. With fastmcp 4.1.6, exceptions cross
  `async with MCPAdapter(...)` unwrapped (a failed connect is a `RuntimeError`; checked 2026-11-08).
- A tool error carries no code, only the API server's text: refused / not found are recognised by
  `tool_output.REFUSED` / `PERMANENT`. The loaded MCP tools have `handle_tool_error=False`, so an
  error raises (the trace marks the span ERROR); `ToolRetryMiddleware(retry_on=worth_retrying)`
  retries all but PERMANENT, and `ToolErrorMiddleware` gives the model the text. The `agent` acceptance
  suite checks the real server's wording.
- On OrbStack, `pods_log` with `previous: true` on a crash-looping pod usually returns "unable to
  retrieve container logs for docker://…" as a *successful* result (the old container's log isn't
  kept); `previous: false` returns the last crash's log. Checked 2026-11-08.
- langchain-anthropic shares one async HTTP client per process (`lru_cache`); its pooled
  connections belong to the event loop that opened them. Calling the agent under several
  `asyncio.run()` in one process (tests, evals) can fail with "Event loop is closed": use one
  loop (`asyncio.Runner`), as tests/acceptance/test_agent.py does.
- Compose `configs: content:` can't be used on a `read_only: true` service (why k8s-mcp isn't read_only).
- MLflow (slim `mlflow-tracing` 3.18): MLflow's HTTP defaults (7 retries, 120 s) hang a call to a
  stopped server for ~4 min; `tracing.enable()` sets `MLFLOW_HTTP_REQUEST_MAX_RETRIES=0` and
  `MLFLOW_HTTP_REQUEST_TIMEOUT` from settings (`MLFLOW_ASYNC_TRACE_LOGGING_RETRY_TIMEOUT` only affects
  Databricks). Without an active experiment the slim package silently gives a `NoOpSpan`
  (`default_experiment_registry` NameError, logged at debug), so `set_experiment` must run first.
- The tracking server is the compose service `mlflow` (official image), at `http://localhost:5050`
  (port 5050: AirPlay often holds 5000); `docker compose up -d --wait k8s-mcp mlflow`. It used to
  run in the cluster via Helm; an old OrbStack setup may still hold a scaled-down `mlflow` release.
- `make accept SUITE=tracing` needs `MLFLOW_TRACKING_URI`. Acceptance runs trace to `k8s-agent-tests` (tests/acceptance/conftest.py), not `k8s-agent`. Read traces with `mlflow.get_trace` + `Trace.search_spans`; spans arrive one by one.
- A LangGraph conditional edge given a lambda makes MLflow's autolog record a nameless span, and the
  upload batch it sits in is lost (the trace then won't load: "not fully exported"). Route with a
  named function or `Command(goto=...)`; a unit test traces a fix run and fails if it recurs.
- `make accept SUITE=remediate` changes the selector Service and rebuilds the scenario (~20 s per test that does).
- Containers: one app image (`Dockerfile`, uv's single-stage example) for `agent` (only
  `investigate`; the remediator kubeconfig only) and `admin` (setup, scenario, eval; your
  kubeconfig). `docker compose build` is slow on OrbStack (~5–10 min when the dependency layer
  changes, mostly exporting ~16k files); code-only changes reuse it. `make accept SUITE=containers` rebuilds the
  image itself. Settings reach containers only through `.env` (or `docker compose run -e`), not your
  shell. Pipe `--json` with `docker compose run -T`: a TTY merges stderr into stdout.
- The image has no `.git`: CI stamps the commit (`GIT_COMMIT` build arg → `AGENT_GIT_COMMIT`); a
  local `docker compose build` records none. MLflow's "Failed to import Git" warnings (no git binary)
  are silenced in `evals.configure` via its logger; `GIT_PYTHON_REFRESH=quiet` doesn't reach them.
- CI (`.github/workflows/ci.yml`): lint → unit-tests → build-image → publish-image → mlflow-evals; the
  evals (PRs to main + the manual button) pull the image just published, by commit SHA. Don't run
  the evals locally to check CI changes: dispatch the workflow (`gh workflow run ci.yml --ref <branch>`).
- The y/N approval prompt needs a real terminal (stdin a TTY): drive demos through a pty. Tests that
  run `agent investigate` as a subprocess need `stdin=DEVNULL`, or a terminal makes them wait for y/N.
