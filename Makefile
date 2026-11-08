.PHONY: check accept

# Lint + unit tests. No cluster, no model, no network.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest tests/unit -q

# Acceptance tests against the real cluster. `make accept M=2` runs milestones 1 to 2, so
# nothing that worked earlier silently breaks; `make accept` runs all of them.
ACCEPT_FILES = $(if $(M),$(foreach i,$(shell seq 1 $(M)),$(wildcard tests/acceptance/test_m$(i)_*.py)),tests/acceptance)

accept:
	uv run pytest $(ACCEPT_FILES) -v -s
