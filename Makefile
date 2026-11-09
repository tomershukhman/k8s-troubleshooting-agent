.PHONY: check lint test accept

# Lint, type check (src) + unit tests. No cluster, no model, no network.
check: lint test

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

test:
	uv run pytest tests/unit -q

# Acceptance tests against the real cluster. `make accept SUITE=remediate` runs tests/acceptance/test_remediate.py
# only; `make accept` runs every suite. Run only the relevant ones (CLAUDE.md, Checks).
ACCEPT_FILES = $(if $(SUITE),$(or $(wildcard tests/acceptance/test_$(SUITE).py),$(error no acceptance suite $(SUITE))),tests/acceptance)

accept:
	uv run pytest $(ACCEPT_FILES) -v -s
