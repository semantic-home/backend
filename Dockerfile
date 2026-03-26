FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app


COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project


COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "--factory", "backend.__main__:create_app", "--host", "0.0.0.0", "--port", "8000"]