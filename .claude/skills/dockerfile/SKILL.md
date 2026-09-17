---
name: dockerfile
description: Use when building, editing or debugging the Dockerfile, compose.yaml, .dockerignore or a service image under infrastructure/docker, when the image is too large or rebuilds from scratch on every code change, when the container cannot see runs/ or data/, or when adding a system dependency to an image.
metadata:
  version: "1.0.0"
---

# Container image (uv multi-stage)

One multi-stage `Dockerfile` at the repo root builds the Thymira workspace image (`thymira:dev`,
entrypoint `thymira`): a builder installs every member's dependencies from the frozen `uv.lock`,
then a slim runtime stage copies only the finished `/app/.venv` and runs as a non-root user.
`compose.yaml` mounts `runs/`, `data/` and `examples/` at run time, so nothing host-specific is
baked in.

## When to use

- Building, editing or debugging the `Dockerfile`, `compose.yaml` or `.dockerignore`.
- The image is too large, rebuilds too slowly, or ships files it should not.
- The container cannot see `runs/`, `data/` or `examples/`.
- Adding a system package to the image, or a service image (API, MLflow) under
  `infrastructure/docker/`.
- Not for: dependency or `pyproject.toml`/`uv.lock` changes (packaging-scaffolding), or the
  `docker` recipes themselves (task-runner).

## Quick start

```bash
just docker-build                 # build thymira:dev from uv.lock
just docker-run --help            # run the CLI with runs/data/examples mounted
docker image ls thymira:dev       # size
```

`just docker-run …` is `docker compose run --rm thymira …`; compose supplies the volumes and `.env`.

## Rules

1. Two stages: the builder installs deps, the runtime stage copies only `/app/.venv` — never a
   single-stage image, so the compiler, `uv` and build caches never ship.
2. Dependencies layer before source: mount `uv.lock`, `pyproject.toml`, `README.md` and the
   member directories, run `uv sync --locked --all-packages --no-install-workspace`, and only
   then `COPY` the members; a code edit re-runs the cheap workspace layer, not the dependency
   sync.
3. The member directories are mounted because uv validates `uv.lock` against every member's
   `pyproject.toml`. As members grow, narrow each mount to its `pyproject.toml` + `README.md`
   so source edits stop invalidating the dependency layer.
4. Always `--locked`; the build must fail on a drifted lockfile, never silently re-resolve.
5. The final sync is `--no-editable` and the runtime copies only `/app/.venv`, so the venv is
   self-contained and no source is needed at run time.
6. Keep `.venv`, `runs/`, `data/`, `tests/` and `docs/` in `.dockerignore`; a host `.venv` in the
   context clobbers the image's Linux venv and bloats the build.
7. Run as the non-root `thymira` user; pin `uv` (`ghcr.io/astral-sh/uv:0.12.5`) and the base tag
   (`python:3.13-slim`; digest-pin for releases) so a build is reproducible.
8. `runs/`, `data/`, `examples/` are volumes, never baked in; run through `just docker-run`
   (compose mounts them) — a bare `docker run` mounts nothing, and that is not a Dockerfile bug.
9. Heavy optional stacks (an MLflow server) are separate services under
   `infrastructure/docker/`, not layers of this image.

## Workflow

1. Diagnose by reading: the layer order says what invalidates the cache; `docker history`
   says which layer is big.
2. "Container can't see data/": do not touch the Dockerfile — run `just docker-run <args>`; edit
   `compose.yaml` only if a genuinely needed mount is absent.
3. Edit the `Dockerfile` keeping the dependency layer above the member `COPY`s; add system
   packages with `apt-get` in the builder stage only.
4. Verify with `just docker-build`, `just docker-run --help` and `docker image ls thymira:dev`.
   `runs/` is mounted read-write, so use a throwaway output directory for real runs.

See `references/uv-docker-patterns.md` for the annotated Dockerfile, cache mounts, service-image
patterns and the image-size checklist.

## In this repository

- Files: `Dockerfile`, `compose.yaml`, `.dockerignore` at the root; recipes in the `justfile`
  `docker` group; `infrastructure/docker/` is the reserved home for service images (e.g. the API
  and an MLflow server), empty for now — no database service in the MVP (ADR-0010); PostgreSQL is
  FINAL work behind its own ADR.
- Workspace: the root is virtual; the 11 `thymira.*` members under `packages/`, `runtime/`,
  `apps/api`, `adapters/cli` are all installed (`--all-packages`) with their runtime
  dependencies only (`--no-dev`).
- Volumes and `.env` come from `compose.yaml` (`runs/` read-write, `data/` and `examples/`
  read-only). Versions checked: 2026-08.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "The container can't see data/, so the Dockerfile/compose is broken." | A bare `docker run` has no volumes; `just docker-run` already mounts runs/data/examples. Fix the command, not the files. |
| "The image is huge, so the base image needs slimming." | `docker history thymira:dev` first: the heavy layer is almost always a dependency (LiteLLM's transitive tree, MLflow), not the base. |
| "I'll COPY data/ in so the container always has it." | Data is host-specific and large; it stays a mounted volume and in `.dockerignore`. |

**Red flags — stop:** a `COPY data/` or `COPY runs/`; source copied before the dependency sync;
`uv sync` without `--locked`; an editable install, a single stage, or `USER root` at run time.

## Related skills

- packaging-scaffolding — the dependencies and `uv.lock` the image installs.
- task-runner — the `just docker-*` recipes and the `{{ARGS}}` pattern.
