.PHONY: check accept

# Lint + unit tests. No cluster, no LLM.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest tests/unit -q

# Acceptance tests against the real cluster. `make accept M=1` runs one milestone;
# `make accept` runs all of them.
accept:
	uv run pytest $(if $(M),tests/acceptance/test_m$(M)_*.py,tests/acceptance) -v -s
