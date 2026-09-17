# syntax=docker/dockerfile:1.7
# Runtime image for the Thymira workspace (the `thymira` CLI entry point; the API server is
# started with `thymira-api` once apps/api lands).
# Build: docker build -t thymira:dev .
# Run:   docker compose run --rm thymira --help   (see compose.yaml for the mounted directories)

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Third-party dependencies only — this layer is reused until a pyproject.toml or uv.lock
# change. The member pyprojects must be visible for uv to validate the workspace lockfile.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=runtime,target=runtime \
    --mount=type=bind,source=apps,target=apps \
    --mount=type=bind,source=adapters,target=adapters \
    uv sync --locked --all-packages --no-install-workspace --no-dev --no-editable

# 2) The workspace members themselves (non-editable, so the venv is self-contained).
COPY pyproject.toml uv.lock README.md ./
COPY packages/ ./packages/
COPY runtime/ ./runtime/
COPY apps/ ./apps/
COPY adapters/ ./adapters/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 999 thymira \
    && useradd --system --gid 999 --uid 999 --create-home thymira \
    && mkdir -p /app/runs /app/data /app/examples \
    && chown -R thymira:thymira /app
WORKDIR /app
COPY --from=builder --chown=thymira:thymira /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER thymira
ENTRYPOINT ["thymira"]
CMD ["--help"]
