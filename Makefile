# CI entry points: `make lint test` on every change; `make build` to pack the charm.

.PHONY: test lint build clean

test:
	uv run pytest
	cd charm && uv run --group unit pytest tests/unit

lint:
	uv run ruff check .
	uv run ruff format --check cli tests charm/payload testbed/nt-testbed

build:
	cd charm && charmcraft pack

clean:
	rm -f charm/*.charm
	rm -rf .pytest_cache .ruff_cache charm/.pytest_cache
	find . -type d -name __pycache__ -not -path './.venv/*' -not -path './charm/.venv/*' -exec rm -rf {} +
