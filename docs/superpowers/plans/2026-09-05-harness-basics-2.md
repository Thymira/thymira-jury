# Harness basics 2: the data path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The demo Run registers the project's declared dataset itself (Inspect, from `.thymira/config.yaml`), trains the one-call `run_experiment` baseline on the dataset as shipped, and the acceptance test asserts the metric the tool really computed — removing every workaround harness basics 1 had to write.

**Architecture:** `ProjectConfig` gains a `datasets` declaration (Contract 0.8). THY's Inspect node, which already parses the project config, registers each declared dataset into the Run's `ArtifactStore` through the existing `register_dataset` function, idempotently, and halts the graph before Plan when a declared dataset cannot be registered (a configuration error is a pre-Execute halt, exactly like a Gate-rejected plan). The shipped `data/german_credit.csv` loses its two corrupt rows (998 rows) and `register_dataset` names the offending lines when it refuses a ragged CSV instead of surfacing polars' raw error. `run_experiment`'s default training script becomes a scikit-learn `Pipeline` whose `ColumnTransformer` one-hot-encodes the columns that are not numeric and casts the rest, so the persisted model still predicts from raw values and `audit_model` can audit it. The runtime-context snapshot degrades to a recorded, model-visible message instead of an uncaught exception. `ScriptedProvider` accepts a callable turn so the scripted experiment agent reports exactly the metrics `run_experiment` returned, and the acceptance test pins that metric as a sidecar.

**Tech Stack:** Python 3.12+, pydantic (frozen `ThymiraModel`), LangGraph (`ThyGraph`), PydanticAI (`AgentRunner`), polars, scikit-learn, pytest, ruff, ty, uv.

**Spec:** `docs/adr/0013-harness-fundamentals-six-decisions.md` ("First pull request", Consequences: the two product defects the second pull request owns), `docs/superpowers/plans/2026-09-04-harness-basics-1.md` ("Out of scope (next PR)"), `CHANGELOG.md` `[Unreleased]` "Known issues". The other half of the agreed second pull request — failure as data and the real gate — is a separate plan (harness basics 3), so this one stays small enough to merge in a day (roadmap rule).

**Baseline:** branch `feat/harness-basics-2` at `ff76148` (`main` after PR #110). Fast lane on the baseline: 1709 passed, 3 skipped, 31 deselected.

## Global Constraints

- Everything in English: code, comments, docstrings, docs, commits.
- Run tools through `uv run …` and `just …`, never the global `python`. On this Windows machine run pytest with `--basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2` (long temp paths hit MAX_PATH and fail as `FileNotFoundError` on `.json.tmp`).
- ruff full rule set (`line-length = 100`, Google docstrings, `ANN`, `D`, `PL`); ty zero diagnostics (`.ty-baseline.json` total 0); the four import-linter contracts stay kept (`just check-imports`). A `PostToolUse` hook formats every edited Python file; do not format by hand.
- Layering: `thy` may import `agents`, `tools`, `state`, `schemas`, `events`; `schemas` imports no runtime member; `thymira.agents` must not load `thymira.tools` at import time (THY-33). No new top-level homes, no `utils.py`.
- No new `EventType` member (the vocabulary is closed; adding one drags MIRA, the API contract and the CLI along).
- "LLM proposes, code authorizes": nothing in this plan changes authorization; Inspect registers datasets as a runtime intake step, deliberately outside the Tool Manager (stated in the CHANGELOG entry).
- Tests: unit tests in `tests/thymira/test_<member>.py`, no marker; the acceptance test keeps `pytestmark = pytest.mark.integration`; LLM calls through `ScriptedProvider`; files only under `tmp_path`.
- The acceptance sidecars under `tests/thymira/acceptance/` are compared byte for byte; regenerate only with `THYMIRA_SNAPSHOT=record` and review the diff. `workspace.expected.txt` must not change in this plan.
- Conventional Commits; each commit ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k
  ```
- One `CHANGELOG.md` `[Unreleased]` entry for the whole pull request (Task 6). Never stage `.env`, `runs/`, `.superpowers/`.
- Contract change rule: a new `ProjectConfig` field bumps `CONTRACT_VERSION` and gets a "Changes" entry in `docs/contracts/contract-v0.1.md`.

---

## File structure

| File | Responsibility in this plan |
|---|---|
| `packages/schemas/src/thymira/schemas/project.py` | `DatasetConfig` + `ProjectConfig.datasets` (Contract 0.8) |
| `packages/schemas/src/thymira/schemas/__init__.py` | export `DatasetConfig`; `CONTRACT_VERSION = "0.8"` |
| `docs/contracts/contract-v0.1.md` | status line; Changes entries 0.7 (recorded after the fact) and 0.8 |
| `examples/credit-risk/.thymira/config.yaml`, `examples/credit-risk/README.md` | the demo project declares its dataset |
| `data/german_credit.csv`, `data/README.md` | the shipped dataset, 998 conformant rows |
| `runtime/tools/src/thymira/tools/datasets.py` | `register_dataset` names ragged lines when it refuses a CSV |
| `runtime/thy/src/thymira/thy/nodes/inspect.py` | Inspect registers declared datasets; halts on a registration error |
| `runtime/thy/src/thymira/thy/models.py` | `ThyState.datasets` |
| `runtime/thy/src/thymira/thy/graph.py` | Inspect receives the artifact store; conditional edge Inspect → Plan \| END |
| `runtime/thy/src/thymira/thy/nodes/plan.py` | planner prompt names the registered datasets |
| `runtime/thy/src/thymira/thy/agents/data.py` | docstring tells the truth about who registers datasets |
| `runtime/tools/src/thymira/tools/builtins/run_experiment.py` | categorical-aware default training pipeline |
| `runtime/agents/src/thymira/agents/runner.py` | runtime-context render degrades to a recorded message |
| `runtime/agents/src/thymira/agents/llm/scripted.py` | callable scripted turns |
| `tests/thymira/test_schemas.py`, `test_tools.py`, `test_tools_e2e.py`, `test_thy_inspect.py`, `test_thy_graph.py`, `test_agents_runner.py`, `test_agents_llm.py`, `test_acceptance_demo_run.py`, `tests/thymira/acceptance/` | tests and sidecars |
| `CHANGELOG.md`, `docs/adr/0013-harness-fundamentals-six-decisions.md`, `AGENTS.md` | the record |

---

### Task 1: `ProjectConfig.datasets` (Contract 0.8) and the demo project's declaration

**Files:**
- Modify: `packages/schemas/src/thymira/schemas/project.py`
- Modify: `packages/schemas/src/thymira/schemas/__init__.py` (module docstring line 1, `CONTRACT_VERSION` line 75, the `project` import block lines 56-62, `__all__`)
- Modify: `docs/contracts/contract-v0.1.md` (status line 3; `## Changes` at line 72)
- Modify: `examples/credit-risk/.thymira/config.yaml`, `examples/credit-risk/README.md`
- Test: `tests/thymira/test_schemas.py`, `tests/thymira/test_thy_inspect.py`

**Interfaces:**
- Produces: `thymira.schemas.DatasetConfig(name: str, path: str, target: str | None = None)` (frozen, `path` relative and contained); `ProjectConfig.datasets: tuple[DatasetConfig, ...] = ()`; `CONTRACT_VERSION == "0.8"`.
- Consumed by: Task 3 (Inspect), Task 6 (acceptance test).

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_schemas.py` (add the imports it lacks: `pytest`, `from pydantic import ValidationError`, `from thymira.schemas import CONTRACT_VERSION, DatasetConfig, ProjectConfig`):

```python
def test_project_config_declares_datasets_with_a_contained_relative_path() -> None:
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "domain": "credit_risk"},
            "datasets": [
                {"name": "applications", "path": "data/applications.csv", "target": "is_high_risk"}
            ],
        }
    )

    assert config.datasets[0].name == "applications"
    assert config.datasets[0].path == "data/applications.csv"
    assert config.datasets[0].target == "is_high_risk"


def test_project_config_declares_no_datasets_by_default() -> None:
    config = ProjectConfig.model_validate({"project": {"name": "demo", "domain": "credit_risk"}})

    assert config.datasets == ()


@pytest.mark.parametrize(
    "path", ["/tmp/x.csv", "C:/data/x.csv", "../x.csv", "data/../../x.csv", "data\\..\\x.csv"]
)
def test_a_dataset_path_that_leaves_the_project_is_refused(path: str) -> None:
    with pytest.raises(ValidationError, match="relative to the project directory"):
        DatasetConfig(name="x", path=path)


def test_a_dataset_name_is_a_stable_identifier() -> None:
    with pytest.raises(ValidationError):
        DatasetConfig(name="German Credit", path="data/x.csv")


def test_contract_version_is_0_8() -> None:
    assert CONTRACT_VERSION == "0.8"
```

In `tests/thymira/test_thy_inspect.py`, extend `test_load_project_context_reads_config_and_context_for_credit_risk` with two assertions after the frameworks one:

```python
    assert [dataset.name for dataset in context.config.datasets] == ["german_credit"]
    assert context.config.datasets[0].target == "is_high_risk"
```

If `tests/thymira/test_schemas.py` (or any other test) already asserts `CONTRACT_VERSION == "0.7"`, update that assertion to `"0.8"` (grep `CONTRACT_VERSION` under `tests/`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_schemas.py tests/thymira/test_thy_inspect.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: FAIL — `ImportError: cannot import name 'DatasetConfig'`, then `datasets` unknown / `"0.7" != "0.8"`.

- [ ] **Step 3: Add `DatasetConfig` and `ProjectConfig.datasets`**

Replace the whole of `packages/schemas/src/thymira/schemas/project.py` with:

```python
"""ProjectConfig: the typed form of ``.thymira/config.yaml`` (baseline, section 15)."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import Field, field_validator

from thymira.schemas.base import ThymiraModel
from thymira.schemas.enums import Framework


class ProjectInfo(ThymiraModel):
    """Identity of an agent-aware data-science project."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    domain: str = Field(min_length=1, description="e.g. 'credit_risk'")


class ExperimentsConfig(ThymiraModel):
    """Experiment tracking settings."""

    tracking: str = "mlflow"


class AgentsConfig(ThymiraModel):
    """Which orchestrator serves the project by default."""

    default: str = "thy"


class GovernanceConfig(ThymiraModel):
    """Audit requirements for the project."""

    audit_required: bool = True
    frameworks: tuple[Framework, ...] = ()


class DatasetConfig(ThymiraModel):
    """One dataset the project declares; THY registers it at Inspect, before any agent runs.

    ``path`` is relative to the project directory (the directory that holds ``.thymira/``) and
    may not leave it. ``target`` names the column the project predicts, when it has one, so the
    planner and the agents do not have to guess it.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    path: str = Field(min_length=1)
    target: str | None = None

    @field_validator("path")
    @classmethod
    def _relative_and_contained(cls, value: str) -> str:
        """Refuse an absolute path or one that climbs out of the project directory."""
        posix = PurePosixPath(value.replace("\\", "/"))
        if posix.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in posix.parts:
            msg = (
                "dataset path must be relative to the project directory and may not contain "
                f"'..': {value!r}"
            )
            raise ValueError(msg)
        return value


class ProjectConfig(ThymiraModel):
    """The whole ``.thymira/config.yaml``; unknown keys are rejected on purpose."""

    project: ProjectInfo
    experiments: ExperimentsConfig = Field(default_factory=ExperimentsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    datasets: tuple[DatasetConfig, ...] = ()
```

In `packages/schemas/src/thymira/schemas/__init__.py`: change line 1 to `"""Thymira contracts — Contract v0.8 (see docs/contracts/contract-v0.1.md).`; add `DatasetConfig,` to the `from thymira.schemas.project import (...)` block (alphabetical: after `AgentsConfig,`); add `"DatasetConfig",` to `__all__` in its alphabetical place; set `CONTRACT_VERSION = "0.8"`.

- [ ] **Step 4: Declare the demo dataset and document the contract**

Append to `examples/credit-risk/.thymira/config.yaml`:

```yaml

datasets:
  - name: german_credit
    path: data/applications.csv
    target: is_high_risk
```

In `examples/credit-risk/README.md`, in the line that describes `data/` (line 13: "`data/` — a pointer to the demo data set …"), replace the line with:

```markdown
- `data/` — the demo data set goes here as `applications.csv` (copy `data/german_credit.csv` from
  the repository root; it is not committed twice). `.thymira/config.yaml` declares it under
  `datasets:`, and THY's Inspect registers it into the Run before any agent works.
```

In `docs/contracts/contract-v0.1.md`: change the status line 3 from `` `0.6` `` to `` `0.8` ``; insert at the top of the `## Changes` list (before the `0.6` entry):

```markdown
- **0.8 (2026-09-05)** — Contract 0.8 adds `ProjectConfig.datasets`, a tuple of `DatasetConfig`
  (`name`, a `path` relative to and contained in the project directory, an optional `target`
  column). It is the project's declaration of the datasets THY's Inspect registers into the Run
  before any agent runs; additive and defaulted to empty.
- **0.7 (2026-09-03, PR #90, recorded after the fact)** — Contract 0.7 added
  `ExecutionConstraints` / `ExecutionAction` and `PolicyDecision.execution_constraints` (the typed
  execution contract the pre-THY Gate records), plus the event types `rework.started` and
  `rework.escalated`. The code bumped `CONTRACT_VERSION` to `0.7` without this entry.
```

Verify the 0.7 wording against `git show af74dfe --stat -- packages/schemas` before committing; adjust the entry if that commit added anything else under `packages/schemas`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_schemas.py tests/thymira/test_thy_inspect.py tests/thymira/test_api_contract.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: PASS. If `test_api_contract.py` pins an OpenAPI shape that embeds `ProjectConfig`, update its expectation in the same commit and say so in the report.

- [ ] **Step 6: Commit**

```bash
git add packages/schemas/src/thymira/schemas/project.py packages/schemas/src/thymira/schemas/__init__.py docs/contracts/contract-v0.1.md examples/credit-risk/.thymira/config.yaml examples/credit-risk/README.md tests/thymira/test_schemas.py tests/thymira/test_thy_inspect.py
git commit -m "feat(schemas): declare a project's datasets in ProjectConfig (Contract 0.8)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

---

### Task 2: The shipped dataset registers as shipped, and a ragged CSV is refused with the line numbers

**Files:**
- Modify: `data/german_credit.csv` (drop the two ragged rows), `data/README.md`
- Modify: `runtime/tools/src/thymira/tools/datasets.py`
- Modify: `tests/thymira/test_tools_e2e.py:125-143, 171-172` (drop `_write_conformant_rows`), `tests/thymira/test_tools.py:1364-1372` (drop the inline workaround)
- Check: `scripts/real_e2e_smoke.py` (line 106 reads the dataset; remove any ragged-row filtering there too)
- Test: `tests/thymira/test_tools.py`

**Interfaces:**
- Produces: `register_dataset` unchanged in signature; on a ragged CSV it raises `ValueError` whose message names the file and the 1-based line numbers. The shipped file has 998 data rows and 21 columns.
- Consumed by: Task 3 (Inspect surfaces that message on `ThyState.error`), Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_tools.py` (the file already imports `pl`, `pytest`, `Path`, `register_dataset`, `LocalArtifactStore`, `new_id`; add any that are missing):

```python
def test_register_dataset_names_the_ragged_lines_of_a_csv_it_refuses(tmp_path: Path) -> None:
    ragged = tmp_path / "ragged.csv"
    ragged.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n9\n", encoding="utf-8", newline="\n")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match=r"ragged\.csv: 2 row\(s\).*3 fields.*lines 3, 5") as info:
        register_dataset(store, ragged, "ragged", produced_by=new_id("agent"))

    assert "fix the file" in str(info.value)
    assert store.list_active() == []


def test_the_shipped_demo_dataset_registers_as_shipped(tmp_path: Path) -> None:
    dataset = Path(__file__).resolve().parents[2] / "data" / "german_credit.csv"
    if not dataset.is_file():
        pytest.skip(f"demo dataset missing: {dataset}")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    _artifact, schema = register_dataset(store, dataset, "german_credit", produced_by=new_id("agent"))

    assert schema.row_count == 998
    assert len(schema.columns) == 21
    assert schema.columns[-1] == "is_high_risk"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py -q -k "ragged_lines or registers_as_shipped" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: FAIL — the first raises polars' `ComputeError` (not a `ValueError` with line numbers); the second raises on the ragged file.

- [ ] **Step 3: Drop the two corrupt rows from the fixture**

The file has 1001 lines: a 21-field header and 1000 data rows; 1-based lines 391 and 789 have 20 fields (each is missing one value — `foreign_worker` and `personal_status_sex` respectively — so they cannot be repaired without inventing data). Remove exactly those two lines, preserving the file's bytes and line endings otherwise. Write a throwaway script under the scratchpad (never under the repository) and run it with `uv run python <script>`:

```python
from pathlib import Path

path = Path("data/german_credit.csv")
raw = path.read_bytes()
newline = b"\r\n" if b"\r\n" in raw else b"\n"
lines = raw.split(newline)
header_fields = lines[0].count(b",") + 1
ragged = [index for index, line in enumerate(lines) if line and line.count(b",") + 1 != header_fields]
assert ragged == [390, 788], ragged  # 0-based indexes of 1-based lines 391 and 789
kept = [line for index, line in enumerate(lines) if index not in ragged]
path.write_bytes(newline.join(kept))
```

Then verify: `uv run python -c "import polars as pl; f = pl.read_csv('data/german_credit.csv'); print(f.shape)"` prints `(998, 21)`.

Update `data/README.md`: replace "the public German Credit dataset, 1,000 rows, used by `examples/credit-risk`" with "the public German Credit dataset as used by `examples/credit-risk`: 998 rows, after the two rows that shipped with a missing field — lines 391 and 789 of the original file — were removed on 2026-09-05; `register_dataset` refuses a ragged CSV rather than guess the missing value".

- [ ] **Step 4: Make `register_dataset` name the ragged lines**

In `runtime/tools/src/thymira/tools/datasets.py` add `import csv` to the stdlib imports and replace `_read_frame` with:

```python
def _read_frame(path: Path) -> pl.DataFrame:
    """Read a supported dataset only for schema capture.

    A ragged CSV (a row whose field count differs from the header's) is refused, as polars does,
    but with the 1-based line numbers of the offending rows so the owner can fix the file rather
    than guess which value is missing.
    """
    if path.suffix.casefold() != ".csv":
        return pl.read_parquet(path)
    try:
        return pl.read_csv(path)
    except pl.exceptions.ComputeError as exc:
        width, ragged = _ragged_lines(path)
        if not ragged:
            msg = f"{path.name}: cannot be read as CSV: {exc}"
            raise ValueError(msg) from exc
        shown = ", ".join(str(number) for number in ragged[:_RAGGED_LINES_SHOWN])
        more = f" and {len(ragged) - _RAGGED_LINES_SHOWN} more" if len(ragged) > _RAGGED_LINES_SHOWN else ""
        msg = (
            f"{path.name}: {len(ragged)} row(s) do not match the header's {width} fields "
            f"(lines {shown}{more}); fix the file before registering it"
        )
        raise ValueError(msg) from exc


def _ragged_lines(path: Path) -> tuple[int, list[int]]:
    """Return the header width and the 1-based line numbers whose field count differs from it."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return 0, []
        width = len(header)
        ragged = [
            number for number, row in enumerate(reader, start=2) if row and len(row) != width
        ]
    return width, ragged
```

Add `_RAGGED_LINES_SHOWN = 10` next to the imports. Keep `__all__` unchanged.

- [ ] **Step 5: Remove the workarounds that existed only because of the ragged rows**

`tests/thymira/test_tools_e2e.py`: delete `_write_conformant_rows` (lines 130-142) and replace line 172 (`_write_conformant_rows(dataset, workspace_dataset)`) with `shutil.copyfile(dataset, workspace_dataset)` (add `import shutil`). Keep `_dataset_path()`.

`tests/thymira/test_tools.py`, in `test_audit_model_reports_subgroup_and_auc_evidence_on_german_credit`: replace lines 1364-1372 (the comment, `lines`/`width`/`kept`/`conformant` block and `raw = pl.read_csv(conformant)`) with:

```python
    # Build a numeric view -- the numeric columns plus a binary protected attribute (is_female,
    # from personal_status_sex) -- so a plain estimator can train and audit.
    raw = pl.read_csv(dataset)
```

`scripts/real_e2e_smoke.py`: read around line 106; if it filters ragged rows (a `count(",")` comparison or a "conformant" copy), replace it with a plain copy of the file and keep the rest. If it only reads the file, leave it.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_tools_e2e.py tests/thymira/test_tools_modelling.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: PASS (the e2e test skips only if the dataset is missing, which it is not).

- [ ] **Step 7: Commit**

```bash
git add data/german_credit.csv data/README.md runtime/tools/src/thymira/tools/datasets.py tests/thymira/test_tools.py tests/thymira/test_tools_e2e.py scripts/real_e2e_smoke.py
git commit -m "fix(tools,data): ship a conformant german_credit.csv and name the ragged lines a CSV is refused for" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

(Omit `scripts/real_e2e_smoke.py` from `git add` if it was not changed.)

---

### Task 3: Inspect registers the project's declared datasets

**Files:**
- Modify: `runtime/thy/src/thymira/thy/nodes/inspect.py` (whole file)
- Modify: `runtime/thy/src/thymira/thy/models.py:174-195` (`ThyState.datasets`)
- Modify: `runtime/thy/src/thymira/thy/graph.py:61-71, 84-86, 179, 209-` (edges, `_route_after_inspect`, node wiring, docstring)
- Modify: `runtime/thy/src/thymira/thy/nodes/plan.py:95-102` (dataset note)
- Modify: `runtime/thy/src/thymira/thy/agents/data.py:12-14` (docstring)
- Test: `tests/thymira/test_thy_inspect.py`, `tests/thymira/test_thy_graph.py`

**Interfaces:**
- Consumes: `ProjectConfig.datasets` (Task 1); `register_dataset(store, path, name, *, produced_by)` and its `ValueError`/`FileNotFoundError` (Task 2); `ArtifactStore.list_active()`.
- Produces: `inspect_node(project_dir: Path, *, artifact_store: ArtifactStore | None = None) -> Callable[..., dict[str, Any]]`; `ThyState.datasets: tuple[str, ...]` (registered names, in declaration order); `ThyState.error` set to `dataset '<name>' declared in .thymira/config.yaml is missing: <relative path>` or `dataset '<name>' could not be registered: <message>`; the graph routes Inspect → END on `error`. Artifacts `datasets/<name>.csv` (or `.parquet`) and `datasets/<name>.schema.json`, `produced_by=state.run.id` (the precedent is Summarize's `analysis.md`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_thy_inspect.py` (add imports: `import yaml`; `from thymira.state import LocalArtifactStore`; `from thymira.thy.nodes.inspect import inspect_node`; `from thymira.thy.graph import graph_definition_hash`):

```python
_TINY_CSV = "colour,size,label\nred,1,yes\nblue,2,no\ngreen,3,yes\nred,4,no\n"


def _project(tmp_path: Path, *, csv_text: str | None = _TINY_CSV) -> Path:
    """Write a project that declares one dataset; `csv_text=None` declares a file that is absent."""
    project_dir = tmp_path / "project"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / ".thymira" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "domain": "credit_risk"},
                "datasets": [
                    {"name": "applications", "path": "data/applications.csv", "target": "label"}
                ],
            }
        ),
        encoding="utf-8",
    )
    if csv_text is not None:
        (project_dir / "data").mkdir()
        (project_dir / "data" / "applications.csv").write_text(
            csv_text, encoding="utf-8", newline="\n"
        )
    return project_dir


def test_inspect_registers_a_declared_dataset_into_the_run(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)

    raw = inspect_node(_project(tmp_path), artifact_store=store)(ThyState(run=run))
    state = ThyState.model_validate(raw)

    assert state.error is None
    assert state.datasets == ("applications",)
    names = {artifact.name: artifact for artifact in store.list_active()}
    assert names["datasets/applications.csv"].produced_by == run.id
    schema = store.load_json("datasets/applications.schema.json")
    assert schema["row_count"] == 4
    assert schema["columns"] == ["colour", "size", "label"]


def test_inspect_does_not_register_a_dataset_twice_on_resume(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    node = inspect_node(_project(tmp_path), artifact_store=store)

    first = ThyState.model_validate(node(ThyState(run=run)))
    second = ThyState.model_validate(node(ThyState(run=run)))

    assert second.datasets == first.datasets == ("applications",)
    assert [a.name for a in store.list_active()].count("datasets/applications.csv") == 1
    assert store.verify() == []


def test_inspect_halts_the_run_when_a_declared_dataset_is_missing(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)

    state = ThyState.model_validate(
        inspect_node(_project(tmp_path, csv_text=None), artifact_store=store)(ThyState(run=run))
    )

    assert state.error == (
        "dataset 'applications' declared in .thymira/config.yaml is missing: data/applications.csv"
    )
    assert state.datasets == ()
    assert store.list_active() == []


def test_inspect_reports_why_a_declared_dataset_could_not_be_registered(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    project_dir = _project(tmp_path, csv_text="a,b\n1,2\n3\n")

    state = ThyState.model_validate(
        inspect_node(project_dir, artifact_store=store)(ThyState(run=run))
    )

    assert state.error is not None
    assert state.error.startswith("dataset 'applications' could not be registered: ")
    assert "lines 3" in state.error


def test_inspect_without_an_artifact_store_registers_nothing_and_still_seeds_the_state(
    tmp_path: Path,
) -> None:
    state = ThyState.model_validate(inspect_node(_project(tmp_path))(ThyState(run=_run())))

    assert state.error is None
    assert state.datasets == ()
    assert state.domain == "credit_risk"


def test_the_graph_ends_after_a_failed_intake_without_planning(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    graph = build_thy_graph(
        AgentCatalog(), log, project_dir=_project(tmp_path, csv_text=None), artifact_store=store
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run)))

    assert final_state.error is not None and "is missing" in final_state.error
    assert final_state.plan == ()
    assert final_state.phase is ThyPhase.INSPECT
```

Add `from thymira.thy.models import ThyPhase` to the imports. In `tests/thymira/test_thy_graph.py`, read the tests that involve `graph_definition_hash()`; if one pins a literal digest, replace the literal with the new value after Step 3 and say so in the report (the hash is documented as "changing whenever a node or edge does", and this task adds a conditional edge).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_thy_inspect.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: FAIL — `inspect_node() got an unexpected keyword argument 'artifact_store'`, then missing `datasets` field.

- [ ] **Step 3: Implement**

`runtime/thy/src/thymira/thy/models.py`: after `project_context: str | None = None` (line 183) add:

```python
    datasets: tuple[str, ...] = ()
```

and in the `ThyState` docstring add one sentence: "`datasets` names the project-declared datasets Inspect registered into the Run, in declaration order, so Plan can tell the planner what already exists."

Replace the whole of `runtime/thy/src/thymira/thy/nodes/inspect.py` with:

```python
"""Inspect node: seed ThyState with the project's config, context and datasets (THY-10).

Inspect is the Run's intake. It loads `.thymira/config.yaml` and `.thymira/context.md`, and it
registers every dataset the config declares (`ProjectConfig.datasets`) into the Run's
`ArtifactStore` through `register_dataset`, so the artifacts a sub-agent later reads
(`profile_dataset`, `run_experiment`) exist before any agent runs and the runtime-context
snapshot can list them. Registration is a runtime intake step, not a tool call: it goes through
no Tool Manager and no Gate, exactly as Summarize writes `analysis.md` -- the Policy Engine
decides what agents may do with the data, not whether the project may declare it.

A declared dataset that cannot be registered (missing file, ragged CSV) is a configuration
error, so the node records the reason on `ThyState.error` and `_route_after_inspect`
(`thymira.thy.graph`) halts the graph before Plan: nothing ran, so there is nothing to audit.
Registration is idempotent -- a dataset whose schema artifact is already active is not written
again -- so a resumed Run re-enters Inspect safely.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from thymira.thy.context import load_project_context
from thymira.tools.datasets import register_dataset

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.schemas import DatasetConfig
    from thymira.state import ArtifactStore
    from thymira.thy.models import ThyState

_SCHEMA_SUFFIX = ".schema.json"


def inspect_node(
    project_dir: Path, *, artifact_store: ArtifactStore | None = None
) -> Callable[..., dict[str, Any]]:
    """Build the Inspect node bound to `project_dir`.

    Seeds `domain`, `governance_frameworks` and `project_context` from
    `load_project_context(project_dir)`; a project without `.thymira` leaves them at `ThyState`'s
    defaults rather than failing the run. With an `artifact_store`, also registers the datasets
    the config declares and records their names on `ThyState.datasets`; without one (a
    lightweight graph), declared datasets are left alone.
    """

    def _inspect(state: ThyState) -> dict[str, Any]:
        context = load_project_context(project_dir)
        updates: dict[str, Any] = {"project_context": context.context_md or None}
        if context.config is not None:
            updates["domain"] = context.config.project.domain
            updates["governance_frameworks"] = context.config.governance.frameworks
            if artifact_store is not None and context.config.datasets:
                registered, error = _register_declared(
                    context.config.datasets, project_dir, artifact_store, produced_by=state.run.id
                )
                updates["datasets"] = registered
                if error is not None:
                    return state.model_copy(update={**updates, "error": error}).model_dump()
        next_state = state.advance()
        return next_state.model_copy(update=updates).model_dump()

    return _inspect


def _register_declared(
    datasets: tuple[DatasetConfig, ...],
    project_dir: Path,
    store: ArtifactStore,
    *,
    produced_by: str,
) -> tuple[tuple[str, ...], str | None]:
    """Register each declared dataset once; stop at the first one that cannot be registered."""
    active = {artifact.name for artifact in store.list_active()}
    registered: list[str] = []
    for dataset in datasets:
        if f"datasets/{dataset.name}{_SCHEMA_SUFFIX}" in active:
            registered.append(dataset.name)
            continue
        source = project_dir / Path(dataset.path)
        if not source.is_file():
            return tuple(registered), (
                f"dataset {dataset.name!r} declared in .thymira/config.yaml is missing: "
                f"{dataset.path}"
            )
        try:
            register_dataset(store, source, dataset.name, produced_by=produced_by)
        except ValueError as exc:
            return tuple(registered), (
                f"dataset {dataset.name!r} could not be registered: {exc}"
            )
        registered.append(dataset.name)
    return tuple(registered), None


__all__ = ["inspect_node"]
```

`runtime/thy/src/thymira/thy/graph.py`:
- `_GRAPH_EDGES`: remove `("inspect", "plan")`.
- `_CONDITIONAL_EDGES`: insert `("inspect", ("plan", END)),` as the first entry.
- Add before `_route_after_plan`:

```python
def _route_after_inspect(state: ThyState) -> str:
    """Plan only when the intake succeeded; a project whose declared data is unusable ends here."""
    return "plan" if state.error is None else END
```

- Line 179: `builder.add_node("inspect", inspect_node(project_dir, artifact_store=artifact_store) if project_dir is not None else _inspect)` (formatted by ruff).
- Where the edges are added (after the nodes; find `builder.add_edge("inspect", "plan")`): replace it with `builder.add_conditional_edges("inspect", _route_after_inspect, ["plan", END])`, matching how the plan edge is added.
- Module docstring (lines 3-4 and 18-19): mention that Inspect also registers the declared datasets when `artifact_store` is given, and that `_route_after_inspect` sends a failed intake to END exactly as `_route_after_plan` sends a rejected plan.
- `build_thy_graph`'s `artifact_store:` docstring (line 162-163): "Where Summarize writes `analysis.md` and where Inspect registers the project's declared datasets. Omitted, …".

`runtime/thy/src/thymira/thy/nodes/plan.py:95-102`: after `constraint_note = (...)` add

```python
        dataset_note = (
            f"\nRegistered datasets: {', '.join(state.datasets)}" if state.datasets else ""
        )
```

and change the prompt to `f"Plan the run for: {state.run.prompt}{constraint_note}{dataset_note}"`.

`runtime/thy/src/thymira/thy/agents/data.py:12-14`: replace the sentence claiming "the production Core factory registers it and supplies the registry and context" with: "THY's Inspect node registers the datasets `.thymira/config.yaml` declares before any agent runs; this agent reads them through `profile_dataset`."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_thy_inspect.py tests/thymira/test_thy_graph.py tests/thymira/test_thy_plan.py tests/thymira/test_thy_agent_data.py tests/thymira/test_core_composition.py tests/thymira/test_core_graph_adapters.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: PASS. `test_core_composition.py::test_composed_graph_hash_changes_when_a_child_version_changes` must still pass (it asserts change, not a literal).

- [ ] **Step 5: Commit**

```bash
git add runtime/thy/src/thymira/thy/nodes/inspect.py runtime/thy/src/thymira/thy/models.py runtime/thy/src/thymira/thy/graph.py runtime/thy/src/thymira/thy/nodes/plan.py runtime/thy/src/thymira/thy/agents/data.py tests/thymira/test_thy_inspect.py tests/thymira/test_thy_graph.py
git commit -m "feat(thy): Inspect registers the project's declared datasets into the Run" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

---

### Task 4: `run_experiment`'s default training handles categorical columns

**Files:**
- Modify: `runtime/tools/src/thymira/tools/builtins/run_experiment.py:191-232` (`_training_code`)
- Test: `tests/thymira/test_tools.py`

**Interfaces:**
- Produces: the default script trains `Pipeline([("encode", ColumnTransformer(numeric cast + OneHotEncoder)), ("classify", LogisticRegression)])` on raw CSV values; the persisted `models/<experiment>.joblib` predicts from raw feature values in header order, exposes `classes_` (the raw labels) and `predict_proba`, so `audit_model` (`_feature_order`, `_class_labels`) audits it unchanged. `metrics.json` stays `{"accuracy": <float>}`.
- Consumed by: Task 6 (the acceptance Run no longer passes `code`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_tools.py`. Mirror the wiring of `test_run_experiment_persists_model_record_and_events` (line 1146) for the context, registry and manager — the helper names below (`_context`, `ToolManager`, `ToolRegistry`, `RunExperiment`, `AuditModel`, `ToolCallStatus`, `joblib`) already exist in the module:

```python
def _categorical_csv(path: Path, rows: int = 40) -> None:
    """A tiny dataset with two categorical features, one numeric feature and a string label."""
    colours = ("red", "blue", "green")
    lines = ["colour,size,shape,label"]
    for index in range(rows):
        colour = colours[index % 3]
        shape = "round" if index % 2 == 0 else "square"
        label = "yes" if (index % 3 == 0) != (index % 5 == 0) else "no"
        lines.append(f"{colour},{index},{shape},{label}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def test_run_experiment_default_training_handles_categorical_features(tmp_path: Path) -> None:
    context = _context(
        tmp_path, capability=ToolCapability(id="run_experiment", external_effects=())
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "shapes.csv"
    _categorical_csv(source)
    register_dataset(context.artifact_store, source, "shapes", produced_by=context.agent_id)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment()), cast("Tool", AuditModel())))
    )

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "shapes",
            "target_column": "label",
            "experiment_name": "shapes-baseline",
            "description": "Train the default baseline on categorical features",
        },
    )

    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error
    reported = json.loads(trained.result.stdout)["metrics"]
    assert 0.0 <= reported["accuracy"] <= 1.0
    assert reported == context.artifact_store.load_json("metrics/shapes-baseline.json")

    audited = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/shapes-baseline.joblib",
            "dataset": "shapes",
            "target_column": "label",
            "protected_column": "colour",
        },
    )

    assert audited.call.status is ToolCallStatus.COMPLETED, audited.result.error
    payload = json.loads(audited.result.stdout)
    assert set(payload["subgroups"]) == {"blue", "green", "red"}
    assert "auc" in payload


def test_run_experiment_default_training_keeps_the_raw_labels_on_the_model(tmp_path: Path) -> None:
    context = _context(
        tmp_path, capability=ToolCapability(id="run_experiment", external_effects=())
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "numbers.csv"
    source.write_text(
        "x,y,label\n" + "\n".join(f"{i},{(i * 7) % 11},{int(i % 3 == 0)}" for i in range(40)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    register_dataset(context.artifact_store, source, "numbers", produced_by=context.agent_id)
    manager = ToolManager(ToolRegistry((cast("Tool", RunExperiment()),)))

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "numbers",
            "target_column": "label",
            "experiment_name": "numbers-baseline",
            "description": "Train the default baseline on numeric features",
        },
    )

    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error
    model_path = tmp_path / "model.joblib"
    model_path.write_bytes(context.artifact_store.load_bytes("models/numbers-baseline.joblib"))
    model = joblib.load(model_path)
    assert [str(label) for label in model.classes_] == ["0", "1"]
    assert len(model.predict([["1", "7"], ["2", "3"]])) == 2
```

If `_context` takes different keyword arguments in the current file, use them as the neighbouring `run_experiment` test does; keep the assertions.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py -q -k "default_training" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: the categorical test FAILS (`run_experiment` returns `FAILED`: `could not convert string to float: 'red'`); the numeric one may pass already (the raw-label rule holds today) — that is fine, it pins the behaviour the new script must keep.

- [ ] **Step 3: Replace the default training script**

In `runtime/tools/src/thymira/tools/builtins/run_experiment.py` replace `_training_code` (lines 191-232) with:

```python
def _training_code(
    dataset_path: Path,
    model_path: Path,
    metrics_path: Path,
    *,
    target_column: str,
    seed: int,
) -> str:
    """Build a fixed-seed training script with JSON-encoded paths and names.

    Every feature column is read as text. A column whose every value parses as a number is cast
    to float; any other column is one-hot encoded (unknown categories at prediction time map to
    all zeros rather than raising). Both steps live inside the persisted `Pipeline`, so the
    model artifact predicts from raw values in header order and `audit_model`/`inspect_model`
    never have to reconstruct an encoding. Labels are fitted as they appear -- a `LabelEncoder`
    on the target would leave `model.classes_` a list of positional codes that no longer name
    anything, and every later reader would have to guess the mapping back.
    """
    return f"""
import csv
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder

source = Path({json.dumps(str(dataset_path))})
with source.open(encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle))
target = {json.dumps(target_column)}
features = [name for name in rows[0] if name != target]


def is_numeric(name):
    try:
        for row in rows:
            float(row[name])
    except ValueError:
        return False
    return True


numeric = [index for index, name in enumerate(features) if is_numeric(name)]
categorical = [index for index in range(len(features)) if index not in numeric]
x_values = [[row[name] for name in features] for row in rows]
labels = [row[target] for row in rows]
encoder = ColumnTransformer(
    [
        ("numeric", FunctionTransformer(np.asarray, kw_args={{"dtype": float}}), numeric),
        ("categorical", OneHotEncoder(handle_unknown="ignore"), categorical),
    ]
)
model = Pipeline(
    [
        ("encode", encoder),
        ("classify", LogisticRegression(random_state={seed}, max_iter=1000)),
    ]
)
x_train, x_test, y_train, y_test = train_test_split(
    x_values, labels, test_size=0.25, random_state={seed}
)
model.fit(x_train, y_train)
predicted = model.predict(x_test)
metrics = {{"accuracy": float(accuracy_score(y_test, predicted))}}
joblib.dump(model, {json.dumps(str(model_path))})
Path({json.dumps(str(metrics_path))}).write_text(
    json.dumps(metrics, sort_keys=True), encoding="utf-8"
)
print(json.dumps(metrics, sort_keys=True))
    """
```

`np.asarray` is a module-level function, so the `FunctionTransformer` pickles; a lambda would not. If `ColumnTransformer` refuses the list-of-lists input on the installed scikit-learn, convert once with `x_values = np.asarray(x_values, dtype=object)` right after building it and keep the rest.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_tools_e2e.py tests/thymira/test_tools_modelling.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: PASS, including every existing `run_experiment`, `inspect_model` and `audit_model` test.

- [ ] **Step 5: Commit**

```bash
git add runtime/tools/src/thymira/tools/builtins/run_experiment.py tests/thymira/test_tools.py
git commit -m "fix(tools): run_experiment's default baseline encodes categorical features inside the persisted pipeline" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

---

### Task 5: The runtime-context snapshot degrades to evidence, and `ScriptedProvider` takes a callable turn

**Files:**
- Modify: `runtime/agents/src/thymira/agents/runner.py:193-208`
- Modify: `runtime/agents/src/thymira/agents/llm/scripted.py`
- Test: `tests/thymira/test_agents_runner.py`, `tests/thymira/test_agents_llm.py`

**Interfaces:**
- Produces: in `AgentRunner.run`, a render failure (`OSError`, `ValueError` — pydantic's `ValidationError` and `json.JSONDecodeError` are `ValueError`s) still appends the model-visible `agent.message` (`form: runtime_context`) before `agent.started`, with text `Current runtime context could not be rendered: <error, at most 500 characters>. The artifact store or a dataset schema may be unreadable; say so in your result if it blocks the task.` The step then runs as usual. `ScriptedProvider(responses)` accepts, besides `str | dict | BaseModel`, a `ScriptedTurn = Callable[[Sequence[LLMMessage]], ScriptedItem]`, resolved only by `complete_turn` with the full message list; `complete`/`complete_structured` raise `LLMCallError("a callable scripted item needs complete_turn")`.
- Consumed by: Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_agents_runner.py` (add `from dataclasses import replace`, `from thymira.schemas import Artifact` and `from thymira.state import LocalArtifactStore` if absent):

```python
class _UnreadableStore(LocalArtifactStore):
    """An artifact store whose manifest cannot be listed."""

    def list_active(self) -> list[Artifact]:
        raise OSError("manifest unreadable")


def test_an_unrenderable_runtime_context_is_recorded_and_the_step_still_runs(
    tmp_path: Path,
) -> None:
    provider, spec, task, ctx = _wired_context(tmp_path)
    assert ctx.tool_context is not None
    broken = _UnreadableStore(tmp_path / "artifacts", task.run_id)
    ctx.tool_context = replace(ctx.tool_context, artifact_store=broken)

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status is TaskStatus.COMPLETED
    snapshot = next(
        e
        for e in ctx.event_log.events()
        if e.type is EventType.AGENT_MESSAGE and e.payload.get("form") == RUNTIME_CONTEXT_FORM
    )
    assert snapshot.surface is EventSurface.MODEL_VISIBLE
    assert "could not be rendered: manifest unreadable" in snapshot.payload["text"]
    started = next(e for e in ctx.event_log.events() if e.type is EventType.AGENT_STARTED)
    assert snapshot.seq < started.seq
    assert "could not be rendered" in provider.calls[0]["prompt"]
```

(`AgentContext` is a `@dataclass`, so assigning `ctx.tool_context` works; `ToolContext` is frozen, hence `replace`. If `_wired_context`'s scripted responses end the step with a `final_result` after one `run_python` call, the step completes; keep whatever it scripts.)

Append to `tests/thymira/test_agents_llm.py` (imports: `from thymira.agents import LLMToolCall, ScriptedProvider`; `from thymira.agents.llm.base import LLMCallError, LLMMessage`):

```python
def test_a_callable_scripted_turn_answers_from_the_messages_it_is_shown() -> None:
    def echo(messages: Sequence[LLMMessage]) -> LLMToolCall:
        return LLMToolCall(id="t1", name="final_result", arguments={"seen": messages[-1].content})

    provider = ScriptedProvider([echo])

    response = provider.complete_turn([LLMMessage(role="user", content="hello")])

    assert response.tool_calls[0].arguments == {"seen": "hello"}
    assert provider.calls[0]["prompt"] == "hello"


def test_a_callable_scripted_item_is_refused_outside_complete_turn() -> None:
    provider = ScriptedProvider([lambda _messages: "never"])

    with pytest.raises(LLMCallError, match="complete_turn"):
        provider.complete("hi")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_agents_runner.py tests/thymira/test_agents_llm.py -q -k "unrenderable or callable_scripted" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: FAIL — `OSError: manifest unreadable` escapes `AgentRunner.run`; `TypeError`/`_as_text` failure for the callable.

- [ ] **Step 3: Implement**

`runtime/agents/src/thymira/agents/runner.py`: add near the other module constants

```python
_RENDER_ERROR_CHARS = 500
```

and a helper (module level, after `_environment` or wherever helpers live):

```python
def _runtime_context_or_reason(tool_context: ToolContext) -> str:
    """Render the snapshot, or the reason it could not be rendered -- never raise.

    The snapshot is evidence before it is prompt text (model-visible ⟺ logged); a store or schema
    that cannot be read is itself a fact worth recording and showing the model, not a reason to
    abort the step before `agent.started` exists.
    """
    try:
        return runtime_context_text(
            workspace=tool_context.workspace, artifact_store=tool_context.artifact_store
        )
    except (OSError, ValueError) as exc:
        reason = str(exc)[:_RENDER_ERROR_CHARS]
        return (
            f"Current runtime context could not be rendered: {reason}. The artifact store or a "
            "dataset schema may be unreadable; say so in your result if it blocks the task."
        )
```

and replace lines 198-201 (the `"text": runtime_context_text(...)` call) with `"text": _runtime_context_or_reason(ctx.tool_context),`. Keep the event order (snapshot before `agent.started`). If `ToolContext` is only imported under `TYPE_CHECKING` in this module, that is fine for the annotation.

`runtime/agents/src/thymira/agents/llm/scripted.py`:
- Imports: `from collections.abc import Callable, Sequence` becomes a runtime import only if needed for the alias; with `from __future__ import annotations` keep them under `TYPE_CHECKING` and define the aliases as strings-free `TypeAlias` values:

```python
ScriptedItem = str | dict[str, Any] | BaseModel
ScriptedTurn = Callable[[Sequence[LLMMessage]], ScriptedItem]
```

(`Callable`, `Sequence` and `LLMMessage` are then real imports — move them out of `TYPE_CHECKING`.)
- `__init__(self, responses: Sequence[ScriptedItem | ScriptedTurn], ...)`; `self._responses: list[ScriptedItem | ScriptedTurn]`.
- `_next` returns `ScriptedItem | ScriptedTurn` unchanged.
- In `complete_turn`, after `item = self._next(prompt, system, recorded_tools)`:

```python
        if not isinstance(item, (str, dict, BaseModel)):
            item = item(messages)
```

- In `complete` and `complete_structured`, after `item = self._next(...)`:

```python
        if not isinstance(item, (str, dict, BaseModel)):
            raise LLMCallError("a callable scripted item needs complete_turn")
```

- Class docstring: add "A callable item is resolved by `complete_turn` with the messages it is shown — the way a scripted model reports what a tool actually returned."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_agents_runner.py tests/thymira/test_agents_llm.py tests/thymira/test_agents_runtime_context.py tests/thymira/test_agents_tool_bridge.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2` and `uv run ty check`
Expected: PASS; ty reports zero diagnostics.

- [ ] **Step 5: Commit**

```bash
git add runtime/agents/src/thymira/agents/runner.py runtime/agents/src/thymira/agents/llm/scripted.py tests/thymira/test_agents_runner.py tests/thymira/test_agents_llm.py
git commit -m "fix(agents): record an unrenderable runtime context instead of aborting the step; scripted turns may read their messages" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

---

### Task 6: The acceptance Run registers through Inspect, trains the default baseline and pins the real metric

**Files:**
- Modify: `tests/thymira/test_acceptance_demo_run.py`
- Create: `tests/thymira/acceptance/metrics.expected.json`
- Modify: `tests/thymira/acceptance/README.md`
- Modify: `CHANGELOG.md` (`[Unreleased]`), `docs/adr/0013-harness-fundamentals-six-decisions.md` (Consequences, last bullet), `AGENTS.md` (repository map, `runtime/thy` sentence)

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: the recorded acceptance Run with no workaround; `metrics.expected.json` holds `{"accuracy": <float>}` recorded from the real artifact.

- [ ] **Step 1: Rewrite the acceptance test**

In `tests/thymira/test_acceptance_demo_run.py`:

1. Module docstring: replace the paragraph "and a conformant copy of data/german_credit.csv -- the two ragged rows dropped, 998 rows -- because register_dataset refuses the file as shipped (see below)" with "and data/german_credit.csv copied to the project's `data/applications.csv`, where `.thymira/config.yaml` declares it, so Inspect registers it exactly as a real Run would". Replace the "Known product defects…" paragraph with:

```
What is real about the numbers: the experiment agent's scripted final_result reports the metrics
run_experiment actually returned (a callable scripted turn reads the tool result it was shown),
the test compares them with the metrics artifact the tool wrote, and metrics.expected.json pins
the accuracy of the default one-call baseline on the shipped dataset (recorded; compared with a
small tolerance because logistic-regression fits are not bit-identical across BLAS builds).
```

2. Imports: drop `register_dataset`; add `from thymira.agents.llm.base import LLMMessage` (under `TYPE_CHECKING` if only annotated) and keep `LLMToolCall`.
3. Delete `_TRAINING_CODE` (lines 86-116) and `_write_conformant_rows` (lines 143-155).
4. Add, after `_COLUMNS`:

```python
_METRIC_TOLERANCE = 0.02


def _report_what_run_experiment_returned(messages: Sequence[LLMMessage]) -> LLMToolCall:
    """The experiment agent's final answer: exactly the metrics the tool reported, nothing typed."""
    tool_output = next(m.content for m in reversed(messages) if m.role == "tool")
    reported = json.loads(tool_output.splitlines()[0])
    return LLMToolCall(
        id="e2",
        name="final_result",
        arguments={
            "parameters": {"seed": "7", "model": "logistic_regression"},
            "metrics": reported["metrics"],
            "seed": 7,
            "model_artifact_id": None,
            "tracker_run_id": "run_" + "a" * 32,
        },
    )
```

5. In the test body: replace lines 186-187 (`dataset = …` / `_write_conformant_rows(...)`) with

```python
    (workspace / "data").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "data" / "german_credit.csv", workspace / "data" / "applications.csv")
```

   delete line 196 (`register_dataset(store, dataset, "german_credit", produced_by=new_id("agent"))`); in the `e1` `run_experiment` call delete the `"code": _TRAINING_CODE,` argument; replace the whole `e2` `LLMToolCall(...)` item with `_report_what_run_experiment_returned,`.

6. After `assert verify_log(...).valid` add:

```python
    assert output.datasets == ("german_credit",) if hasattr(output, "datasets") else True
    reported = output.recommendation.metrics
    written = store.load_json("metrics/thymira-experiment.json")
    assert reported == written, "the agent reported metrics the tool did not write"
    if RECORD:
        _expect("metrics.expected.json", json.dumps(written, sort_keys=True) + "\n")
    else:
        expected = json.loads((ACCEPTANCE_DIR / "metrics.expected.json").read_text("utf-8"))
        assert abs(written["accuracy"] - expected["accuracy"]) <= _METRIC_TOLERANCE, (
            f"the default baseline's accuracy drifted: {written} vs {expected}; run with "
            "THYMIRA_SNAPSHOT=record and review the diff"
        )
```

   Check `ThyOutput` (`runtime/thy/src/thymira/thy/models.py`) for a `datasets` field: if `run_thy` does not project `ThyState.datasets` onto `ThyOutput`, assert on the store instead — `assert "datasets/german_credit.schema.json" in {a.name for a in store.list_active()}` — and drop the `hasattr` line; do not add a field to `ThyOutput` in this task.

7. `tests/thymira/acceptance/README.md`: add a line for `metrics.expected.json` (what it pins, the tolerance and why, how to refresh).

- [ ] **Step 2: Record the metric sidecar and run the acceptance test**

Run once: `THYMIRA_SNAPSHOT=record uv run pytest tests/thymira/test_acceptance_demo_run.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2` (PowerShell: `$env:THYMIRA_SNAPSHOT="record"; uv run pytest …; Remove-Item Env:THYMIRA_SNAPSHOT`).
Then `git status --short tests/thymira/acceptance/`: only `metrics.expected.json` may be new; `workspace.expected.txt`, both `*.system-prompt.expected.md` and both `*.tool-schemas.expected.json` must be unchanged. If any of them changed, stop and find out why before continuing (the schemas, prompts and artifact names are not supposed to move in this plan).
Run again without the variable: `uv run pytest tests/thymira/test_acceptance_demo_run.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt2`
Expected: PASS, and `metrics.expected.json` contains a plausible accuracy for a logistic baseline on German credit (roughly 0.70-0.80).

- [ ] **Step 3: The record**

`CHANGELOG.md` `[Unreleased]`: delete the `### Known issues` block (lines 19-24; both issues are fixed here). Add under `### Added`:

```markdown
- **A project declares its datasets, and THY registers them at intake.** `ProjectConfig.datasets`
  (Contract 0.8: `name`, a `path` relative to and contained in the project directory, an optional
  `target`) lists the data a Run works on; the Inspect node registers each declared dataset into
  the Run's artifact store before any agent runs — idempotently, so a resumed Run re-enters
  Inspect safely — records their names on `ThyState.datasets` for the planner, and halts the
  graph before Plan when a declared dataset is missing or unreadable (a configuration error is a
  pre-Execute halt, exactly like a rejected plan). Registration is a runtime intake step outside
  the Tool Manager, like Summarize's report: the Policy Engine decides what agents may do with the
  data, not whether the project may declare it. `examples/credit-risk` declares `german_credit`
  at `data/applications.csv`.
```

Under `### Fixed`:

```markdown
- **The shipped demo dataset registers as shipped.** `data/german_credit.csv` lost the two rows
  that were missing a field (lines 391 and 789; 998 rows remain), and `register_dataset` now
  refuses a ragged CSV with the 1-based line numbers of the offending rows instead of polars'
  raw error. The four test copies of the "drop the ragged rows" workaround are gone.
- **`run_experiment`'s default baseline handles categorical columns.** The default training
  script is a persisted scikit-learn `Pipeline`: numeric columns are cast, every other column is
  one-hot encoded, labels stay raw — so `audit_model` audits the artifact unchanged and the
  one-call baseline runs end to end on the demo dataset. Callers that passed their own `code`
  to work around `float(row[name])` no longer need to.
- **An unreadable runtime context no longer aborts the step.** When the snapshot cannot be
  rendered (an unreadable store, a corrupt dataset schema), the runner records the reason as the
  model-visible snapshot itself and the step runs; previously the exception escaped before
  `agent.started` and took the Run down with it.
- The acceptance Run (`tests/thymira/test_acceptance_demo_run.py`) now registers its dataset
  through Inspect, trains the default baseline, reports the metrics the tool returned (a
  callable `ScriptedProvider` turn) and pins the real accuracy in `metrics.expected.json`.
```

`docs/adr/0013-harness-fundamentals-six-decisions.md`: append to the last Consequences bullet: "Both were fixed by the second pull request (2026-09-05, plan `docs/superpowers/plans/2026-09-05-harness-basics-2.md`), which also moved dataset registration into Inspect."

`AGENTS.md`, repository map, the `runtime/thy` sentence: change "ThyGraph (Inspect → Plan → Execute → Summarize)" to "ThyGraph (Inspect — which registers the project's declared datasets — → Plan → Execute → Summarize)".

- [ ] **Step 4: The gate**

Run: `just check` (lint, ty gate, imports, skills, roadmap, fast lane) and then `just test-slow`.
Expected: all clean; fast lane count ≥ 1709 + the new tests; slow lane passes.

- [ ] **Step 5: Commit**

```bash
git add tests/thymira/test_acceptance_demo_run.py tests/thymira/acceptance/metrics.expected.json tests/thymira/acceptance/README.md CHANGELOG.md docs/adr/0013-harness-fundamentals-six-decisions.md AGENTS.md
git commit -m "test(acceptance): the demo Run registers through Inspect, trains the default baseline and pins the real metric" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k"
```

---

## Out of scope (harness basics 3)

Failure as data across the Run (an `end_reason` on `agent.completed`, the three failure classes), the real gate (denial as a result fact with a stable marker, one same-call escalation with `justification` re-run through the deterministic engine, `never` checked in `_constraint_error` before any answerer, the human answer through the risk-interview park/resume pattern, identity by event provenance, the governance route's anonymous-approval residual), driving the acceptance Run through the Core composition so `artifact.created` is emitted for the datasets Inspect registers, the verbatim log with export-time redaction (decision 1), the fail-closed sandbox (decision 2), the log format version (decision 6).
