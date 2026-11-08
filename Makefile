.PHONY: check accept

# Lint + unit tests. No cluster, no model, no network.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest tests/unit -q

# Acceptance tests against the real cluster. `make accept M=3` runs milestone 3's tests only;
# `make accept` runs every milestone's, so nothing that worked earlier silently breaks (run it
# before committing a milestone).
ACCEPT_FILES = $(if $(M),$(or $(wildcard tests/acceptance/test_m$(M)_*.py),$(error no acceptance tests for milestone $(M))),tests/acceptance)

accept:
	uv run pytest $(ACCEPT_FILES) -v -s
