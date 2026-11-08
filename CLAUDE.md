# k8s-agent

Design: `SPEC.md`. Build order and per-milestone checks: `PLAN.md`.

## Checks
- `make check`: ruff lint + format check + unit tests (no cluster, no LLM). Keep green.
- `make accept M=<n>`: milestone n's acceptance tests against the real cluster; `make accept` runs all.
- One test: `uv run pytest tests/unit/test_scenario_spec.py::test_copy_is_valid -q`
- Run from the repo root (`AGENT_SCENARIOS_DIR` defaults to `./scenarios`).

## Cluster / env gotchas
- Acceptance tests use `AGENT_KUBE_CONTEXT`, else the kubeconfig's current context. Dev cluster
  here is OrbStack: `AGENT_KUBE_CONTEXT=orbstack make accept M=1`.
- `scenario up|down` refuse a `scn-*` namespace they didn't create (label
  `app.kubernetes.io/managed-by=k8s-agent-scenarios`).
- Count Service endpoints via EndpointSlices; the v1 Endpoints API is deprecated (warns on 1.33+).
- Images in scenario manifests must be pinned by digest (the loader rejects tags).
- `make accept M=1` takes ~2 min: crashloop needs 2 restarts (10s + 20s back-off).
