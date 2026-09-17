# Thymira task runner. This file is canonical; the Makefile forwards to it.
# Every recipe line is a plain command that works in bash and PowerShell; logic lives in scripts/.

set dotenv-load := true
set shell := ["bash", "-uc"]
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

[doc("List recipes")]
default:
    @just --list --unsorted

# ----------------------------------------------------------------- setup

[group("setup")]
[doc("Install every dependency group from the lockfile")]
setup:
    uv sync --locked --all-groups --all-packages

[group("setup")]
[doc("Install the git pre-commit hooks")]
hooks:
    uv run pre-commit install --install-hooks

[group("setup")]
[doc("Re-resolve uv.lock")]
lock:
    uv lock

[group("setup")]
[doc("Fail if uv.lock is out of date")]
lock-check:
    uv lock --check

# --------------------------------------------------------------- quality

[group("quality")]
[doc("Format code with ruff")]
fmt:
    uv run ruff format .

[group("quality")]
[doc("Lint: ruff check, ruff format --check, yamllint")]
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run yamllint .

[group("quality")]
[doc("Apply safe ruff fixes and format")]
lint-fix:
    uv run ruff check --fix .
    uv run ruff format .

[group("quality")]
[doc("Type-check with ty (full report)")]
typecheck:
    uv run ty check

[group("quality")]
[doc("Type-check gate: the diagnostic count may not exceed the baseline (0)")]
typecheck-ratchet:
    uv run python scripts/ty_ratchet.py

[group("quality")]
[doc("Verify the import-linter architecture contracts (layers, THY/MIRA independence)")]
check-imports:
    uv run python scripts/lint_imports.py

[group("quality")]
[doc("Validate the package and relationship invariant inventory")]
check-package-invariants:
    uv run python scripts/check_package_invariants.py

[group("quality")]
[doc("Apply the Thymira Operations dashboard to Langfuse (no-op without LANGFUSE_* keys)")]
langfuse-dashboards:
    uv run python scripts/langfuse_dashboards.py

[group("quality")]
[doc("Report god classes and long modules in the workspace members")]
god-classes:
    uv run python .agents/skills/python-god-classes/scripts/detect_god_classes.py packages runtime apps adapters

# ------------------------------------------------------------------ tests

[group("test")]
[doc("Fast lane: everything except @slow (seconds)")]
test:
    uv run pytest -m "not slow"

[group("test")]
[doc("Full suite including @slow")]
test-all:
    uv run pytest

[group("test")]
[doc("Only the @slow end-to-end modules")]
test-slow:
    uv run pytest -m slow

[group("test")]
[doc("Full suite with coverage report")]
test-cov:
    uv run pytest --cov --cov-report=term-missing --cov-report=xml

# ----------------------------------------------------------------- agents

[group("agents")]
[doc("Copy .agents/skills into .claude/skills (Claude Code cannot read .agents)")]
sync-skills:
    uv run python scripts/sync_skills.py

[group("agents")]
[doc("Validate SKILL.md frontmatter and structure")]
validate-skills:
    uv run python scripts/validate_skills.py

[group("meta")]
[doc("The two roadmap documents must agree on every task")]
check-roadmap:
    uv run python scripts/check_roadmap.py

[group("meta")]
[doc("Validate lifecycle-scoped Agent Notes and their mandatory alternatives rationale")]
check-notes:
    uv run python scripts/validate_agent_notes.py

# ----------------------------------------------------------------- docker

[group("docker")]
[doc("Build the runtime image (thymira:dev)")]
docker-build:
    docker build -t thymira:dev .

[group("docker")]
[doc("Run the CLI inside the image, e.g. just docker-run --help")]
docker-run *ARGS:
    docker compose run --rm thymira {{ARGS}}

# -------------------------------------------------------------------- run

[group("run")]
[doc("Run the thymira CLI, e.g. just thymira --help")]
thymira *ARGS:
    uv run thymira {{ARGS}}

[group("run")]
[doc("Serve the web console in front of a running API, e.g. just web --port 8080")]
web *ARGS:
    uv run thymira-web {{ARGS}}

# ------------------------------------------------------------------- meta

[group("meta")]
[doc("Pre-push gate: lint, ratchet, imports, inventory, skills, roadmap, fast tests")]
check: lint typecheck-ratchet check-imports check-package-invariants validate-skills check-roadmap check-notes test

[group("meta")]
[doc("What CI runs: check plus lock-check and the full suite")]
ci: lint typecheck-ratchet check-imports check-package-invariants validate-skills check-roadmap check-notes lock-check test-all

[group("meta")]
[doc("Remove caches and build artefacts")]
clean:
    uv run python scripts/clean.py
