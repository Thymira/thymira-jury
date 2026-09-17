# uv multi-stage Docker patterns (Thymira)

Reference for the root `Dockerfile`, `compose.yaml` and `.dockerignore`. Read the SKILL.md rules
first; this file shows the mechanics and the exact commands.

## The repository Dockerfile, annotated

```dockerfile
# syntax=docker/dockerfile:1.7          # enables RUN --mount (cache + bind mounts)
ARG PYTHON_VERSION=3.13

# ---- builder: resolves and installs into /app/.venv ----
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/   # uv is pinned, not "latest"

ENV UV_COMPILE_BYTECODE=1 \      # ship .pyc so the CLI starts faster
    UV_LINK_MODE=copy \          # copy out of the cache mount (no cross-device hardlinks)
    UV_PYTHON_DOWNLOADS=0 \      # use the image's interpreter, never fetch one
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# 1) Dependencies only. Cached until uv.lock / a manifest changes, NOT on a src/ edit.
RUN --mount=type=cache,target=/root/.cache/uv \        # uv's download/build cache persists
    --mount=type=bind,source=uv.lock,target=uv.lock \  # bind = present during this RUN, not a layer
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=runtime,target=runtime \
    --mount=type=bind,source=apps,target=apps \
    --mount=type=bind,source=adapters,target=adapters \
    uv sync --locked --all-packages --no-install-workspace --no-dev --no-editable

# 2) The project itself, non-editable so the venv is self-contained.
COPY pyproject.toml uv.lock README.md ./
COPY packages/ ./packages/
COPY runtime/ ./runtime/
COPY apps/ ./apps/
COPY adapters/ ./adapters/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --no-dev --no-editable

# ---- runtime: only the finished venv, non-root ----
FROM python:${PYTHON_VERSION}-slim AS runtime
RUN groupadd --system --gid 999 thymira \
    && useradd --system --gid 999 --uid 999 --create-home thymira \
    && mkdir -p /app/runs /app/data /app/examples \
    && chown -R thymira:thymira /app
WORKDIR /app
COPY --from=builder --chown=thymira:thymira /app/.venv /app/.venv   # nothing else crosses the stage
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER thymira
ENTRYPOINT ["thymira"]
CMD ["--help"]
```

Why it does not "rebuild everything on a code change": step 1 is `--no-install-workspace` and runs
before the members are copied, so editing `runtime/**` invalidates only the small step-2 workspace sync,
not the dependency resolve. What *does* over-invalidate today: step 1 binds the whole `packages/`,
`runtime/`, `apps/`, `adapters/` trees, so editing any `thymira.*` member's source rebuilds the
dependency layer. Only each member's `pyproject.toml` + `README.md` are actually needed there (uv
reads them to validate `uv.lock`; each manifest's `readme` field points at its own `README.md`).
Narrowing the step-1 binds to those manifests removes the extra invalidation; keep whatever you
bind in step with `[tool.uv.workspace].members` in the root `pyproject.toml`.

## Cache mounts vs bind mounts vs COPY

- `--mount=type=cache,target=/root/.cache/uv` — a persistent build cache shared across builds; its
  contents never land in the image. This is what makes a re-resolve fast without shipping anything.
- `--mount=type=bind,source=…,target=…` — makes a build-context file readable *during that RUN
  only*; it does not create an image layer, so a manifest read here leaves no trace in the image.
- `COPY` — creates an image layer. Order COPYs cheapest-changing first (manifests) to costliest
  (`src/`) so an edit invalidates as few layers as possible.

## Adding a system package (builder stage only)

Native wheels sometimes need a compiler or headers. Install them in the *builder*, never the
runtime, so the shipped image stays slim:

```dockerfile
# in the builder stage, before the first uv sync
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
```

If a package is needed at run time (a shared `.so`, not just to build), install the runtime library
(e.g. `libgomp1`) in the runtime stage instead — add only the specific library, not `-dev` headers.

## Debugging the image

```bash
docker run --rm --entrypoint sh thymira:dev        # shell in the runtime image (entrypoint is `thymira`)
docker run --rm --entrypoint sh thymira:dev -c 'ls -la /app/.venv/bin && du -sh /app/.venv'
docker image ls thymira:dev                        # size
docker history thymira:dev                          # per-layer size — find the heavy layer
docker build --progress=plain -t thymira:dev .      # full build log when a step fails
```

On Windows Git Bash, a manual `docker run -v "$PWD/data:/app/data:ro" …` can be silently dropped:
Git Bash rewrites the container-side path and the mount vanishes with no error. Prefer
`docker compose run` (it reads volumes from `compose.yaml`, immune to this) or prefix the command
with `MSYS_NO_PATHCONV=1`.

## Image-size checklist

1. Which layer grew? `docker history thymira:dev` attributes size; a dependency such as
   LiteLLM's or MLflow's transitive tree usually dwarfs the base and roughly triples the image
   (~0.9 GB → ~2.3 GB). That is the usual cause of a "surprisingly large" image.
2. Is the runtime stage copying only `/app/.venv` from the builder — nothing else?
3. Are `.venv`, `runs/`, `data/`, `tests/`, `docs/`, `.git` in `.dockerignore`? Confirm with
   `docker build --progress=plain` and watch the "transferring context" size.
4. Is `--no-editable` on the final sync (no path back-references to source)?
5. Did an `apt-get` layer forget `rm -rf /var/lib/apt/lists/*`?
6. Use `docker history thymira:dev` to attribute size to a layer before changing anything.

## Adding a service image for a workspace member

The MVP roadmap adds `docker compose up` (Thymira API + an MLflow server) under
`infrastructure/docker/`; run persistence is local JSON/JSONL (ADR-0010), so no database service
is added in the MVP, and PostgreSQL is FINAL work behind its own ADR. Give each member its own
image using the same two-stage shape, but sync
just that package so the image carries only its dependency closure:

```dockerfile
# infrastructure/docker/api.Dockerfile — image for the thymira-api member
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# --package selects one workspace member; --no-install-project keeps deps in their own layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    # + every member manifest, as in the root image
    uv sync --package thymira-api --locked --no-install-project --no-dev --no-editable
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --package thymira-api --locked --no-dev --no-editable

FROM python:3.13-slim AS runtime
# groupadd/useradd non-root, then:
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER app
CMD ["uvicorn", "thymira.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Keep the existing root image untouched; add the new image and its service to a compose file under
`infrastructure/docker/`, not to the root `compose.yaml`, which stays the `thymira` CLI's compose.
