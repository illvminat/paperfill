.PHONY: sync lint fmt test check sources-check docker

sync:
	uv sync --all-groups --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/paperfill

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest --cov --cov-report=term-missing -m "not live"

check: lint test sources-check

sources-check:
	uv run python scripts/sources_check.py

docker:
	docker build -t paperfill:local .
	docker run --rm paperfill:local version
