# Single image: the CLI and, later, the dashboard. Built from the lock file only.
FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 paperfill
USER paperfill
ENTRYPOINT ["paperfill"]
CMD ["version"]
