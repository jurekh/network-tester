# CI entry points: `make lint test` on every change; `make build` to pack the charm.

.PHONY: test lint build clean sync-shared

# cli/ holds the canonical copies of the modules shared with the charm
# payload (which cannot import installed packages on nodes). Edit the cli
# copy, then refresh the payload copy here; tests/test_cross_implementation.py
# fails the build if the copies drift.
SHARED_MODULES = schemas.py representatives.py

sync-shared:
	cd cli && cp $(SHARED_MODULES) ../charm/payload/

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
