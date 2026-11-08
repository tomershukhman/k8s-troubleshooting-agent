# k8s-agent

Design: `SPEC.md`. Build order and per-milestone checks: `PLAN.md`.

## Checks
- `make check`: ruff lint + format check + unit tests (no cluster, no LLM). Keep green.
- `make accept M=<n>`: milestone n's acceptance tests only, against the real cluster; `make accept`
  runs every milestone's (do that before committing a milestone).
- M2/M3 acceptance keeps running scenarios (recreated only if missing, a roleRef changed, or the
  fault isn't live) and leaves them up. `AGENT_ACCEPT_REUSE_SCENARIOS=false` = fresh and removed
  (needed after deleting an object from a manifest). M1 always deploys and deletes: that's its test.
- One test: `uv run pytest tests/unit/test_agent.py::test_founded_evidence_passes_straight_through -q`
- Run from the repo root (`AGENT_SCENARIOS_DIR` defaults to `./scenarios`). Unit tests run in an empty
  temp dir with AGENT_* cleared and a kubeconfig that doesn't exist (`tests/unit/conftest.py`), so
  they never read `.env` or reach a cluster.

## Cluster / env gotchas
- Acceptance tests use `AGENT_KUBE_CONTEXT`, else the kubeconfig's current context. Dev cluster
  here is OrbStack: `AGENT_KUBE_CONTEXT=orbstack make accept M=1`.
- Scenario namespaces are neutral (`shop-*`, in each scenario's `manifests/namespace.yaml`, with the
  agent's RoleBindings); the folder name describes the fault and must never reach the cluster (a unit
  test checks names/labels). `scenario up|down` refuse a namespace without
  `app.kubernetes.io/part-of=k8s-agent-demo`. The identities are in `deploy/rbac.yaml`.
- Count Service endpoints via EndpointSlices; the v1 Endpoints API is deprecated (warns on 1.33+).
- Images in scenario manifests must be pinned by digest (a unit test checks).
- `make accept M=1` takes ~2 min: crashloop needs 2 restarts (10s + 20s back-off).
- `make accept M=2` needs Docker: it runs `agent setup` and starts `k8s-mcp` if it isn't running.
- Credential files under `.kube-agent/` must be rewritten in place (`access.write_private`): k8s-mcp
  bind-mounts the single file, so a replaced file (new inode) leaves it on the old token. The server
  watches the file and reloads; no restart after `agent setup`.
- The API server caches token checks for ~10s, so a revoked token keeps working briefly.
- MCP client is `langchain[mcp]` (`MCPAdapter`, beta). Invoke a tool with a ToolCall dict to get a
  ToolMessage whose `status == "error"` marks a tool error. With fastmcp 4.1.6, exceptions cross
  `async with MCPAdapter(...)` unwrapped (a failed connect is a `RuntimeError`; checked 2026-11-08).
- A tool error carries no code, only the API server's text: refused / not found are recognised by
  `tool_output.REFUSED` / `PERMANENT` (not retried); `ToolRetryMiddleware` only retries exceptions,
  so `RetryToolErrors` raises for the rest. The M3 acceptance test checks the real server's wording.
- On OrbStack, `pods_log` with `previous: true` on a crash-looping pod usually returns "unable to
  retrieve container logs for docker://…" as a *successful* result (the old container's log isn't
  kept); `previous: false` returns the last crash's log. Checked 2026-11-08.
- langchain-anthropic shares one async HTTP client per process (`lru_cache`); its pooled
  connections belong to the event loop that opened them. Calling the agent under several
  `asyncio.run()` in one process (tests, evals) can fail with "Event loop is closed": use one
  loop (`asyncio.Runner`), as tests/acceptance/test_m3_agent.py does.
- Compose `configs: content:` can't be used on a `read_only: true` service (why k8s-mcp isn't read_only).
