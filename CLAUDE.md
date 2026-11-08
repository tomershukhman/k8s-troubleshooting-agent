# k8s-agent

Design: `SPEC.md`. Build order and per-milestone checks: `PLAN.md`.

## Checks
- `make check`: ruff lint + format check + unit tests (no cluster, no LLM). Keep green.
- `make accept M=<n>`: acceptance tests for milestones 1..n against the real cluster; `make accept` runs all.
- One test: `uv run pytest tests/unit/test_scenario_spec.py::test_copy_is_valid -q`
- Run from the repo root (`AGENT_SCENARIOS_DIR` defaults to `./scenarios`). Unit tests run in an empty
  temp dir with AGENT_* cleared (`tests/unit/conftest.py`), so they never read `.env`.

## Cluster / env gotchas
- Acceptance tests use `AGENT_KUBE_CONTEXT`, else the kubeconfig's current context. Dev cluster
  here is OrbStack: `AGENT_KUBE_CONTEXT=orbstack make accept M=1`.
- Scenario namespaces are neutral (`shop-*`, set in each `scenario.yaml`); the folder name describes
  the fault and must never reach the cluster (the loader rejects names/labels/annotations containing
  it). `scenario up|down` refuse a namespace without `app.kubernetes.io/part-of=k8s-agent-demo`.
- Count Service endpoints via EndpointSlices; the v1 Endpoints API is deprecated (warns on 1.33+).
- Images in scenario manifests must be pinned by digest (the loader rejects tags).
- `make accept M=1` takes ~2 min: crashloop needs 2 restarts (10s + 20s back-off).
- `make accept M=2` needs Docker: it runs `agent setup` and starts `k8s-mcp` if it isn't running.
- Credential files under `.kube-agent/` must be rewritten in place (`access.write_private`): k8s-mcp
  bind-mounts the single file, so a replaced file (new inode) leaves it on the old token. The server
  watches the file and reloads; no restart after `agent setup`.
- The API server caches token checks for ~10s, so a revoked token keeps working briefly.
- MCP client is `langchain[mcp]` (`MCPAdapter`, beta). Invoke a tool with a ToolCall dict to get a
  ToolMessage whose `status == "error"` marks a tool error; an unknown tool raises `MCPError`, which
  must be caught inside the `async with` (outside it arrives wrapped in an ExceptionGroup).
- Compose `configs: content:` can't be used on a `read_only: true` service (why k8s-mcp isn't read_only).
