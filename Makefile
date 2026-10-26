.PHONY: dev verify

# Run the server on 127.0.0.1:8000.
dev:
	uv run aiverse-server

# Lint, format check, type check and tests.
verify:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest
