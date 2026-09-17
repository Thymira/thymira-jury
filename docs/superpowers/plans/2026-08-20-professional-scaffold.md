# Professional Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `mads-msct` into an English-language, agent-readable, professionally tooled Python repository (uv, ruff, ty, import-linter, pre-commit, just/make, Docker, CI, AGENTS.md, 12 agent skills) without changing runtime behaviour.

**Architecture:** Configuration lives in `pyproject.toml` + a few root dotfiles; repo tooling lives in `scripts/` (stdlib-only Python so it behaves identically on Windows and Linux); agent skills live canonically in `.agents/skills/` and are copied (never symlinked) into `.claude/skills/`. `AGENTS.md` is the single instruction file; `CLAUDE.md`/`GEMINI.md` import it.

**Tech Stack:** Python 3.11–3.13 (dev pin 3.13), uv 0.12.5, ruff 0.16.3, ty 0.0.73, import-linter 2.13, pre-commit 4.6.2, yamllint 1.38.0, just 1.58.0, GNU make 4.4, Docker, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-20-professional-scaffold-design.md`

## Global Constraints

- Every new file, comment, docstring and commit message is in **English**. Do not translate existing Spanish files.
- Runtime behaviour of `src/mads` must not change; the full suite must stay at **484 passed**.
- `requires-python = ">=3.11"`; `.python-version` = `3.13`; ruff/ty target `py311`.
- Tool versions: `ruff>=0.16.3,<0.17`, `ty==0.0.73`, `import-linter>=2.13,<3`, `pre-commit>=4.6,<5`, `yamllint>=1.38,<2`, `uv_build>=0.12.5,<0.13`, pre-commit revs `pre-commit-hooks v6.0.0`, `ruff-pre-commit v0.16.3`, `uv-pre-commit 0.12.5`, `yamllint v1.38.0`.
- No symlinks (Windows, `core.symlinks=false`). Paths in docs/scripts use forward slashes.
- Scripts under `scripts/` and inside skills are **stdlib-only**, have a `main() -> int`, `if __name__ == "__main__": raise SystemExit(main())`, argparse `--help`, and print actionable errors.
- Skill names match `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64 chars, equal the directory name, never contain `claude` or `anthropic`; frontmatter keys ⊆ {`name`, `description`, `metadata`}; `SKILL.md` ≤ 300 lines.
- Run everything through `uv run …` (PATH: `$HOME/.local/bin` holds uv/just/ruff/ty on this machine).
- Commits follow Conventional Commits and end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Work happens on branch `feat/professional-scaffold`.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `pyproject.toml` | project metadata, groups, build backend, ruff, pytest, coverage, import-linter | 1, 5 |
| `.python-version`, `uv.lock` | interpreter pin, lockfile | 1 |
| `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example` | hygiene | 2 |
| `.git-blame-ignore-revs` | hides the format commit from blame | 3 |
| `ty.toml`, `.ty-baseline.json`, `scripts/ty_ratchet.py`, `tests/tooling/test_ty_ratchet.py` | type-check ratchet | 4 |
| `tests/test_*.py` (6 slow + integration markers) | test lanes | 6 |
| `justfile`, `Makefile`, `scripts/clean.py` | task runner | 7 |
| `scripts/sync_skills.py`, `scripts/validate_skills.py`, tests | skill tooling | 8 |
| `.pre-commit-config.yaml`, `.yamllint.yaml` | hooks | 9 |
| `Dockerfile`, `.dockerignore`, `compose.yaml` | containers | 10 |
| `.github/workflows/ci.yml`, `.github/workflows/pr-agent.yml`, `.pr_agent.toml` | CI / review bot | 11 |
| `.agents/skills/*` | 12 skills + scripts + tests | 12a–12i |
| `.claude/skills/*`, `.claude/settings.json`, `scripts/hooks/format_on_edit.py` | Claude Code integration | 13 |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CONTRIBUTING.md`, `README.md` banner | instructions | 14 |
| `docs/adr/0001-harness-and-two-layer-architecture.md`, `CHANGELOG.md` | decisions, history | 15 |

Shared test helper (created in Task 4, reused by 8, 12, 13): `tests/tooling/conftest.py`

```python
"""Helpers for testing repository scripts that are not importable packages."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script(relative_path: str) -> ModuleType:
    """Import a script file (e.g. 'scripts/ty_ratchet.py') as a module."""
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
```

`tests/tooling/__init__.py` is an empty file (the `tests/` package style requires it).

---

### Task 1: Packaging — uv, PEP 735 groups, uv_build

**Files:**
- Modify: `pyproject.toml` (full rewrite below)
- Create: `.python-version`
- Regenerate: `uv.lock`
- Test: existing `tests/test_packaging.py`

**Interfaces:**
- Produces: dependency groups `test`, `lint`, `typecheck`, `dev`; ruff/pytest/coverage config used by every later task.

- [ ] **Step 1: Write `pyproject.toml`** (replace the whole file):

```toml
[project]
name = "mads"
version = "0.1.0"
description = "MADS: auditable multi-agent data-science system (thesis skeleton)"
readme = "README.md"
requires-python = ">=3.11"
# LiteLLM is the only LLM integration (see src/mads/llm/litellm_provider.py). It resolves
# the real provider from the model identifier, so there are no per-SDK code paths.
dependencies = [
    "joblib>=1.3",
    "langgraph>=1.0,<2",
    "litellm>=1.90,<2",
    "pydantic>=2,<3",
    "scikit-learn>=1.5,<2",
]

[project.optional-dependencies]
# Third-party boosting libraries stay optional: the system must install without them
# (the catalog simply does not offer them, see mads/model_catalog.py::is_available) and
# they weigh tens of MB that a minimal install should not pay for.
boosting = ["xgboost>=2.0", "lightgbm>=4.0", "catboost>=1.2"]

[project.scripts]
mads = "mads.cli:main"

# PEP 735 dependency groups: local tooling, never published. `uv sync` installs `dev`.
[dependency-groups]
# Tests need the full catalog, otherwise they would run against 15 algorithms believing
# they are 18 and the optional-algorithm tests would be skipped silently.
test = [
    "mads[boosting]",
    "pytest>=9.1,<10",
    "pytest-cov>=7.1,<8",
    "pytest-xdist>=3.8,<4",
    "hypothesis>=6.165",
]
lint = [
    "ruff>=0.16.3,<0.17",
    "import-linter>=2.13,<3",
    "yamllint>=1.38,<2",
    "pre-commit>=4.6,<5",
]
# ty is pre-1.0: pin exactly so diagnostics are reproducible across machines and CI.
typecheck = ["ty==0.0.73"]
dev = [
    { include-group = "test" },
    { include-group = "lint" },
    { include-group = "typecheck" },
]

[tool.uv]
default-groups = ["dev"]
required-version = ">=0.12"

[build-system]
requires = ["uv_build>=0.12.5,<0.13"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "mads"
module-root = "src"

# ---------------------------------------------------------------------------
# Ruff (lint + format). ty is configured in ty.toml, import-linter below.
# ---------------------------------------------------------------------------
[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests", "scripts"]
extend-exclude = [".claude/skills", ".pytest-tmp", "runs", "data"]

[tool.ruff.lint]
select = [
    "E", "W",   # pycodestyle
    "F",        # pyflakes
    "I",        # isort
    "B",        # flake8-bugbear
    "UP",       # pyupgrade
    "N",        # pep8-naming
    "SIM",      # flake8-simplify
    "C4",       # flake8-comprehensions
    "RUF",      # ruff-specific
    "PT",       # flake8-pytest-style
    "PTH",      # flake8-use-pathlib
    "T20",      # flake8-print
    "ARG",      # flake8-unused-arguments
    "TRY",      # tryceratops
    "BLE",      # flake8-blind-except
    "DTZ",      # flake8-datetimez
    "ISC",      # flake8-implicit-str-concat
    "PIE",      # flake8-pie
    "RET",      # flake8-return
    "PERF",     # perflint
    "FURB",     # refurb
    "PLE", "PLW", "PLC",  # pylint errors/warnings/conventions (PLR complexity is deferred)
    "S",        # flake8-bandit
    "A",        # flake8-builtins
    "G",        # flake8-logging-format
    "RSE",      # flake8-raise
    "SLF",      # flake8-self
    "TID",      # flake8-tidy-imports
    "TC",       # flake8-type-checking
]
ignore = [
    "COM812", "ISC001",  # conflict with the formatter
    "TRY003",            # long messages in exceptions are fine here
]
# Deferred until the English translation pass rewrites every docstring: D (pydocstyle),
# ANN (annotations), PLR (complexity), ERA (commented-out code).

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]
"tests/**" = ["S101", "ARG", "PLR2004", "SLF001", "S311", "S603", "S607"]
"src/mads/cli.py" = ["T20"]
"src/mads/console.py" = ["T20"]
"scripts/**" = ["T20", "S603", "S607"]
".agents/skills/**" = ["T20", "S603", "S607"]

[tool.ruff.lint.isort]
known-first-party = ["mads"]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.format]
docstring-code-format = true

# ---------------------------------------------------------------------------
# pytest / coverage
# ---------------------------------------------------------------------------
[tool.pytest.ini_options]
testpaths = ["tests"]
# Serial by default: reproducible everywhere and does not saturate the machine while
# training models. Opt into parallelism explicitly with `pytest -n auto`.
addopts = "--basetemp=.pytest-tmp --strict-markers"
markers = [
    "slow: full orchestrator or end-to-end runs that train many models (minutes); excluded by `just test`",
    "integration: exercises the on-disk ArtifactStore, real scikit-learn training or the CLI",
]
# LightGBM records internal column names while fitting and sklearn then warns that the
# prediction X lacks them, although names were never passed. Library noise, not a
# pipeline problem. The other two filters mirror warnings already handled in the model
# builder; they are repeated here because pytest recomposes its own filters at start-up.
filterwarnings = [
    "ignore:X does not have valid feature names:UserWarning",
    "ignore:The SAMME\\.R algorithm .* is deprecated and will be removed in 1\\.6.*:FutureWarning:sklearn\\.ensemble\\._weight_boosting",
    "ignore:.*Maximum iterations \\(500\\) reached.*:sklearn.exceptions.ConvergenceWarning:sklearn\\.neural_network\\._multilayer_perceptron",
]

[tool.coverage.run]
source = ["mads"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = true
```

- [ ] **Step 2: Pin the interpreter**

Run: `uv python pin 3.13` → creates `.python-version` containing `3.13`.

- [ ] **Step 3: Lock and sync**

Run: `uv lock && uv sync --locked --all-groups`
Expected: `Resolved N packages`, no errors. If `uv lock` rejects `mads[boosting]` inside
`[dependency-groups]`, replace that line with the three boosting requirements verbatim
(`"xgboost>=2.0", "lightgbm>=4.0", "catboost>=1.2"`) and add a comment saying why.

- [ ] **Step 4: Verify the wheel ships the policy pack**

Run: `uv build && uv run python -c "import zipfile,glob; w=glob.glob('dist/*.whl')[0]; names=zipfile.ZipFile(w).namelist(); assert 'mads/policy_packs/fraude_aml.json' in names, names; print('OK', w)"`
Expected: `OK dist/mads-0.1.0-py3-none-any.whl`. If the assertion fails, create an empty
`src/mads/policy_packs/__init__.py` and repeat; record the outcome in the commit body.

- [ ] **Step 5: Confirm the packaging test and import still work**

Run: `uv run pytest tests/test_packaging.py -q`
Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .python-version uv.lock src/mads/policy_packs/__init__.py 2>/dev/null; git add pyproject.toml .python-version uv.lock
git commit -m "build: manage the project with uv (PEP 735 groups, uv_build, pinned 3.13)

Replaces the setuptools metadata with uv_build, moves dev tooling into
dependency groups (test/lint/typecheck/dev), tightens langgraph to the 1.x
line the lockfile already resolves, and adds the ruff/pytest/coverage
configuration used by the rest of the scaffold.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Repository hygiene files

**Files:**
- Modify: `.gitignore`, `.env.example`
- Create: `.gitattributes`, `.editorconfig`

- [ ] **Step 1: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[codz]
*.egg-info/
build/
dist/

# Environments and secrets (uv.lock is tracked on purpose)
.venv/
.env
!.env.example

# Tool caches
.pytest_cache/
.pytest-*/
.ruff_cache/
.import_linter_cache/
.mypy_cache/
.coverage
.coverage.*
htmlcov/
coverage.xml
.ipynb_checkpoints/

# Run artifacts and large local data
runs/
data/creditcard.csv
*.log

# IDEs
.idea/
.vscode/*
!.vscode/extensions.json

# Coding agents: shared config is tracked, personal config is not
.claude/settings.local.json
.claude/*.local.*
```

- [ ] **Step 2: Write `.gitattributes`**

```gitattributes
# Normalise text to LF in the repository and on checkout on every platform.
* text=auto eol=lf

*.py    text eol=lf
*.pyi   text eol=lf
*.md    text eol=lf
*.toml  text eol=lf
*.yaml  text eol=lf
*.yml   text eol=lf
*.json  text eol=lf
*.sh    text eol=lf
justfile text eol=lf
Makefile text eol=lf

# Windows-only scripts need CRLF
*.bat text eol=crlf
*.ps1 text eol=crlf

# Data and binary artefacts: never rewrite line endings, never diff as text
*.csv     -text
*.parquet -text binary
*.joblib  -text binary
*.npy     -text binary
*.png     -text binary
*.pdf     -text binary
```

- [ ] **Step 3: Write `.editorconfig`**

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.{yml,yaml,json,toml}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab

[justfile]
indent_size = 4
```

- [ ] **Step 4: Rewrite `.env.example` in English** (same variables, same defaults)

```dotenv
# Copy this file to .env and fill in the key of the provider LiteLLM will use for the
# chosen model identifier (--model or MADS_MODEL). LiteLLM reads these variables
# directly; MADS never processes, stores or logs them.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# MADS_MODEL is a MADS convention (not a native LiteLLM variable): the default model
# identifier when --model is omitted. LiteLLM format: a bare name means OpenAI
# (e.g. gpt-5.6-luna); "<provider>/<model>" routes elsewhere (e.g. anthropic/claude-sonnet-4-5).
MADS_MODEL=gpt-5.6-luna
```

- [ ] **Step 5: Verify no renormalisation noise**

Run: `git add --renormalize . && git status --short | grep -v "^A\|^M  \.\(gitignore\|gitattributes\|editorconfig\|env.example\)" | head`
Expected: empty (the index is already LF-only).

- [ ] **Step 6: Commit**

```bash
git add .gitignore .gitattributes .editorconfig .env.example
git commit -m "chore: add repository hygiene files (gitattributes, editorconfig, gitignore)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: One-time formatting and a green ruff

**Files:**
- Modify: `src/**/*.py`, `tests/**/*.py`, `probar_agentico.py` (formatting / safe fixes only)
- Create: `.git-blame-ignore-revs`

- [ ] **Step 1: Format and apply safe fixes only**

Run: `uv run ruff format . && uv run ruff check --fix .`
(`--fix` applies only safe fixes; never pass `--unsafe-fixes` here.)

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -q -p no:cacheprovider`
Expected: `484 passed`.

- [ ] **Step 3: Commit the pure format/safe-fix pass**

```bash
git add -A src tests probar_agentico.py
git commit -m "style: format the codebase with ruff (one-time, blame-ignored)

Mechanical: ruff format + safe autofixes only. No behaviour change; the full
suite passes. Listed in .git-blame-ignore-revs.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Record the commit for blame**

Run: `printf '# Run: git config blame.ignoreRevsFile .git-blame-ignore-revs\n# style: format the codebase with ruff (one-time)\n%s\n' "$(git rev-parse HEAD)" > .git-blame-ignore-revs && git config blame.ignoreRevsFile .git-blame-ignore-revs`

- [ ] **Step 5: Triage the remaining findings**

Run: `uv run ruff check . --statistics`

Apply this rule per rule code, in order:
1. `F821` (undefined name): open each location; if it is an obvious typo with a test covering the path, fix it; otherwise add `# noqa: F821  # TODO(bug): <one-line description>` and list the location in the final report.
2. Count ≤ 10 → fix by hand, preserving behaviour (e.g. `BLE001`: narrow to the exception types the code can actually raise, or add `# noqa: BLE001  # reason` when the broad except is deliberate for robustness; `RUF012`: annotate with `ClassVar`; `DTZ006`: pass `tz=datetime.UTC` only if the consumer already expects aware datetimes — otherwise `# noqa: DTZ006  # naive timestamps are part of the trace contract`).
3. Count > 10 → add the code to `[tool.ruff.lint] ignore` with a trailing comment `# deferred: <N> findings on 2026-08-20` and add it to the deferred list in the CHANGELOG (Task 15).

After each batch run `uv run pytest -q -p no:cacheprovider -m "not slow"` (fast) and, once at the
end, the full suite.

- [ ] **Step 6: Verify ruff is green**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` and `N files already formatted`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "fix(lint): resolve or explicitly defer the remaining ruff findings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: ty configuration and diagnostic ratchet

**Files:**
- Create: `ty.toml`, `scripts/ty_ratchet.py`, `.ty-baseline.json`, `tests/tooling/__init__.py`, `tests/tooling/conftest.py` (content in *File map*), `tests/tooling/test_ty_ratchet.py`

**Interfaces:**
- Produces: `scripts/ty_ratchet.py` with `parse_concise(text: str) -> dict[str, int]`, `compare(current: dict[str, int], baseline: dict, strict: bool) -> tuple[bool, str]`, `main(argv: list[str] | None = None) -> int`; CLI flags `--baseline PATH` (default `.ty-baseline.json`), `--update-baseline`, `--strict`.

- [ ] **Step 1: Write `ty.toml`**

```toml
# ty configuration. Single source of truth: do NOT add [tool.ty] to pyproject.toml —
# ty.toml takes precedence and the two would drift.
[environment]
python-version = "3.11"
root = ["./src"]

[src]
include = ["src", "tests", "scripts", ".agents/skills"]
respect-ignore-files = true

[terminal]
output-format = "concise"
```

- [ ] **Step 2: Write the failing tests** `tests/tooling/test_ty_ratchet.py`

```python
from __future__ import annotations

import json

from tests.tooling.conftest import load_script

SAMPLE = """\
src/mads/a.py:12:5: error[invalid-return-type] Return type does not match
src/mads/a.py:40:9: warning[possibly-unresolved-reference] Name `x` may be unbound
src/mads/b.py:3:1: error[invalid-return-type] Return type does not match
Found 3 diagnostics
"""


def test_parse_concise_counts_per_rule():
    ratchet = load_script("scripts/ty_ratchet.py")
    counts = ratchet.parse_concise(SAMPLE)
    assert counts == {"invalid-return-type": 2, "possibly-unresolved-reference": 1}


def test_parse_concise_falls_back_to_summary_line():
    ratchet = load_script("scripts/ty_ratchet.py")
    assert ratchet.parse_concise("Found 7 diagnostics\n") == {"<unparsed>": 7}


def test_compare_fails_on_increase_and_passes_on_decrease():
    ratchet = load_script("scripts/ty_ratchet.py")
    baseline = {"total": 2, "by_rule": {"invalid-return-type": 2}}
    ok, _ = ratchet.compare({"invalid-return-type": 3}, baseline, strict=False)
    assert ok is False
    ok, _ = ratchet.compare({"invalid-return-type": 1}, baseline, strict=False)
    assert ok is True


def test_compare_strict_fails_when_baseline_is_stale():
    ratchet = load_script("scripts/ty_ratchet.py")
    baseline = {"total": 2, "by_rule": {"invalid-return-type": 2}}
    ok, message = ratchet.compare({"invalid-return-type": 1}, baseline, strict=True)
    assert ok is False
    assert "--update-baseline" in message


def test_main_update_baseline_writes_file(tmp_path, monkeypatch):
    ratchet = load_script("scripts/ty_ratchet.py")
    monkeypatch.setattr(ratchet, "run_ty", lambda: SAMPLE)
    baseline = tmp_path / "baseline.json"
    assert ratchet.main(["--baseline", str(baseline), "--update-baseline"]) == 0
    data = json.loads(baseline.read_text())
    assert data["total"] == 3
    assert data["by_rule"]["invalid-return-type"] == 2
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/tooling/test_ty_ratchet.py -q`
Expected: errors (`FileNotFoundError` for the script).

- [ ] **Step 4: Write `scripts/ty_ratchet.py`**

```python
"""Fail CI only when the number of ty diagnostics grows.

ty is pre-1.0 and the legacy code base has a known number of diagnostics. This script
turns that number into a ratchet: the count may go down, never up.

Usage:
    python scripts/ty_ratchet.py                   # compare against .ty-baseline.json
    python scripts/ty_ratchet.py --strict          # also fail if the baseline is stale (CI)
    python scripts/ty_ratchet.py --update-baseline # rewrite the baseline after a clean-up
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / ".ty-baseline.json"
DIAGNOSTIC = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<severity>error|warning|info)\[(?P<rule>[a-z0-9-]+)\]")
SUMMARY = re.compile(r"^Found (?P<n>\d+) diagnostics?")


def find_ty() -> str:
    """Locate the ty executable next to the running interpreter, then on PATH."""
    candidates = [Path(sys.executable).with_name("ty"), Path(sys.executable).with_name("ty.exe")]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("ty")
    if found is None:
        msg = "ty was not found. Install the typecheck group: uv sync --group typecheck"
        raise SystemExit(msg)
    return found


def run_ty() -> str:
    """Run `ty check` and return its combined output (exit code is irrelevant here)."""
    completed = subprocess.run(
        [find_ty(), "check", "--output-format", "concise"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return completed.stdout + completed.stderr


def parse_concise(text: str) -> dict[str, int]:
    """Count diagnostics per rule from ty's concise output."""
    counts: Counter[str] = Counter()
    summary_total: int | None = None
    for line in text.splitlines():
        match = DIAGNOSTIC.match(line.strip())
        if match:
            counts[match.group("rule")] += 1
            continue
        summary = SUMMARY.match(line.strip())
        if summary:
            summary_total = int(summary.group("n"))
    if not counts and summary_total:
        return {"<unparsed>": summary_total}
    return dict(sorted(counts.items()))


def compare(current: dict[str, int], baseline: dict, strict: bool) -> tuple[bool, str]:
    """Return (ok, human-readable message)."""
    current_total = sum(current.values())
    baseline_total = int(baseline.get("total", 0))
    baseline_rules: dict[str, int] = baseline.get("by_rule", {})
    lines = [f"ty diagnostics: {current_total} (baseline {baseline_total})"]
    for rule in sorted(set(current) | set(baseline_rules)):
        before, after = baseline_rules.get(rule, 0), current.get(rule, 0)
        if before != after:
            lines.append(f"  {rule}: {before} -> {after}")
    if current_total > baseline_total:
        lines.append("FAIL: diagnostics increased. Fix the new ones or, if they are pre-existing")
        lines.append("      findings surfaced by a ty upgrade, run: python scripts/ty_ratchet.py --update-baseline")
        return False, "\n".join(lines)
    if strict and current_total < baseline_total:
        lines.append("FAIL (strict): the baseline is stale. Lower it with: python scripts/ty_ratchet.py --update-baseline")
        return False, "\n".join(lines)
    if current_total < baseline_total:
        lines.append("Diagnostics went down — consider: python scripts/ty_ratchet.py --update-baseline")
    lines.append("OK")
    return True, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail when the baseline is stale")
    args = parser.parse_args(argv)

    current = parse_concise(run_ty())
    if args.update_baseline:
        payload = {"total": sum(current.values()), "by_rule": current}
        args.baseline.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline written to {args.baseline}: {payload['total']} diagnostics")
        return 0
    if not args.baseline.exists():
        print(f"No baseline at {args.baseline}. Create it with --update-baseline")
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    ok, message = compare(current, baseline, strict=args.strict)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/tooling/test_ty_ratchet.py -q`
Expected: `5 passed`.

- [ ] **Step 6: Record the baseline and check it**

Run: `uv run python scripts/ty_ratchet.py --update-baseline && uv run python scripts/ty_ratchet.py --strict`
Expected: `Baseline written … N diagnostics` then `OK`. Note N (≈78) for the final report.

- [ ] **Step 7: Commit**

```bash
git add ty.toml .ty-baseline.json scripts/ty_ratchet.py tests/tooling
git commit -m "ci(ty): add ty configuration and a diagnostic-count ratchet

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: import-linter architecture contracts

**Files:**
- Modify: `pyproject.toml` (append the section below)

- [ ] **Step 1: Append to `pyproject.toml`**

```toml
# ---------------------------------------------------------------------------
# import-linter: the architecture as machine-checked contracts (`uv run lint-imports`).
# Layers are listed highest first; a layer may import the ones below it, never above.
# Modules joined with `|` on one line are independent siblings (must not import each other).
# ---------------------------------------------------------------------------
[tool.importlinter]
root_packages = ["mads"]
exclude_type_checking_imports = true

[[tool.importlinter.contracts]]
name = "Layered architecture (current modules)"
type = "layers"
containers = ["mads"]
exhaustive = true
exhaustive_ignores = ["policy_packs"]
layers = [
    "cli",
    "console",
    "orchestrator",
    "audit",
    "workers",
    "gate",
    "decisions | policies | risk",
    "skills",
    "contracts | artifacts | tracing",
    "llm | model_catalog | rag",
    "utils",
]
ignore_imports = [
    # TODO(arch): workers.ml is a pure CSV/tabular loader misplaced under workers. Move it
    # to the foundation layer (e.g. mads.tabular_io) and delete this exception.
    "mads.skills.** -> mads.workers.ml",
]

[[tool.importlinter.contracts]]
name = "Skills never depend on the orchestrator or the entry points"
type = "forbidden"
source_modules = ["mads.skills"]
forbidden_modules = ["mads.orchestrator", "mads.cli", "mads.console"]

[[tool.importlinter.contracts]]
name = "Audit observes runs from disk and never imports the orchestrator"
type = "forbidden"
source_modules = ["mads.audit"]
forbidden_modules = ["mads.orchestrator", "mads.workers", "mads.cli", "mads.console"]
```

- [ ] **Step 2: Run it**

Run: `uv run lint-imports`
Expected: `Contracts: 3 kept, 0 broken.` If the layers contract reports a violation, inspect
the reported edge: (a) `exhaustive` complaint about a module → add it to the right layer line
(e.g. `policy_packs` if it gained an `__init__.py` in Task 1 → keep it in `exhaustive_ignores`);
(b) an unexpected upward edge → do **not** reorder layers to hide it; add a narrowly scoped
`ignore_imports` entry with a `TODO(arch)` comment and report it.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "ci(architecture): enforce module layering with import-linter

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Test lanes — `slow` and `integration` markers

**Files:**
- Modify: `tests/test_end_to_end.py`, `tests/test_agentic_orchestrator.py`, `tests/test_analytical_decisions.py`, `tests/test_meta_auditor_controls.py`, `tests/test_orchestrator_risk_integration.py`, `tests/test_governance_training_gate.py` (add `slow`), plus every test module that uses the `synthetic_case` fixture or `ArtifactStore` (add `integration`).

- [ ] **Step 1: Mark the six slow modules**

In each of the six files, directly after the imports, add:

```python
pytestmark = pytest.mark.slow
```

(add `import pytest` if missing). If a module already has a `pytestmark`, make it a list:
`pytestmark = [pytest.mark.slow, <existing>]`.

- [ ] **Step 2: Mark integration modules mechanically**

Run: `grep -l "synthetic_case\|ArtifactStore" tests/test_*.py`
For each listed file that is **not** one of the six slow files, add `pytestmark = pytest.mark.integration`
(slow files keep only `slow`; `slow` implies integration).

- [ ] **Step 3: Verify the lanes**

Run: `uv run pytest -m "not slow" -q -p no:cacheprovider | tail -1` → expect fewer than 484 tests, all passed, well under 2 minutes.
Run: `uv run pytest -q -p no:cacheprovider | tail -1` → expect `484 passed` (count unchanged).
Run: `uv run pytest --collect-only -q -m slow | tail -1` → expect the six modules' tests only.

- [ ] **Step 4: Commit**

```bash
git add tests
git commit -m "test: register slow/integration markers and tag the heavy modules

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Task runners — `justfile` (canonical) and `Makefile` (shim)

**Files:**
- Create: `justfile`, `Makefile`, `scripts/clean.py`

- [ ] **Step 1: Write `scripts/clean.py`**

```python
"""Remove caches and build artefacts (cross-platform replacement for `rm -rf`)."""

from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = [".pytest_cache", ".pytest-tmp", ".ruff_cache", ".import_linter_cache", "build", "dist", "htmlcov"]
FILES = [".coverage", "coverage.xml"]


def main() -> int:
    removed = 0
    for name in DIRECTORIES:
        path = REPO_ROOT / name
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
    for name in FILES:
        path = REPO_ROOT / name
        if path.is_file():
            path.unlink()
            removed += 1
    for pycache in REPO_ROOT.rglob("__pycache__"):
        if ".venv" in pycache.parts:
            continue
        shutil.rmtree(pycache)
        removed += 1
    print(f"Removed {removed} cache entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write `justfile`**

```just
# MADS task runner. This file is canonical; the Makefile forwards to it.
# Every recipe line is a plain command that works in bash and PowerShell; logic lives in scripts/.

set dotenv-load := true
set shell := ["bash", "-uc"]

[windows]
set shell := ["powershell.exe", "-NoLogo", "-Command"]

[doc("List recipes")]
default:
    @just --list --unsorted

# ----------------------------------------------------------------- setup

[group("setup")]
[doc("Install every dependency group from the lockfile")]
setup:
    uv sync --locked --all-groups

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
[doc("Type-check ratchet: fail only if diagnostics grew")]
typecheck-ratchet:
    uv run python scripts/ty_ratchet.py

[group("quality")]
[doc("Verify the import-linter architecture contracts")]
check-imports:
    uv run lint-imports

[group("quality")]
[doc("Report god classes and long modules in src/mads")]
god-classes:
    uv run python .agents/skills/python-god-classes/scripts/detect_god_classes.py src/mads

# ------------------------------------------------------------------ tests

[group("test")]
[doc("Fast lane: everything except @slow")]
test:
    uv run pytest -m "not slow"

[group("test")]
[doc("Full suite (minutes)")]
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

# ----------------------------------------------------------------- docker

[group("docker")]
[doc("Build the runtime image (mads:dev)")]
docker-build:
    docker build -t mads:dev .

[group("docker")]
[doc("Build the image with the boosting extra (mads:dev-boosting)")]
docker-build-boosting:
    docker build --build-arg EXTRAS=boosting -t mads:dev-boosting .

[group("docker")]
[doc("Run the CLI inside the image, e.g. just docker-run catalog")]
docker-run *ARGS:
    docker compose run --rm mads {{ARGS}}

# -------------------------------------------------------------------- run

[group("run")]
[doc("Run the mads CLI, e.g. just mads catalog")]
mads *ARGS:
    uv run mads {{ARGS}}

# ------------------------------------------------------------------- meta

[group("meta")]
[doc("Pre-push gate: lint, ratchet, imports, skills, fast tests")]
check: lint typecheck-ratchet check-imports validate-skills test

[group("meta")]
[doc("What CI runs: check plus lock-check and the full suite")]
ci: lint typecheck-ratchet check-imports validate-skills lock-check test-all

[group("meta")]
[doc("Remove caches and build artefacts")]
clean:
    uv run python scripts/clean.py
```

- [ ] **Step 3: Write `Makefile`**

```make
# Thin shim. The justfile is the source of truth: `make <target>` runs `just <target>`.
# Install just with: uv tool install rust-just
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory
.SUFFIXES:

JUST_VERSION := $(shell just --version)
ifeq ($(strip $(JUST_VERSION)),)
$(error just is not installed. Install it with: uv tool install rust-just)
endif

.PHONY: help
help: ## List available recipes (delegates to `just --list`)
	@just --list --unsorted

# Never try to rebuild this file through the catch-all rule.
Makefile: ;

# Forward every other target to the identically named just recipe.
%:
	@just $@
```

- [ ] **Step 4: Verify on both shells**

Run (Git Bash): `just --list && just lint && just check-imports && make help && make lock-check`
Run (PowerShell): `just --list; just lock-check; just mads --help`
Expected: recipe list printed once per invocation; each command exits 0 (`just lint` may fail until Task 9 adds `.yamllint.yaml` — if yamllint complains only about missing config, continue; re-run after Task 9).

- [ ] **Step 5: Commit**

```bash
git add justfile Makefile scripts/clean.py
git commit -m "build: add justfile (canonical) and a forwarding Makefile

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Skill tooling — `sync_skills.py` and `validate_skills.py`

**Files:**
- Create: `scripts/sync_skills.py`, `scripts/validate_skills.py`, `tests/tooling/test_sync_skills.py`, `tests/tooling/test_validate_skills.py`

**Interfaces:**
- Produces: `sync_skills.sync(source: Path, target: Path, check: bool) -> list[str]` (returns drift descriptions; empty = in sync), CLI `--source`, `--target`, `--check`.
- Produces: `validate_skills.validate_skill(skill_dir: Path) -> list[str]` (error strings), `validate_skills.parse_frontmatter(text: str) -> tuple[dict, str]` (frontmatter dict, body), CLI positional `paths` (default `.agents/skills`).

- [ ] **Step 1: Write the failing tests** `tests/tooling/test_sync_skills.py`

```python
from __future__ import annotations

from pathlib import Path

from tests.tooling.conftest import load_script


def make_skill(root: Path, name: str, body: str = "# Demo\n") -> Path:
    skill = root / name
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Demo skill.\n---\n{body}", encoding="utf-8")
    (skill / "scripts" / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    return skill


def test_sync_copies_skills_and_reports_in_sync(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    make_skill(source, "beta")
    assert sync_skills.sync(source, target, check=False) == []
    assert (target / "alpha" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: alpha")
    assert (target / "beta" / "scripts" / "tool.py").exists()
    assert sync_skills.sync(source, target, check=True) == []


def test_check_reports_drift_and_stale_targets(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    sync_skills.sync(source, target, check=False)
    (target / "alpha" / "SKILL.md").write_text("tampered", encoding="utf-8")
    make_skill(target, "stale")
    drift = sync_skills.sync(source, target, check=True)
    assert any("alpha/SKILL.md" in item for item in drift)
    assert any("stale" in item for item in drift)
    # a real sync repairs both
    assert sync_skills.sync(source, target, check=False) == []
    assert not (target / "stale").exists()
    assert sync_skills.sync(source, target, check=True) == []


def test_pycache_is_not_copied(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    (source / "alpha" / "scripts" / "__pycache__").mkdir()
    (source / "alpha" / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"")
    sync_skills.sync(source, target, check=False)
    assert not (target / "alpha" / "scripts" / "__pycache__").exists()
```

and `tests/tooling/test_validate_skills.py`

```python
from __future__ import annotations

from pathlib import Path

from tests.tooling.conftest import load_script

VALID = """---
name: {name}
description: Use when testing the validator. Validates demo skills in third person.
metadata:
  version: "1.0.0"
---

# Demo

Run `scripts/tool.py`.
"""


def write_skill(root: Path, dirname: str, name: str | None = None, text: str | None = None) -> Path:
    skill = root / dirname
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(text or VALID.format(name=name or dirname), encoding="utf-8")
    return skill


def test_valid_skill_has_no_errors(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    assert validate.validate_skill(write_skill(tmp_path, "demo-skill")) == []


def test_parse_frontmatter_reads_nested_metadata(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    meta, body = validate.parse_frontmatter(VALID.format(name="demo-skill"))
    assert meta["name"] == "demo-skill"
    assert meta["metadata"] == {"version": "1.0.0"}
    assert body.lstrip().startswith("# Demo")


def test_name_must_match_directory(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", name="other-name"))
    assert any("directory" in e for e in errors)


def test_rejects_bad_names_and_missing_description(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    bad = "---\nname: Bad--Name\n---\n# x\n"
    errors = validate.validate_skill(write_skill(tmp_path, "bad--name", text=bad))
    assert any("name" in e.lower() for e in errors)
    assert any("description" in e for e in errors)


def test_rejects_reserved_words_unknown_keys_and_missing_references(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    text = (
        "---\nname: demo-skill\ndescription: Something.\ncontext: fork\n---\n"
        "# Demo\nSee `references/missing.md` and `scripts\\\\win.py`.\n"
    )
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", text=text))
    joined = "\n".join(errors)
    assert "context" in joined
    assert "references/missing.md" in joined
    assert "backslash" in joined.lower()
    text2 = "---\nname: claude-helper\ndescription: Something.\n---\n# x\n"
    errors2 = validate.validate_skill(write_skill(tmp_path, "claude-helper", text=text2))
    assert any("reserved" in e for e in errors2)


def test_body_length_limit(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    long_body = VALID.format(name="demo-skill") + ("line\n" * 520)
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", text=long_body))
    assert any("500" in e for e in errors)


def test_main_reports_all_skills(tmp_path, capsys):
    validate = load_script("scripts/validate_skills.py")
    write_skill(tmp_path, "demo-skill")
    write_skill(tmp_path, "other-skill", name="mismatch")
    assert validate.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "other-skill" in out
    assert "1 skill(s) valid" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/tooling/test_sync_skills.py tests/tooling/test_validate_skills.py -q`
Expected: errors (scripts missing).

- [ ] **Step 3: Write `scripts/sync_skills.py`**

```python
"""Copy canonical skills from .agents/skills into .claude/skills.

Codex, Cursor and Gemini CLI read `.agents/skills/` natively; Claude Code only reads
`.claude/skills/`. Symlinks are not an option on Windows, so the directory is copied and
the copy is committed. Run `--check` (pre-commit, CI) to fail when the copy drifted.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / ".agents" / "skills"
TARGET = REPO_ROOT / ".claude" / "skills"
IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
README = (
    "# Generated directory\n\n"
    "These skills are copied from `.agents/skills/` by `scripts/sync_skills.py`.\n"
    "Edit the canonical copy and run `just sync-skills`; do not edit files here.\n"
)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_DIRS or name.endswith(".pyc")}


def _skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _compare_tree(source: Path, target: Path, prefix: str) -> list[str]:
    """Return differences between two skill directories as 'prefix/relative: reason'."""
    drift: list[str] = []
    comparison = filecmp.dircmp(source, target, ignore=list(IGNORED_DIRS))
    drift.extend(f"{prefix}/{name}: missing in target" for name in comparison.left_only if not name.endswith(".pyc"))
    drift.extend(f"{prefix}/{name}: unexpected in target" for name in comparison.right_only if not name.endswith(".pyc"))
    drift.extend(f"{prefix}/{name}: content differs" for name in comparison.diff_files)
    for sub in comparison.common_dirs:
        drift.extend(_compare_tree(source / sub, target / sub, f"{prefix}/{sub}"))
    return drift


def sync(source: Path, target: Path, check: bool) -> list[str]:
    """Mirror `source` into `target`. With check=True, only report drift."""
    drift: list[str] = []
    source_names = {p.name for p in _skill_dirs(source)}
    target.mkdir(parents=True, exist_ok=True)
    for stale in sorted(p for p in target.iterdir() if p.is_dir() and p.name not in source_names):
        if check:
            drift.append(f"{stale.name}: stale, not present in {source}")
        else:
            shutil.rmtree(stale)
    for skill in _skill_dirs(source):
        destination = target / skill.name
        if check:
            if not destination.is_dir():
                drift.append(f"{skill.name}: missing in target")
            else:
                drift.extend(_compare_tree(skill, destination, skill.name))
            continue
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill, destination, ignore=_ignore)
    if not check:
        (target / "README.md").write_text(README, encoding="utf-8")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--target", type=Path, default=TARGET)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)
    drift = sync(args.source, args.target, check=args.check)
    if args.check:
        if drift:
            print("Skill copies are out of date:")
            print("\n".join(f"  - {item}" for item in drift))
            print("Run: just sync-skills  (or: python scripts/sync_skills.py)")
            return 1
        print("Skill copies are in sync")
        return 0
    print(f"Synced {len(_skill_dirs(args.source))} skill(s) into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write `scripts/validate_skills.py`**

```python
"""Validate agent skills against the Agent Skills specification (agentskills.io).

Checks every `<dir>/SKILL.md`: frontmatter delimiters on the very first line, the
`name`/`description` constraints, the portable key allowlist, body length, referenced
files and forward-slash paths. Exit code 1 when any skill is invalid.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = [REPO_ROOT / ".agents" / "skills"]
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED = ("anthropic", "claude")
ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
MAX_BODY_LINES = 500
REFERENCE_RE = re.compile(r"(?<![\w/])((?:scripts|references|assets)/[\w./-]+)")
XML_TAG_RE = re.compile(r"<[^>]+>")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the minimal YAML subset used by SKILL.md files.

    Supports `key: value` and one level of nested mapping (`metadata:` followed by
    indented `key: value` lines). Returns (frontmatter, body). Raises ValueError when
    the delimiters are missing.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        msg = "SKILL.md must start with '---' on the first line"
        raise ValueError(msg)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        msg = "closing '---' for the frontmatter not found"
        raise ValueError(msg) from exc
    data: dict = {}
    current_key: str | None = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):
            if current_key is None or not isinstance(data.get(current_key), dict):
                msg = f"unexpected indented line in frontmatter: {raw!r}"
                raise ValueError(msg)
            key, _, value = raw.strip().partition(":")
            data[current_key][key.strip()] = _unquote(value)
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            msg = f"malformed frontmatter line: {raw!r}"
            raise ValueError(msg)
        key = key.strip()
        value = value.strip()
        if value == "":
            data[key] = {}
            current_key = key
        else:
            data[key] = _unquote(value)
            current_key = None
    return data, "\n".join(lines[end + 1 :])


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def validate_skill(skill_dir: Path) -> list[str]:
    """Return a list of human-readable errors for one skill directory (empty = valid)."""
    errors: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return [f"{skill_dir.name}: SKILL.md is missing"]
    text = skill_file.read_text(encoding="utf-8")
    try:
        meta, body = parse_frontmatter(text)
    except ValueError as exc:
        return [f"{skill_dir.name}: {exc}"]

    name = str(meta.get("name", ""))
    if not name:
        errors.append(f"{skill_dir.name}: 'name' is required")
    else:
        if not NAME_RE.match(name):
            errors.append(f"{skill_dir.name}: name {name!r} must match {NAME_RE.pattern} (lowercase, hyphens, no '--')")
        if len(name) > 64:
            errors.append(f"{skill_dir.name}: name longer than 64 characters")
        if name != skill_dir.name:
            errors.append(f"{skill_dir.name}: name {name!r} must equal the directory name")
        if any(word in name for word in RESERVED):
            errors.append(f"{skill_dir.name}: name contains a reserved word ({', '.join(RESERVED)})")

    description = str(meta.get("description", "")).strip()
    if not description:
        errors.append(f"{skill_dir.name}: 'description' is required and must be non-empty")
    elif len(description) > 1024:
        errors.append(f"{skill_dir.name}: description longer than 1024 characters")
    if XML_TAG_RE.search(description) or XML_TAG_RE.search(name):
        errors.append(f"{skill_dir.name}: name/description must not contain XML tags")

    unknown = sorted(set(meta) - ALLOWED_KEYS)
    if unknown:
        errors.append(f"{skill_dir.name}: non-portable frontmatter key(s): {', '.join(unknown)}")
    compatibility = meta.get("compatibility")
    if compatibility and len(str(compatibility)) > 500:
        errors.append(f"{skill_dir.name}: compatibility longer than 500 characters")

    body_lines = body.count("\n") + 1
    if body_lines > MAX_BODY_LINES:
        errors.append(f"{skill_dir.name}: body has {body_lines} lines; keep SKILL.md under {MAX_BODY_LINES}")

    if re.search(r"(scripts|references|assets)\\", body):
        errors.append(f"{skill_dir.name}: use forward slashes in paths, not backslashes")
    for reference in sorted(set(REFERENCE_RE.findall(body))):
        if not (skill_dir / reference).exists():
            errors.append(f"{skill_dir.name}: referenced file does not exist: {reference}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, default=DEFAULT_DIRS, help="skill roots to validate")
    args = parser.parse_args(argv)
    valid = invalid = 0
    for root in args.paths:
        skill_dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        if not skill_dirs:
            print(f"{root}: no skill directories found")
            invalid += 1
        for skill_dir in skill_dirs:
            errors = validate_skill(skill_dir)
            if errors:
                invalid += 1
                print(f"FAIL {skill_dir}")
                print("\n".join(f"  - {e}" for e in errors))
            else:
                valid += 1
    print(f"{valid} skill(s) valid, {invalid} invalid")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/tooling -q`
Expected: all pass (5 + 3 + 7 = 15).

- [ ] **Step 6: Lint the new scripts and commit**

Run: `uv run ruff check scripts tests/tooling && uv run ruff format --check scripts tests/tooling`

```bash
git add scripts/sync_skills.py scripts/validate_skills.py tests/tooling
git commit -m "feat(agents): add skill sync and validation scripts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: pre-commit and yamllint

**Files:**
- Create: `.pre-commit-config.yaml`, `.yamllint.yaml`

- [ ] **Step 1: Write `.yamllint.yaml`**

```yaml
extends: default

rules:
  line-length:
    max: 120
    allow-non-breakable-words: true
    level: warning
  document-start: disable
  truthy:
    check-keys: false   # GitHub Actions uses `on:` as a key
  comments:
    min-spaces-from-content: 1
  indentation:
    spaces: 2
    indent-sequences: consistent

ignore: |
  .venv/
  runs/
  .pytest-tmp/
```

- [ ] **Step 2: Write `.pre-commit-config.yaml`**

```yaml
# Install once: just hooks   (or: uv run pre-commit install --install-hooks)
# Run on demand: uv run pre-commit run --all-files
minimum_pre_commit_version: "4.6.0"
default_language_version:
  python: python3.13
fail_fast: false

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
        args: [--markdown-linebreak-ext=md]
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
      - id: check-json
      - id: check-added-large-files
        args: [--maxkb=1024]
      - id: check-merge-conflict
      - id: check-case-conflict
      - id: check-illegal-windows-names
      - id: detect-private-key
      - id: mixed-line-ending
        args: [--fix=lf]
      - id: debug-statements
      - id: name-tests-test
        args: [--pytest-test-first]
        exclude: ^tests/(conftest|fakes)\.py$|^tests/tooling/conftest\.py$

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.3
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/astral-sh/uv-pre-commit
    rev: 0.12.5
    hooks:
      - id: uv-lock

  - repo: https://github.com/adrienverge/yamllint
    rev: v1.38.0
    hooks:
      - id: yamllint

  # Project-wide checks run from the uv environment (they need the project installed).
  - repo: local
    hooks:
      - id: import-linter
        name: import-linter (architecture contracts)
        entry: uv run lint-imports
        language: system
        pass_filenames: false
        always_run: true
      - id: ty-ratchet
        name: ty ratchet (diagnostics must not grow)
        entry: uv run python scripts/ty_ratchet.py
        language: system
        pass_filenames: false
        always_run: true
      - id: validate-skills
        name: validate agent skills
        entry: uv run python scripts/validate_skills.py
        language: system
        pass_filenames: false
        files: ^\.agents/skills/
      - id: sync-skills
        name: skill copies in .claude/skills are current
        entry: uv run python scripts/sync_skills.py --check
        language: system
        pass_filenames: false
        files: ^(\.agents|\.claude)/skills/

ci:
  autofix_prs: false
  autoupdate_schedule: monthly
  skip: [import-linter, ty-ratchet, validate-skills, sync-skills]
```

- [ ] **Step 3: Run everything**

Run: `uv run yamllint . && uv run pre-commit run --all-files`
Expected: every hook `Passed` (or `Skipped` when no matching files, e.g. sync/validate before
the skills exist). If `ruff-check`/`trailing-whitespace`/`end-of-file-fixer` modify files, re-run
until clean and include the changes in the commit.

- [ ] **Step 4: Install the hooks locally and commit**

```bash
uv run pre-commit install --install-hooks
git add -A
git commit -m "ci: add pre-commit hooks and yamllint configuration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Container image

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `compose.yaml`

- [ ] **Step 1: Write `.dockerignore`**

```dockerignore
.git
.github
.venv
**/__pycache__
**/*.pyc
.pytest_cache
.pytest-tmp
.ruff_cache
.import_linter_cache
build
dist
*.egg-info
.env
runs
data
tests
docs
examples
compliance
.agents
.claude
.vscode
.idea
*.md
!README.md
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1.7
# Runtime image for the `mads` CLI.
# Build: docker build -t mads:dev .            (add --build-arg EXTRAS=boosting for xgboost/lightgbm/catboost)
# Run:   docker compose run --rm mads catalog   (see compose.yaml for the mounted directories)

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
# One optional extra to install (e.g. "boosting"); empty installs the minimal catalog.
ARG EXTRAS=""

# 1) Dependencies only — this layer is reused until pyproject.toml or uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-install-project --no-dev --no-editable ${EXTRAS:+--extra $EXTRAS}

# 2) The project itself (non-editable, so the venv is self-contained).
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable ${EXTRAS:+--extra $EXTRAS}

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
RUN groupadd --system --gid 999 mads \
    && useradd --system --gid 999 --uid 999 --create-home mads \
    && mkdir -p /app/runs /app/data /app/examples \
    && chown -R mads:mads /app
WORKDIR /app
COPY --from=builder --chown=mads:mads /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER mads
ENTRYPOINT ["mads"]
CMD ["--help"]
```

- [ ] **Step 3: Write `compose.yaml`**

```yaml
services:
  mads:
    build:
      context: .
      args:
        EXTRAS: ${MADS_DOCKER_EXTRAS:-}
    image: mads:dev
    env_file:
      - path: .env
        required: false
    volumes:
      - ./runs:/app/runs
      - ./data:/app/data:ro
      - ./examples:/app/examples:ro
    # Example:
    #   docker compose run --rm mads run examples/cases/caso_titanic_3.json --output runs/titanic
```

- [ ] **Step 4: Build and smoke-test**

Run: `docker build -t mads:dev . && docker run --rm mads:dev --help && docker run --rm mads:dev catalog | head -5`
Expected: build succeeds; `--help` prints the argparse usage; `catalog` lists algorithms (the
boosting ones are absent unless `EXTRAS=boosting`).

- [ ] **Step 5: Lint and commit**

Run: `uv run yamllint compose.yaml`

```bash
git add Dockerfile .dockerignore compose.yaml
git commit -m "build(docker): add a multi-stage uv image and compose file

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: CI workflows and PR-Agent configuration

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/pr-agent.yml`, `.pr_agent.toml`

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main, dev]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  UV_LOCKED: "1"

jobs:
  lock-check:
    name: uv.lock is current
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          enable-cache: true
      - run: uv lock --check

  quality:
    name: lint, types, architecture, skills, build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          enable-cache: true
      - run: uv sync --locked --all-groups
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run yamllint .
      - run: uv run lint-imports
      - run: uv run python scripts/ty_ratchet.py --strict
      - run: uv run python scripts/validate_skills.py
      - run: uv run python scripts/sync_skills.py --check
      - name: Build and check the wheel contents
        run: |
          uv build
          uv run python -c "import glob, zipfile; w = glob.glob('dist/*.whl')[0]; names = zipfile.ZipFile(w).namelist(); assert 'mads/policy_packs/fraude_aml.json' in names, names; print('wheel OK', w)"

  test:
    name: tests (fast lane) ${{ matrix.os }} py${{ matrix.python-version }}
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: ["3.11", "3.13"]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          enable-cache: true
          python-version: ${{ matrix.python-version }}
      - run: uv sync --locked --group test
      - run: uv run pytest -m "not slow" -p no:cacheprovider

  test-full:
    name: tests (full suite, coverage)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          enable-cache: true
          python-version: "3.13"
      - run: uv sync --locked --group test
      - run: uv run pytest --cov --cov-report=xml --cov-report=term -p no:cacheprovider
      - uses: actions/upload-artifact@v4
        with:
          name: coverage-xml
          path: coverage.xml
```

(`actions/upload-artifact@v4` is a moving major tag — acceptable for an artifact upload; if the
executor can resolve a SHA with `gh api repos/actions/upload-artifact/git/ref/tags/v4`, pin it.)

- [ ] **Step 2: Write `.github/workflows/pr-agent.yml`**

```yaml
name: PR Agent

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  issue_comment:

jobs:
  pr-agent:
    if: ${{ github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    permissions:
      issues: write
      pull-requests: write
      contents: write
      checks: write
    steps:
      - name: Check whether an LLM key is configured
        id: key
        run: echo "present=${{ secrets.OPENAI_KEY != '' }}" >> "$GITHUB_OUTPUT"
      - name: Run PR Agent
        if: steps.key.outputs.present == 'true'
        uses: the-pr-agent/pr-agent@v0.42.0
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - name: Explain why nothing ran
        if: steps.key.outputs.present != 'true'
        run: echo "Set the OPENAI_KEY repository secret to enable automated PR review."
```

- [ ] **Step 3: Write `.pr_agent.toml`**

```toml
# PR-Agent (https://github.com/The-PR-Agent/pr-agent) repository configuration.
# Only overrides are listed; everything else uses the tool defaults.

[config]
response_language = "en-US"

[pr_reviewer]
require_tests_review = true
require_security_review = true
require_estimate_effort_to_review = true
extra_instructions = """
Review against AGENTS.md ("Code review rules"). In particular: an LLM output must never
become an authorization (only PolicyGate authorizes); skills must not import the orchestrator;
audit code reads persisted artifacts only; no secrets or PII in traces; new behaviour needs
tests in the right lane (unit vs slow/integration); CHANGELOG.md [Unreleased] is updated for
user-visible changes; everything new is written in English.
"""

[pr_description]
enable_pr_diagram = true
publish_labels = false

[pr_code_suggestions]
extra_instructions = """
Prefer suggestions that keep runtime behaviour unchanged and respect the import-linter
layers. Do not suggest reformatting; ruff handles formatting.
"""

[ignore]
glob = ["uv.lock", ".claude/skills/**", "data/**", "runs/**", ".ty-baseline.json"]
```

- [ ] **Step 4: Validate**

Run: `uv run yamllint .github && uv run python -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('.pr_agent.toml').read_text()); print('toml OK')"`
Expected: no yamllint errors (warnings about line length are acceptable), `toml OK`.

- [ ] **Step 5: Commit**

```bash
git add .github .pr_agent.toml
git commit -m "ci: add GitHub Actions pipeline and PR-Agent configuration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Agent skills (canonical, `.agents/skills/`)

Tasks 12a–12i are independent and can run in parallel (one subagent each). They share the
house style below. **Before writing, the orchestrating agent loads `superpowers:writing-skills`
and forwards its requirements to each implementer.**

**House style for every `SKILL.md`:**
- Line 1 is `---`. Frontmatter: `name`, `description` (one paragraph, third person, starts with
  `Use when …`, lists concrete triggers, ≤ 400 characters), `metadata:` with `  version: "1.0.0"`.
- Sections in this order: `# <Title>`, `## When to use` (bullets, include "not for"),
  `## Quick start` (the 1–4 commands), `## Rules` (numbered, each rule one sentence + one
  sentence of *why*), `## Workflow` (ordered steps), `## In this repository` (paths, recipes,
  conventions specific to MADS), `## Related skills` (names only).
- ≤ 300 lines; no time-sensitive statements other than a single `Versions checked: 2026-08`
  line where versions matter; forward slashes; reference files one level deep; one default
  per decision (no menus of alternatives).
- Scripts: stdlib-only, `main() -> int`, `--help`, `--json` where output is structured,
  friendly errors, exit 1 on findings when a `--fail-*` flag is set. Tests live in
  `tests/tooling/test_<script>.py` and import via `load_script(".agents/skills/<name>/scripts/<file>.py")`.
- After writing: `uv run python scripts/validate_skills.py` must pass; `uv run ruff check
  .agents` clean; the subagent reads the skill once more as a stranger and removes anything
  the model would already know.
- Commit message pattern: `feat(skills): add <name> skill`.

#### Task 12a: `packaging-scaffolding`

**Files:** `.agents/skills/packaging-scaffolding/SKILL.md`, `references/pyproject-anatomy.md`

- [ ] Write the description: `Use when creating a new Python package or module tree, converting a flat layout to src-layout, editing pyproject.toml (dependencies, dependency groups, extras, build backend, entry points), or regenerating uv.lock. Covers this repository's uv + uv_build + src/mads conventions and how to verify a change with uv lock, uv sync and uv build.`
- [ ] Rules to encode: (1) runtime deps go in `[project.dependencies]`, published optional features in `[project.optional-dependencies]`, local tooling in `[dependency-groups]` — never the other way round; (2) every dependency gets an upper bound at the next major (`>=x.y,<x+1`) unless the project is a library; (3) after any `pyproject.toml` edit run `uv lock` then `uv sync --locked`, and commit `uv.lock` in the same commit; (4) new subpackages need `__init__.py`; data files live inside the package tree and are verified in the wheel (`uv build` + zip listing); (5) new console scripts are declared in `[project.scripts]` and smoke-tested with `uv run <name> --help`; (6) `requires-python` is a compatibility floor — do not raise it to match the dev pin in `.python-version`; (7) a new top-level `mads` module must be placed into the import-linter `layers` list (see `check-imports`).
- [ ] `references/pyproject-anatomy.md`: an annotated walk through the repository's actual `pyproject.toml` sections (copy the relevant TOML blocks and explain each), plus the `uv` command table (`uv add`, `uv add --group`, `uv add --optional`, `uv remove`, `uv lock --upgrade-package`, `uv tree`, `uv build`).
- [ ] Verify with `just validate-skills`; commit.

#### Task 12b: `python-god-classes`

**Files:** `.agents/skills/python-god-classes/SKILL.md`, `scripts/detect_god_classes.py`, `references/refactoring-playbook.md`, `tests/tooling/test_detect_god_classes.py`

- [ ] Description: `Use when a class or module has grown too large or mixes responsibilities (many methods, 300+ lines, hard to summarise in one sentence), when asked to split, decompose or refactor a big class, or before adding more behaviour to one. Detects god classes with an AST metrics script and guides a safe, test-covered extraction into single-responsibility components.`
- [ ] Write the failing tests first:

```python
from __future__ import annotations

import json
from pathlib import Path

from tests.tooling.conftest import load_script

BIG = "class Big:\n" + "".join(
    f"    def m{i}(self):\n        self.a{i % 3} = {i}\n        return self.a{i % 3}\n" for i in range(20)
)
SMALL = "class Small:\n    def __init__(self):\n        self.x = 1\n    def get(self):\n        return self.x\n"
DATACLASS = "from dataclasses import dataclass\n@dataclass\nclass Point:\n    x: int\n    y: int\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_flags_big_class_only(tmp_path):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "mod.py", BIG + "\n" + SMALL + "\n" + DATACLASS)
    report = detect.analyse_paths([tmp_path], detect.Thresholds(max_methods=15, max_class_lines=300, max_module_lines=600, max_module_defs=25, min_cohesion=0.3))
    flagged = [c.name for c in report.classes if c.flags]
    assert flagged == ["Big"]
    big = next(c for c in report.classes if c.name == "Big")
    assert big.methods == 20
    assert 0.0 <= big.cohesion <= 1.0


def test_flags_long_module(tmp_path):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "long.py", "".join(f"def f{i}():\n    return {i}\n\n" for i in range(30)))
    report = detect.analyse_paths([tmp_path], detect.Thresholds(max_methods=15, max_class_lines=300, max_module_lines=600, max_module_defs=25, min_cohesion=0.3))
    assert any("top-level definitions" in flag for m in report.modules for flag in m.flags)


def test_cli_json_and_exit_code(tmp_path, capsys):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "mod.py", BIG)
    assert detect.main([str(tmp_path), "--json", "--fail-over"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["classes"][0]["name"] == "Big"
    assert detect.main([str(tmp_path), "--json", "--max-methods", "50"]) == 0
```

- [ ] Implement `detect_god_classes.py` with: `@dataclass(frozen=True) class Thresholds(max_methods=15, max_class_lines=300, max_module_lines=600, max_module_defs=25, min_cohesion=0.3)`; `@dataclass class ClassMetrics(file, name, line, lines, methods, public_methods, attributes, cohesion, flags: list[str])`; `@dataclass class ModuleMetrics(file, lines, top_level_defs, flags)`; `@dataclass class Report(classes, modules)` with `to_dict()`; `analyse_file(path, thresholds)`, `analyse_paths(paths, thresholds) -> Report` (walk `*.py`, skip `.venv`, `__pycache__`); cohesion = mean over methods of (attributes the method touches / all attributes of the class), 1.0 when a class has < 2 methods or no attributes; classes decorated with `dataclass`/`NamedTuple`/`TypedDict`/`Enum` bases are measured but only flagged on `max_class_lines`; flags are strings such as `"20 methods > 15"`, `"cohesion 0.21 < 0.30"`, `"812 lines > 600"`, `"30 top-level definitions > 25"`; table output sorted by number of flags then lines; `--json`, `--top N`, `--fail-over`, threshold flags.
- [ ] Run the tests, then the script on the repo: `uv run python .agents/skills/python-god-classes/scripts/detect_god_classes.py src/mads` — expected to list `GenerateAnalyticalReportSkill`, `AgentState`, `AgenticOrchestrator` (by lines) among others; paste the top five into `references/refactoring-playbook.md` as the worked example.
- [ ] Rules to encode: detect mechanically first; cohesion beats raw size (data classes are not god classes); characterization tests before any extraction; Extract Class along data seams shown by cohesion, Extract Method for long procedures; one extraction per commit with the full suite green; re-run the detector and require the aggregate to go down; thresholds are documented defaults to tune, not laws; don't flag visitor-style classes of one-line delegators. `references/refactoring-playbook.md`: the extraction recipes (Extract Class, Extract Method, Introduce Parameter Object, Replace Conditional with Strategy) with a before/after sketch each, and the commit `166fbca` (AgenticOrchestrator split into eight coordinators) as the repo precedent plus the detector's current top findings.
- [ ] Commit.

#### Task 12c: `python-debug`

**Files:** `.agents/skills/python-debug/SKILL.md`, `scripts/bisect_test.py`, `tests/tooling/test_bisect_test.py`

- [ ] Description: `Use when a test fails, a stack trace appears, behaviour is unexpected or a bug is reported — before proposing any fix. Systematic reproduce → isolate → hypothesise → fix → regression-test workflow for Python, including pdb/breakpoint, structured logging, git bisect and flaky-test triage in this LangGraph/LiteLLM code base.`
- [ ] Test first: `build_commands("v1.0", "HEAD", ["tests/test_x.py::test_y"])` returns `[["git","bisect","start","HEAD","v1.0"], ["git","bisect","run", sys.executable, "-m", "pytest", "-x", "-q", "tests/test_x.py::test_y"], ["git","bisect","reset"]]`; `main(["--dry-run","v1.0","HEAD","--","tests/test_x.py"])` prints the three commands and returns 0.
- [ ] Implement `bisect_test.py` (`good`, `bad`, `--`, pytest args; `--dry-run`; runs the commands with `subprocess.run(check=False)` and always runs `git bisect reset` at the end, even on failure).
- [ ] Rules: never fix before reproducing; one hypothesis, one change; regression test first and watch it fail; prefer `breakpoint()` over more than three prints; in orchestrator runs use the trace (`runs/<name>/trace.jsonl`, `mads status`, `mads verify`) and `DeterministicTestProvider` from `tests/fakes.py` instead of real LLM calls; bisect when the suspect range exceeds ~5 commits; never "fix" flakiness with sleeps or retries; confirm the minimal repro matches the original report. Mention `pytest -x --lf`, `-k`, `--pdb`, `PYTHONWARNINGS`, and `uv run python -X dev`.
- [ ] Commit.

#### Task 12d: `python-testing`, `python-testing-unit`, `python-testing-integration`

**Files:** three `SKILL.md` files.

- [ ] Descriptions:
  - `python-testing`: `Use when deciding which kind of test to write, where it goes, which pytest marker applies, or how to run the suite (fast lane vs full). Routes to python-testing-unit and python-testing-integration and states this repository's lanes: default pytest = everything, just test = not slow, just test-all = full.`
  - `python-testing-unit`: `Use when writing or reviewing fast, isolated pytest tests for one function or class with no filesystem, network, model training or LLM calls. Covers fixtures, parametrize, fakes versus mocks at architectural boundaries, hypothesis for numeric/encoding code, and what disqualifies a test from the unit lane.`
  - `python-testing-integration`: `Use when a test needs the on-disk ArtifactStore, real scikit-learn training, a full orchestrator run, the CLI, or any other slow or environment-dependent boundary. Covers the slow and integration markers, the deterministic LLM fake, tmp_path isolation, cleanup, and flakiness rules.`
- [ ] `python-testing` is a router (≤ 120 lines): decision table (unit vs integration vs slow), lane commands (`just test`, `just test-all`, `just test-slow`, `uv run pytest -m "not slow" -k name`, `-n auto` opt-in), marker rules (`pytestmark` at module level; `--strict-markers`), naming (`tests/test_<module>.py`; `tests/tooling/` for scripts), the rule "no PR lowers coverage" (`just test-cov`), and links to the two children.
- [ ] `python-testing-unit` rules: mock at the boundary (LLM provider, filesystem, clock), never private helpers; >2 mocks means the unit is too big (hand off to `python-god-classes`); `parametrize` over loops; narrowest fixture scope; hypothesis for `numeric.py`, `encoding.py`, `outlier_bounds.py`-style transforms; assert behaviour not call counts; snapshots last resort; use `tests/fakes.py::DeterministicTestProvider` rather than `unittest.mock` for the LLM.
- [ ] `python-testing-integration` rules: integration tests must be excludable (`slow` for full runs, `integration` for store/training/CLI); `synthetic_case` and `policy_pack` fixtures from `tests/conftest.py`; every run writes under `tmp_path` only; never reuse a `runs/` directory (`--force` semantics); assert on persisted outcomes (`trace.jsonl`, `manifest.json`, artifacts) not on coordinator call order; no real network (LiteLLM is monkeypatched); quarantine flaky tests with a marker and an owner, never retry loops; seeds are explicit.
- [ ] Commit all three.

#### Task 12e: `ruff` and `ty`

**Files:** two `SKILL.md` files.

- [ ] Descriptions:
  - `ruff`: `Use when linting, formatting, sorting imports, fixing style findings, adding a noqa, or changing [tool.ruff] in pyproject.toml. Covers the commands, the fix-versus-suppress decision, safe versus unsafe fixes, and how new rule categories are rolled out on this legacy code base.`
  - `ty`: `Use when type-checking, adding annotations, triaging ty diagnostics, editing ty.toml, or when the ty ratchet fails in CI or pre-commit. Explains the baseline ratchet (scripts/ty_ratchet.py), rule severities, suppression comments, and ty's pre-1.0 caveats.`
- [ ] `ruff` rules: `just lint` / `just lint-fix` / `just fmt`; never a bare `# noqa`, always `# noqa: CODE  # reason`; never `--unsafe-fixes` unattended; formatter runs after fixes; new categories are enabled one at a time with `--statistics` first; deferred list (D, ANN, PLR, ERA) and the procedure to enable one (count → fix → enable → commit); per-file ignores are for *kinds* of files (tests, CLI), not for silencing one module; the config lives only in `pyproject.toml`.
- [ ] `ty` rules: `just typecheck` (report) vs `just typecheck-ratchet` (gate); how to read `.ty-baseline.json`; after fixing diagnostics run `uv run python scripts/ty_ratchet.py --update-baseline` and commit the file; suppress with `# ty: ignore[rule]` only with a reason; `ty.toml` is the only config (no `[tool.ty]`); new code must not add diagnostics; prefer precise annotations over `Any`; `python-version` in `ty.toml` follows `requires-python`, not the dev pin; ty is beta — upgrade deliberately (`typecheck` group pins it) and re-baseline in the same commit.
- [ ] Commit both.

#### Task 12f: `check-imports`

**Files:** `.agents/skills/check-imports/SKILL.md`, `scripts/import_graph.py`, `tests/tooling/test_import_graph.py`

- [ ] Description: `Use when adding an import between mads subpackages, creating a new top-level module, when uv run lint-imports fails, or when evolving the architecture layers toward the execution/assurance split. Explains the import-linter contracts, how to read a violation, when to fix versus ignore_imports, and how to extend the layers list.`
- [ ] Tests first: a tmp package `pkg/{a,b,c}.py` where `a` imports `b`, `b` imports `c`, `c` imports `a`; `build_graph(tmp_path / "pkg") == {("a","b"), ("b","c"), ("c","a")}`; `violations(graph, layers=[["a"], ["b"], ["c"]])` returns `[("c","a")]`; `main([str(pkg), "--layers", "a;b;c", "--json"])` prints JSON with `edges` and `violations`.
- [ ] Implement `import_graph.py`: AST walk of `Import`/`ImportFrom` (absolute `pkg.x` and relative), node = first module segment under the package, `--layers "cli;console;decisions|policies|risk;..."` (`;` separates layers top→bottom, `|` independent siblings), table or `--json`; exit 1 with `--fail-on-violation`.
- [ ] Rules: start from the contract names in `pyproject.toml`; a violation is a design signal — fix by moving the dependency down (protocol/contract in a lower layer) rather than reordering layers; `ignore_imports` only with `TODO(arch)` and an issue; `exclude_type_checking_imports` means `TYPE_CHECKING` imports are free — use them for annotations; new module → add to exactly one layer line (`exhaustive = true` will fail otherwise); the target layout (`kernel`, `knowledge`, `execution | assurance`, `runtime`, `cli | mcp`) and the exact future contracts from ADR-0001; `uv run lint-imports --verbose` for timings/details.
- [ ] Commit.

#### Task 12g: `dockerfile`

**Files:** `.agents/skills/dockerfile/SKILL.md`, `references/uv-docker-patterns.md`

- [ ] Description: `Use when building, editing or debugging the container image, compose.yaml or .dockerignore, or when an image is too large, rebuilds too slowly or ships files it should not. Covers the uv multi-stage pattern, lockfile-frozen installs, non-root runtime, the EXTRAS build argument and the mounted runs/data directories.`
- [ ] Rules: dependencies layer before source (`--no-install-project`); `--locked` always, never re-resolve in the image; `--no-editable` for the final sync; `.venv` in `.dockerignore`; runtime stage gets only `/app/.venv`; non-root user; pin `uv` and the base image (note digest pinning for releases); one extra via `EXTRAS`; runs/data/examples are volumes, never baked in; `docker compose run --rm mads <args>` is the way to run. `references/uv-docker-patterns.md`: annotated copy of the repository Dockerfile, cache-mount explanation, how to add a system package (`apt-get` in builder only), how to debug (`docker run --rm --entrypoint sh mads:dev`), image-size checklist.
- [ ] Commit.

#### Task 12h: `task-runner`

**Files:** `.agents/skills/task-runner/SKILL.md`

- [ ] Description: `Use when adding or changing a justfile recipe or Makefile target, when unsure which command runs lint, tests, type-checks or the skill sync, or when a recipe behaves differently on Windows. States that the justfile is canonical and cross-platform (PowerShell on Windows), the Makefile forwards to it, and recipes stay one-liners that call uv run or scripts/.`
- [ ] Rules: justfile canonical, Makefile never gains logic; one command per recipe line (each line is its own shell); anything needing pipes, conditionals or `cd` becomes a `scripts/*.py`; recipe names are verb-first kebab-case and grouped; every recipe has `[doc("...")]`; `just check` is the pre-push gate and must stay fast; `default` lists recipes; test a new recipe on both shells (`just <recipe>` in PowerShell and Git Bash); `{{ARGS}}` recipes use `*ARGS`.
- [ ] Commit.

#### Task 12i: `changelog`

**Files:** `.agents/skills/changelog/SKILL.md`, `scripts/check_changelog.py`, `tests/tooling/test_check_changelog.py`

- [ ] Description: `Use before committing a user-visible change, when preparing a release, or when asked to update CHANGELOG.md. Keep a Changelog 1.1.0 sections, the mapping from Conventional Commit types to sections, the release checklist (version bump in pyproject.toml, tag, compare links) and the CI check that [Unreleased] was touched.`
- [ ] Tests first (use a temporary git repo created with `git init -q`, `git -c user.name=t -c user.email=t@t commit`): `changed_files(repo, base_ref)` lists files changed since base; `needs_entry(changed)` is True when any path starts with `src/` and False for `tests/`, `docs/`, `scripts/` only; `main(["--repo", repo, "--base", "HEAD~1"])` returns 1 when `src/x.py` changed without `CHANGELOG.md`, 0 when both changed, and 0 when the last commit message contains `[skip changelog]`.
- [ ] Implement `check_changelog.py` (`--repo`, `--base` default `origin/main`, `--changelog` default `CHANGELOG.md`, skip token `[skip changelog]`).
- [ ] Rules: changelog is for users, commits for maintainers — curate, don't dump; type→section map (`feat`→Added, `fix`→Fixed, `perf`→Changed, breaking→**Breaking** callout under Changed/Removed, `docs/test/chore/ci/build/refactor`→omitted unless user-visible); every PR touching `src/` edits `[Unreleased]` or says `[skip changelog]` in the commit; release steps (move entries, bump `[project].version`, `uv lock`, tag `vX.Y.Z`, update compare links); newest first; link references at the bottom.
- [ ] Commit.

---

### Task 13: Claude Code integration — synced skills, settings, format hook

**Files:**
- Create: `scripts/hooks/format_on_edit.py`, `tests/tooling/test_format_on_edit.py`, `.claude/settings.json`, `.claude/skills/**` (generated)

- [ ] **Step 1: Test first** `tests/tooling/test_format_on_edit.py`

```python
from __future__ import annotations

import json

from tests.tooling.conftest import load_script


def test_extracts_python_file_inside_root(tmp_path):
    hook = load_script("scripts/hooks/format_on_edit.py")
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("x=1\n", encoding="utf-8")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}, "cwd": str(tmp_path)}
    assert hook.target_file(payload, tmp_path) == target


def test_ignores_non_python_and_outside_files(tmp_path):
    hook = load_script("scripts/hooks/format_on_edit.py")
    assert hook.target_file({"tool_input": {"file_path": str(tmp_path / "a.md")}}, tmp_path) is None
    assert hook.target_file({"tool_input": {"file_path": "C:/elsewhere/a.py"}}, tmp_path) is None
    assert hook.target_file({"tool_input": {}}, tmp_path) is None


def test_main_formats_file_and_exits_zero(tmp_path, monkeypatch):
    hook = load_script("scripts/hooks/format_on_edit.py")
    target = tmp_path / "scripts" / "y.py"
    target.parent.mkdir(parents=True)
    target.write_text("import os,sys\nx=( 1 )\n", encoding="utf-8")
    monkeypatch.setenv("FORMAT_ON_EDIT_ROOT", str(tmp_path))
    stdin = json.dumps({"tool_input": {"file_path": str(target)}})
    assert hook.main(stdin) == 0
    assert target.read_text(encoding="utf-8") == "x = 1\n"
```

- [ ] **Step 2: Implement `scripts/hooks/format_on_edit.py`**

```python
"""Claude Code PostToolUse hook: format the Python file that was just edited.

Reads the hook payload from stdin, and if `tool_input.file_path` is a `.py` file inside the
repository (or FORMAT_ON_EDIT_ROOT), runs `ruff check --fix` and `ruff format` on it. Always
exits 0 so a formatting problem never blocks the agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def target_file(payload: dict, root: Path) -> Path | None:
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw or not str(raw).endswith(".py"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def ruff_command() -> list[str]:
    local = Path(sys.executable).with_name("ruff.exe" if os.name == "nt" else "ruff")
    if local.exists():
        return [str(local)]
    found = shutil.which("ruff")
    return [found] if found else ["uv", "run", "ruff"]


def main(stdin_text: str | None = None) -> int:
    root = Path(os.environ.get("FORMAT_ON_EDIT_ROOT", REPO_ROOT))
    try:
        payload = json.loads(stdin_text if stdin_text is not None else sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    path = target_file(payload, root)
    if path is None:
        return 0
    ruff = ruff_command()
    for args in (["check", "--fix", "--quiet", str(path)], ["format", "--quiet", str(path)]):
        subprocess.run([*ruff, *args], cwd=root, check=False, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the tests** — `uv run pytest tests/tooling/test_format_on_edit.py -q` → `3 passed`.

- [ ] **Step 4: Write `.claude/settings.json`**

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run:*)",
      "Bash(uv sync:*)",
      "Bash(uv lock:*)",
      "Bash(uv build)",
      "Bash(just:*)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)"
    ]
  },
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "uv run python scripts/hooks/format_on_edit.py",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 5: Generate the copies and validate**

Run: `uv run python scripts/validate_skills.py && uv run python scripts/sync_skills.py && uv run python scripts/sync_skills.py --check && ls .claude/skills`
Expected: `12 skill(s) valid, 0 invalid`, `Synced 12 skill(s)…`, `Skill copies are in sync`, 12 directories + `README.md`.

- [ ] **Step 6: Commit**

```bash
git add .claude scripts/hooks tests/tooling/test_format_on_edit.py
git commit -m "feat(agents): Claude Code settings, format-on-edit hook and synced skill copies

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: `AGENTS.md`, pointers, `CONTRIBUTING.md`, README banner

**Files:**
- Create: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CONTRIBUTING.md`
- Modify: `README.md` (prepend banner)

- [ ] **Step 1: Write `AGENTS.md`** (≤ 200 lines, < 32 KiB). Required sections and content:

```markdown
# AGENTS.md — MADS

MADS is an auditable multi-agent data-science system (master's thesis). An LLM may *propose*
an action; only deterministic code — the risk classifier, the policy pack and `PolicyGate` —
may *authorize* it. Every run leaves a hash-chained trace that the meta-auditor re-checks.

**Language policy.** Everything new (code, comments, docstrings, docs, commits) is written in
English. Existing Spanish code and docs are legacy and are being translated; do not mix
languages inside a file you touch — translate the parts you change.

## Commands (justfile is canonical; `make <target>` forwards to `just <target>`)

| Command | What it does |
|---|---|
| `just setup` / `just hooks` | install all groups from `uv.lock` / install pre-commit hooks |
| `just test` / `just test-all` / `just test-slow` | fast lane (`-m "not slow"`) / full suite (minutes) / only slow |
| `just lint` / `just lint-fix` / `just fmt` | ruff check + format check + yamllint / apply safe fixes / format |
| `just typecheck` / `just typecheck-ratchet` | ty report / gate: diagnostics must not grow |
| `just check-imports` | import-linter architecture contracts |
| `just check` | pre-push gate: lint, ratchet, imports, skills, fast tests |
| `just sync-skills` / `just validate-skills` | regenerate `.claude/skills` / validate SKILL.md files |
| `just mads <args>` | run the CLI, e.g. `just mads catalog` |
| `just docker-build` / `just docker-run <args>` | image / run inside the container |

Use `uv run …` for anything else; never call the global `python`.

## Repository map
(one line each: src/mads subpackages with purpose, tests, scripts, .agents/skills, .claude, docs, compliance, data, examples, runs)

## Architecture — current invariants (machine-checked by `just check-imports`)
- Layers, highest first: cli → console → orchestrator → audit → workers → gate → (decisions | policies | risk) → skills → (contracts | artifacts | tracing) → (llm | model_catalog | rag) → utils.
- `mads.skills` never imports the orchestrator; `mads.audit` never imports the orchestrator (it reads `trace.jsonl` and `artifacts/manifest.json` from disk).
- "Skill" in `src/mads/skills/` means a versioned data-science pipeline step (`Skill` protocol, `skills/registry.py`). It is unrelated to the agent skills in `.agents/skills/`.
- `PolicyGate` (`gate.py`) is the only place that turns risk + policy into allow / needs_approval / blocked; LLM confidence is never authorization.
- LiteLLM (`llm/litellm_provider.py`) is the only model integration; keys come from `.env`, are never logged.
- Trace events are hash-chained; prompts/outputs are redacted (`utils.redact`) before tracing; chain-of-thought is never persisted.

## Architecture — target (see docs/adr/0001-harness-and-two-layer-architecture.md)
Two orchestrators on LangGraph + LiteLLM: an execution layer (orchestrator + code/statistics/experiment sub-agents) and a latent assurance layer (audit orchestrator + sub-agents querying an EU AI Act / credit-risk regulation knowledge base) that can Block / RequireChanges / Annotate through the runtime's control channel. Planned packages: `mads.kernel`, `mads.knowledge`, `mads.execution`, `mads.assurance`, `mads.runtime`, `mads.cli`, `mads.mcp`. Do not create them yet; when you do, extend the import-linter layers as described in the ADR and the `check-imports` skill.

## Conventions
- Python ≥ 3.11 syntax (`X | None`, `match` allowed); dev interpreter 3.13 (`.python-version`).
- ruff (`line-length = 100`), Google-style docstrings in English, explicit exception types, `pathlib`, no `print` outside `cli.py`/`console.py`/scripts.
- ty: new code adds zero diagnostics; run `just typecheck-ratchet`.
- Tests: `tests/test_<module>.py`; module-level `pytestmark = pytest.mark.slow` for full runs, `integration` for store/training/CLI; LLM calls use `tests/fakes.py::DeterministicTestProvider`; never the network.
- Commits: Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`, `refactor: …`, `test: …`, `build: …`, `ci: …`, `chore: …`). Branches: `feat/…`, `fix/…`, `docs/…`.
- `CHANGELOG.md` `[Unreleased]` is updated for user-visible changes (or the commit says `[skip changelog]`).
- After editing `pyproject.toml`: `uv lock` and commit `uv.lock`.
- Never commit `.env`, `runs/`, `data/creditcard.csv`, or anything under `.claude/` except `settings.json` and the generated `skills/`.

## Agent skills (canonical: `.agents/skills/`; Claude Code reads the generated `.claude/skills/`)
(12 lines: `name` — trigger summary)
How tools load them: Codex, Cursor and Gemini CLI read `.agents/skills/` natively; Claude Code reads `.claude/skills/` (run `just sync-skills` after editing a skill; CI fails on drift). Gemini CLI asks for confirmation the first time a skill activates.

## Gotchas
- Windows: `core.symlinks=false` — never create symlinks; `just` runs recipes in PowerShell; use `uv run python scripts/...` for anything with pipes.
- The full suite trains real models (~3.5 min). Use `just test` while iterating, `just test-all` before pushing.
- ty is pre-1.0 (pinned); a ty upgrade goes with `--update-baseline` in the same commit.
- `README.md` and `docs/*.md` are Spanish legacy; `AGENTS.md`, `CONTRIBUTING.md`, ADRs and specs are the English sources of truth.

## Code review rules
- Reject any change where an LLM output (selection, confidence, free text) becomes an authorization or bypasses `PolicyGate`.
- Reject imports that break the layers or the two forbidden contracts; do not accept new `ignore_imports` without a `TODO(arch)`.
- Require tests in the right lane; a behaviour change without a test is incomplete.
- Require English; require `CHANGELOG.md` for user-visible changes; require `uv.lock` alongside `pyproject.toml` changes.
- Prefer small, single-responsibility classes; point at `python-god-classes` when a class grows.
```

Fill the "(…)" placeholders with real one-line entries derived from the repository (the
architecture brief at `C:/Users/lucas/.claude/jobs/7432cb24/tmp/research/a1-architecture-brief.md`
and `a2-package-analysis.json` list each subpackage's purpose).

- [ ] **Step 2: Write `CLAUDE.md`**

```markdown
@AGENTS.md

## Claude Code specifics
- A `PostToolUse` hook (`.claude/settings.json`) runs ruff on every Python file you edit; you do not need to format manually.
- `.claude/skills/` is generated from `.agents/skills/` — edit the canonical copy and run `just sync-skills`.
- `.claude/settings.json` is shared; put personal overrides in `.claude/settings.local.json` (gitignored).
```

- [ ] **Step 3: Write `GEMINI.md`**

```markdown
@AGENTS.md

## Gemini CLI specifics
- Skills are discovered from `.agents/skills/` (alias of `.gemini/skills/`); Gemini asks for confirmation the first time each skill activates.
- No `.gemini/settings.json` is required; this file is imported through the default `GEMINI.md` context file.
```

- [ ] **Step 4: Write `CONTRIBUTING.md`** — sections: Prerequisites (uv; `uv tool install rust-just`; Docker optional; Windows notes incl. `winget install ezwinports.make` only if you want `make`), Setup (`just setup`, `just hooks`, `.env`), Daily loop (`just test`, `just lint-fix`, `just check`), Test lanes, Architecture checks, Type-checking ratchet, Commit and PR conventions (Conventional Commits, CHANGELOG, one logical change per PR, CI must be green), Adding or editing an agent skill (write in `.agents/skills/<name>/`, `just validate-skills`, `just sync-skills`, commit both), Docker, Release checklist (from the `changelog` skill), Language policy.

- [ ] **Step 5: Prepend the README banner** (keep the rest untouched)

```markdown
> **Developers and coding agents:** this README is the original Spanish project description.
> The English sources of truth for working on the repository are [`AGENTS.md`](AGENTS.md)
> (conventions, commands, architecture invariants) and [`CONTRIBUTING.md`](CONTRIBUTING.md)
> (setup and workflow). Run `just setup && just check` to get started.

```

- [ ] **Step 6: Verify**

Run: `wc -l AGENTS.md && wc -c AGENTS.md && uv run python -c "import re,pathlib; t=pathlib.Path('AGENTS.md').read_text(encoding='utf-8'); assert '(' + '…' + ')' not in t and 'TODO' not in t; print('no placeholders')"`
Expected: ≤ 200 lines, < 32768 bytes, `no placeholders`. Also `grep -c "@AGENTS.md" CLAUDE.md GEMINI.md` → 1 each.

- [ ] **Step 7: Commit**

```bash
git add AGENTS.md CLAUDE.md GEMINI.md CONTRIBUTING.md README.md
git commit -m "docs(agents): add AGENTS.md with Claude/Gemini pointers and CONTRIBUTING.md

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: ADR-0001 and `CHANGELOG.md`

**Files:**
- Create: `docs/adr/0001-harness-and-two-layer-architecture.md`, `docs/adr/README.md`, `CHANGELOG.md`

- [ ] **Step 1: Write `docs/adr/README.md`** (what ADRs are, numbering, statuses Proposed/Accepted/Superseded, template link).

- [ ] **Step 2: Write ADR-0001** with sections: Status (Accepted 2026-08-20), Context (the two-layer
vision in the owner's words, the existing LangGraph + LiteLLM investment, the "should we adopt
the DeepSeek harness?" question), Decision (LangGraph runtime + LiteLLM gateway; `deepagents`
for deep-agent sub-agents; thin custom harness owning tool registry, hook/event bus,
intervention protocol, provenance ledger, policy gates; own Typer+Textual CLI/TUI as trust
boundary + MCP server as secondary surface), Alternatives considered (table from spec §7:
deepseek-harness, Claude Agent SDK, OpenAI Agents SDK, Pydantic AI, Google ADK, smolagents,
AG2, CrewAI — one row each with why not), Consequences, Target package layout with a
migration table (today's module → target package: `contracts, llm, tracing, artifacts,
policies, utils, gate → kernel`; `rag → knowledge`; `orchestrator, workers, decisions, risk,
skills → execution`; `audit → assurance`; `cli, console → cli`; new: `runtime`, `mcp`), and
the future import-linter contracts (verbatim from spec §7). Sources: the research report
`C:/Users/lucas/.claude/jobs/7432cb24/tmp/research/r3-harness-landscape.md` (copy its
Sources list into the ADR).

- [ ] **Step 3: Write `CHANGELOG.md`**

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Professional development scaffold: uv-managed `pyproject.toml` with PEP 735 dependency groups and the `uv_build` backend, `uv.lock`, `.python-version` (3.13).
- Quality gates: ruff (lint + format), ty with a diagnostic-count ratchet (`ty.toml`, `scripts/ty_ratchet.py`), import-linter architecture contracts, pre-commit hooks, yamllint.
- Task runners: `justfile` (canonical) and a forwarding `Makefile`.
- Container image (`Dockerfile`, `compose.yaml`) built with uv.
- GitHub Actions CI (lock check, quality, test matrix, full suite with coverage) and PR-Agent configuration.
- Agent instructions for Claude Code, Codex, Cursor and Gemini CLI (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) and twelve agent skills under `.agents/skills/` (synced to `.claude/skills/`).
- `CONTRIBUTING.md`, ADR-0001 (harness choice and two-layer architecture), design spec and plan under `docs/superpowers/`.
- Test lanes: `slow` and `integration` markers; `just test` runs the fast lane.

### Changed
- `langgraph` requirement tightened to `>=1.0,<2` (the version the lockfile already resolved); the `dev` extra became the `dev` dependency group.
- The whole code base was formatted once with ruff (blame-ignored commit, see `.git-blame-ignore-revs`).
- `.env.example` rewritten in English.
- `.claude/settings.json` and the generated `.claude/skills/` are tracked; personal Claude settings stay ignored.

### Deferred lint rules (ratchet)
- `D` (docstrings), `ANN` (annotations), `PLR` (complexity), `ERA` (commented-out code): to be enabled during the English translation pass. <!-- Task 3 appends any category it had to defer -->

## [0.1.0] - 2026-08-19

### Added
- Thesis skeleton: LangGraph agentic orchestrator with eight injectable coordinators, data-science skills catalogue, `PolicyGate` with JSON policy packs, hash-chained trace, meta-auditor with controls A1–A18, LiteLLM as the single model integration, CLI (`run`, `verify`, `audit`, `catalog`, `skills`, `runs`, `status`, `llm-test`) and observability console.

[unreleased]: https://github.com/OWNER/mads-msct/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/mads-msct/releases/tag/v0.1.0
```

Replace `OWNER` with the GitHub owner from `git remote get-url origin`.

- [ ] **Step 4: Commit**

```bash
git add docs/adr CHANGELOG.md
git commit -m "docs: add ADR-0001 (harness and two-layer architecture) and CHANGELOG

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: End-to-end verification and push

- [ ] Run, in order, and paste the outputs into the final report:
  1. `uv lock --check && uv sync --locked --all-groups`
  2. `just check` (lint, ratchet, imports, skills, fast tests)
  3. `uv run ty check | tail -1` (report the count) and `uv run python scripts/ty_ratchet.py --strict`
  4. `uv run pre-commit run --all-files`
  5. `uv run pytest -q -p no:cacheprovider | tail -1` → `484 passed`
  6. `make help`, `make lock-check` (Git Bash) and `just --list` (PowerShell)
  7. `docker build -t mads:dev . && docker run --rm mads:dev --help | head -3`
  8. `uv run python scripts/validate_skills.py && uv run python scripts/sync_skills.py --check`
  9. `wc -l AGENTS.md` ≤ 200; `git grep -nE "\b(TODO|TBD)\b" -- AGENTS.md CLAUDE.md GEMINI.md CONTRIBUTING.md .agents` → only deliberate `TODO(arch)`/`TODO(bug)` markers
  10. `git status --short` → clean
- [ ] Dispatch an independent reviewer (deep tier) over `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, every `SKILL.md` and the spec: check English, accuracy against the repository, no contradictions with `pyproject.toml`/`justfile`, skill descriptions trigger-first and distinct from each other. Fix findings, re-run `just check`, commit as `docs: address review findings`.
- [ ] `git push -u origin feat/professional-scaffold`
- [ ] Final report: what was done, the flagged decisions (format commit, `.claude/` tracking, langgraph pin), baseline numbers (ruff before/after, ty count, test count/time), the `F821` locations, the harness recommendation summary, and next steps (`gh pr create`, choose a LICENSE, set `OPENAI_KEY`, translation pass).
