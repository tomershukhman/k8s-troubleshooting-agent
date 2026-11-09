.PHONY: check lint test accept

# Lint, type check (src) + unit tests. No cluster, no model, no network.
check: lint test

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

test:
	uv run pytest tests/unit -q

# Acceptance tests against the real cluster. `make accept M=3` runs milestone 3's tests only;
# `make accept` runs every milestone's. Run only the relevant ones (CLAUDE.md, Checks).
ACCEPT_FILES = $(if $(M),$(or $(wildcard tests/acceptance/test_m$(M)_*.py),$(error no acceptance tests for milestone $(M))),tests/acceptance)

accept:
	uv run pytest $(ACCEPT_FILES) -v -s
