# Harness basics 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the demo Run visible and honest: every model request records the exact system prompt and tool schemas it sent, every sub-agent gets an environment block and a superseding runtime-context message, the eight core tools have descriptions that teach the model, mutating tools carry a mandatory `description`, tool results render with `[exit code: N]` last, and an acceptance test pins all of it against committed sidecars and a workspace oracle.

**Architecture:** Nothing new is invented. `PromptBuilder` gains a `PromptEnvironment` (persona line + per-tool guidance) that is stable within a Run, so prefix caching still holds; the dynamic facts (workspace, sandbox mode, registered datasets, artifacts) travel as one `agent.message` event with `form: runtime_context` and `surface: MODEL_VISIBLE`, and the builder keeps only the latest one, which is how "this snapshot supersedes earlier snapshots" is enforced by the fold and not by hope. Tools keep the `Tool` protocol; `read_file` learns paging, `edit_file`/`glob`/`grep` are new builtins, and the tool bridge renders one canonical text projection of `ToolResult`. The acceptance test drives the real `ThyGraph` with real builtin tools and a `ScriptedProvider`, and compares what was sent to the model with committed sidecars (dsh's recorded-session discipline).

**Tech Stack:** Python 3.12+, pydantic, PydanticAI (`Agent`, `Tool.from_schema`), LangGraph (`ThyGraph`), polars, scikit-learn (the experiment tool), pytest (`integration` marker), ruff, ty, import-linter.

**Spec:** `docs/adr/0013-harness-fundamentals-six-decisions.md` (the six decisions and the first-PR scope) and, for the verbatim model-facing texts, the reading report "DeepSeek Harness: qué copiar" sections 3 (F3, F4, F10) and 5.

## Global Constraints

- Layering: `agents → tools → policies → state → events → schemas` (import-linter). `thymira.agents` may import `thymira.tools`; `thymira.tools` never imports `thymira.agents` or `thymira.thy`.
- ruff full rule set, `line-length = 100`, Google docstrings on every public symbol; ty zero diagnostics; `# noqa: CODE  # reason` only per line.
- Records that cross a boundary are frozen pydantic models (`ThymiraModel`, `extra="forbid"`); in-process helpers are `@dataclass(frozen=True, slots=True)`.
- Tests: unit tests in `tests/thymira/test_<member>.py` with no marker; the acceptance test is `integration` (it spawns the Python sandbox subprocess). LLM boundary only through `ScriptedProvider`. Files only under `tmp_path`. Run pytest with a short `--basetemp` (for example `C:/Users/lucas/AppData/Local/Temp/thy-bt1`) on Windows: a long temp path exceeds `MAX_PATH` and fails with `FileNotFoundError`.
- `uv run …`, never global `python`. Commit messages: Conventional Commits, English, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01BkX3MhMkFHcwQB8AGZ1h4k`.
- No new abstraction without a caller in the same task. No new event type (`EventType` is closed; use `agent.message` with a `form` payload field).
- Everything in English (code, comments, docs, commits).
- Baseline before any change: `uv run pytest -q -m "not slow" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider` → `1662 passed, 3 skipped, 31 deselected` at commit `ca7f233`.

---

## File structure

| File | Responsibility |
|---|---|
| `runtime/agents/src/thymira/agents/tool_bridge.py` | Modify: `render_tool_output(execution) -> str`, the one canonical text projection of a `ToolExecution` the model sees |
| `runtime/tools/src/thymira/tools/builtins/files.py` | Modify: `read_file` paging + line marker, `write_file` `description`, new `EditFile` tool, dsh-style descriptions |
| `runtime/tools/src/thymira/tools/builtins/search.py` | Create: `Glob` and `Grep` tools (workspace-contained, capped, deterministic) |
| `runtime/tools/src/thymira/tools/builtins/run_python.py` | Modify: `description` argument, dsh-style description, register `EditFile`/`Glob`/`Grep` |
| `runtime/tools/src/thymira/tools/builtins/run_experiment.py`, `data_analysis.py` | Modify: `description` argument on `run_experiment`; dsh-style descriptions on `profile_dataset` |
| `runtime/tools/src/thymira/tools/guidance.py` | Create: `tool_guidance(registry, names) -> tuple[str, ...]`, the per-tool "when to use" paragraphs |
| `runtime/tools/src/thymira/tools/__init__.py` | Modify: export `tool_guidance` |
| `runtime/agents/src/thymira/agents/prompts.py` | Modify: `PromptEnvironment`, persona line, guidance section, latest-runtime-context fold |
| `runtime/agents/src/thymira/agents/runtime_context.py` | Create: `runtime_context_text(...)` and `RUNTIME_CONTEXT_FORM` |
| `runtime/agents/src/thymira/agents/runner.py` | Modify: build the environment, append the runtime-context event, pass both to the builder |
| `runtime/agents/src/thymira/agents/__init__.py` | Modify: export `PromptEnvironment`, `runtime_context_text` |
| `tests/thymira/test_agents_tool_bridge.py` | Modify: rendering tests |
| `tests/thymira/test_tools_files.py` | Create: paging, edit, glob, grep tests |
| `tests/thymira/test_tools.py` (and any test calling `run_python`/`write_file`/`run_experiment`) | Modify: add `description` to scripted arguments |
| `tests/thymira/test_agents_prompts.py`, `test_agents_runner.py` | Modify: environment, guidance, runtime-context tests |
| `tests/thymira/test_acceptance_demo_run.py` | Create: the recorded demo Run |
| `tests/thymira/acceptance/` | Create: `data.system-prompt.expected.md`, `data.tool-schemas.expected.json`, `experiment.system-prompt.expected.md`, `experiment.tool-schemas.expected.json`, `workspace.expected.txt` |
| `docs/adr/0013-harness-fundamentals-six-decisions.md`, `docs/adr/README.md` | Already written in the worktree; committed in Task 0 |
| `CHANGELOG.md`, `AGENTS.md`, `runtime/tools/README.md` | Modify in Task 7 |

---

### Task 0: Commit the ADR

**Files:**
- Already modified in the worktree: `docs/adr/0013-harness-fundamentals-six-decisions.md`, `docs/adr/README.md`, `docs/superpowers/plans/2026-09-04-harness-basics-1.md`

- [ ] **Step 1: Check the docs gate**

Run: `uv run python scripts/check_roadmap.py` (must still pass; the ADR touches no roadmap task) and `git status --short`.
Expected: only the three files above are listed.

- [ ] **Step 2: Commit**

```bash
git add docs/adr/0013-harness-fundamentals-six-decisions.md docs/adr/README.md docs/superpowers/plans/2026-09-04-harness-basics-1.md
git commit -m "docs(adr): ADR-0013 records the six harness decisions after the dsh reading [skip changelog]"
```

---

### Task 1: One canonical text projection of a tool result

**Files:**
- Modify: `runtime/agents/src/thymira/agents/tool_bridge.py:71-94`
- Test: `tests/thymira/test_agents_tool_bridge.py`

**Interfaces:**
- Consumes: `thymira.tools.ToolExecution` (`.result: ToolResult` with `success`, `stdout`, `stderr`, `exit_code`, `error`).
- Produces: `render_tool_output(execution: ToolExecution) -> str`, exported from `thymira.agents.tool_bridge` and used by `_build_one_tool`. Rules, in this order: a failed or denied call renders `Error: <error>`; a successful call renders `stdout` (or `(no output)` when empty); when `stderr` is non-empty, append a blank line then `stderr:` and the last 40 lines of it; when `exit_code is not None`, the last line is always `[exit code: N]` (kept last because a parser anchors on it, as dsh does).

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_agents_tool_bridge.py`:

```python
from thymira.agents.tool_bridge import render_tool_output
from thymira.schemas import ToolCall, ToolCallStatus, new_id
from thymira.tools import ToolExecution, ToolResult


def _execution(result: ToolResult) -> ToolExecution:
    call = ToolCall(
        id=new_id("tool"),
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        tool_name="run_python",
        arguments={},
        status=ToolCallStatus.COMPLETED if result.success else ToolCallStatus.FAILED,
    )
    return ToolExecution(call=call, result=result)


def test_render_puts_the_exit_code_marker_last_after_stdout_and_stderr():
    rendered = render_tool_output(
        _execution(ToolResult(success=True, stdout="42\n", stderr="warn\n", exit_code=0))
    )

    assert rendered.splitlines()[0] == "42"
    assert "stderr:\nwarn" in rendered
    assert rendered.splitlines()[-1] == "[exit code: 0]"


def test_render_reports_a_failure_as_an_error_line_and_keeps_the_exit_code():
    rendered = render_tool_output(
        _execution(ToolResult(success=False, error="python execution failed", exit_code=1))
    )

    assert rendered.splitlines()[0] == "Error: python execution failed"
    assert rendered.splitlines()[-1] == "[exit code: 1]"


def test_render_says_no_output_when_a_successful_call_printed_nothing():
    rendered = render_tool_output(_execution(ToolResult(success=True)))

    assert rendered == "(no output)"


def test_render_keeps_only_the_tail_of_a_long_stderr():
    noisy = "\n".join(f"line {i}" for i in range(100)) + "\n"
    rendered = render_tool_output(
        _execution(ToolResult(success=True, stdout="ok", stderr=noisy, exit_code=0))
    )

    assert "line 99" in rendered
    assert "line 0\n" not in rendered
    assert "[stderr truncated to the last 40 lines]" in rendered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_agents_tool_bridge.py -q -k render`
Expected: FAIL with `ImportError: cannot import name 'render_tool_output'`.

- [ ] **Step 3: Implement `render_tool_output`**

In `runtime/agents/src/thymira/agents/tool_bridge.py`, add above `_build_one_tool`:

```python
STDERR_TAIL_LINES = 40
"""How many trailing stderr lines the model sees; the full text stays on the ToolCall."""


def render_tool_output(execution: ToolExecution) -> str:
    """Render the one text projection of a tool result the model reads.

    The order is fixed and the exit-code marker is always the last line, so a reader (model or
    parser) can anchor on it the way dsh's ``[exit code: N]`` marker is anchored: a denied or
    failed call is ``Error: <reason>``; a successful call is its stdout or ``(no output)``; a
    non-empty stderr follows as a bounded tail; the marker closes when the tool ran a process.
    """
    result = execution.result
    lines: list[str] = []
    if not result.success:
        lines.append(f"Error: {result.error or 'tool call failed'}")
    else:
        lines.append(result.stdout.rstrip("\n") if result.stdout.strip() else "(no output)")
    if result.stderr.strip():
        stderr_lines = result.stderr.rstrip("\n").splitlines()
        if len(stderr_lines) > STDERR_TAIL_LINES:
            stderr_lines = [
                f"[stderr truncated to the last {STDERR_TAIL_LINES} lines]",
                *stderr_lines[-STDERR_TAIL_LINES:],
            ]
        lines.append("")
        lines.append("stderr:")
        lines.extend(stderr_lines)
    if result.exit_code is not None:
        lines.append(f"[exit code: {result.exit_code}]")
    return "\n".join(lines)
```

Add `ToolExecution` to the runtime import from `thymira.tools` (it is used at run time now, not only for typing), and replace the body of `_call` so it uses the renderer:

```python
    def _call(**arguments: Any) -> str:
        with tool_observation(name=tool_name, arguments=arguments) as observation:
            execution = manager.execute(context, tool_name, arguments)
            if usage is not None:
                usage.charge_tool()
            output = render_tool_output(execution)
            observation.update(output=_bounded(output))
            return output
```

Add `"render_tool_output"` to `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_agents_tool_bridge.py -q`
Expected: PASS. If an existing test asserted the old `error: …` or `ok` text, update that assertion to the new rendering (`Error: …`, `(no output)`); do not weaken it.

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check runtime/agents tests/thymira/test_agents_tool_bridge.py && uv run ruff format --check runtime/agents tests/thymira/test_agents_tool_bridge.py && uv run ty check runtime/agents`
Expected: clean.

```bash
git add runtime/agents/src/thymira/agents/tool_bridge.py tests/thymira/test_agents_tool_bridge.py
git commit -m "feat(agents): render tool results for the model with the exit-code marker last"
```

---

### Task 2: A mandatory `description` on every mutating tool

**Files:**
- Modify: `runtime/tools/src/thymira/tools/builtins/run_python.py:31-35`, `builtins/files.py:42-50`, `builtins/run_experiment.py:28-36`
- Test: `tests/thymira/test_tools.py` (new cases) plus every existing test that calls `run_python`, `write_file` or `run_experiment` with scripted arguments (`grep -rn "run_python\|write_file\|run_experiment" tests/thymira`)

**Interfaces:**
- Produces: the three argument models gain a required `description: str` field; the model-facing JSON schema (through `input_schema`) lists it as required; `ToolManager` already validates and records it, so `tool.started.payload["arguments"]["description"]` carries it.

The field, identical on the three models (only the first example differs per tool):

```python
DESCRIPTION_FIELD = Field(
    min_length=1,
    max_length=200,
    description=(
        "Clear, concise description of what this call does in active voice, 5-10 words "
        '(shown in the UI and recorded as evidence). Examples: "Profile missing values per '
        'column"; "Train the logistic-regression baseline"; "Write the feature-engineering '
        'script".'
    ),
)
```

Put `DESCRIPTION_FIELD` in `runtime/tools/src/thymira/tools/builtins/files.py` next to `_MAX_WRITE_BYTES` and import it from there in the other two modules (files.py is already imported by run_python.py; run_experiment.py adds the import).

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_tools.py`:

```python
import pytest
from pydantic import ValidationError

from thymira.tools.builtins.files import WriteFileArguments
from thymira.tools.builtins.run_experiment import RunExperimentArguments
from thymira.tools.builtins.run_python import RunPythonArguments


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        (RunPythonArguments, {"code": "print(1)"}),
        (WriteFileArguments, {"path": "a.txt", "content": "x"}),
        (RunExperimentArguments, {"dataset": "d", "target_column": "y"}),
    ],
)
def test_mutating_tool_arguments_require_a_description(model, arguments):
    with pytest.raises(ValidationError, match="description"):
        model.model_validate(arguments)


def test_run_python_schema_advertises_description_as_required():
    schema = RunPythonArguments.model_json_schema()

    assert "description" in schema["required"]
    assert "5-10 words" in schema["properties"]["description"]["description"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py -q -k description`
Expected: FAIL (`DID NOT RAISE` and `KeyError: 'required'` or `'description' not in …`).

- [ ] **Step 3: Add the field to the three models**

`files.py`:

```python
DESCRIPTION_FIELD = Field(
    min_length=1,
    max_length=200,
    description=(
        "Clear, concise description of what this call does in active voice, 5-10 words "
        '(shown in the UI and recorded as evidence). Examples: "Profile missing values per '
        'column"; "Train the logistic-regression baseline"; "Write the feature-engineering '
        'script".'
    ),
)


class WriteFileArguments(BaseModel):
    """Arguments for write_file."""

    path: str = Field(min_length=1)
    content: str
    description: str = DESCRIPTION_FIELD
    kind: ArtifactKind | None = Field(
        default=None,
        description="When set, also register the written file as an Artifact of this kind.",
    )
```

`run_python.py`:

```python
class RunPythonArguments(BaseModel):
    """Validated arguments for run_python."""

    code: str = Field(min_length=1)
    description: str = DESCRIPTION_FIELD
    timeout_s: float = Field(default=30.0, gt=0, le=300)
```

`run_experiment.py`: add `description: str = DESCRIPTION_FIELD` after `target_column`, importing `DESCRIPTION_FIELD` from `thymira.tools.builtins.files`.

Export `DESCRIPTION_FIELD` in `files.py`'s `__all__`.

- [ ] **Step 4: Fix every scripted call in the existing tests**

Run: `uv run pytest -q -m "not slow" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider -x`
For each failure of the form `description: Field required`, add `"description": "<5-10 words>"` to that test's arguments (for example `{"code": "print(1)", "description": "Print one"}`). Also grep the THY agent prompts (`runtime/thy/src/thymira/thy/agents/prompts/*.md`) and the THY agent tests (`tests/thymira/test_thy_agent_*.py`) for scripted `LLMToolCall(... name="run_python" ...)`/`"write_file"`/`"run_experiment"` and add the field there too. Do not change `_FakeTool` doubles that carry their own `arguments_model`.
Expected after the fixes: the fast lane passes again (`1662 + 5 passed`).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check runtime/tools tests && uv run ruff format --check runtime/tools tests && uv run ty check runtime/tools`

```bash
git add runtime/tools tests
git commit -m "feat(tools): mutating tools require a 5-10 word description argument"
```

---

### Task 3: The file tools the model expects: paged read, edit, glob, grep, and descriptions that teach

**Files:**
- Modify: `runtime/tools/src/thymira/tools/builtins/files.py` (paging on `read_file`, new `EditFile`, descriptions)
- Create: `runtime/tools/src/thymira/tools/builtins/search.py` (`Glob`, `Grep`)
- Modify: `runtime/tools/src/thymira/tools/builtins/run_python.py:81-110` (register the three new tools; dsh-style description), `builtins/data_analysis.py:125` (`profile_dataset` description), `builtins/run_experiment.py:45` (description)
- Test: `tests/thymira/test_tools_files.py` (new)

**Interfaces:**
- Produces tools named `edit_file`, `glob`, `grep` registered by `builtins_registry()`; `read_file` accepts `offset` (1-based line, default 1) and `limit` (default 2000) and, when the window is partial, ends with `[showing lines A-B of N]`.
- Capabilities (honest metadata): `edit_file` → `data_access=("workspace",)`, `side_effects=("workspace_write",)`, `external_effects=()`, `reversibility="reversible"`; `glob`/`grep` → `data_access=("workspace",)`, `external_effects=()`.
- Model-facing descriptions (verbatim; they are the contract):
  - `read_file`: `Read a UTF-8 text file from the workspace and return its content. Use offset and limit to continue reading a large file; a partial window ends with "[showing lines A-B of N]". Use profile_dataset, not read_file, to understand a registered dataset.`
  - `write_file`: `Create or fully replace a UTF-8 text file inside the workspace. Existing files are overwritten, so read an existing file first and prefer edit_file for targeted changes. Pass kind to register the file as an Artifact.`
  - `edit_file`: `Edit an existing UTF-8 text file inside the workspace by replacing literal text. old_string must match exactly and, unless replace_all is true, appear exactly once; the error tells you when it does not. Read the file first unless you just created or edited it.`
  - `glob`: `Find files in the workspace whose paths match a glob pattern. Returns file paths only, never directories, in modification-time order; a pattern with no "/" matches the basename at any depth. At most 100 paths come back; a larger result says how many were omitted.`
  - `grep`: `Search file contents in the workspace with a Python regular expression. Returns matching lines as "path:line: text", grouped by file, at most 250 matches; a capped result says so. Use read_file on a matched file for surrounding context.`
  - `run_python`: `Execute Python code in a fresh interpreter with write access to the workspace and a bounded timeout. Nothing persists between calls: write files for anything the next call needs. Non-zero exits are reported as "[exit code: N]"; check that marker on every result and investigate a failure before moving on. Long output is truncated to its tail. The script runs under the workspace-write file sandbox: a blocked file operation is a policy denial, not a bug in the code, so do not retry another way.`
  - `profile_dataset`: `Create a deterministic JSON profile (missingness, distributions, numeric correlations) of a registered dataset and save it as a report artifact. Use it before modelling; it is the only correct way to look at a dataset, because the profile is bounded and reproducible.`
  - `run_experiment`: `Train and evaluate a deterministic baseline classifier on a registered dataset, track it in the local experiment tracker and save the model and its metrics as artifacts. Report only the metrics the result carries; never a number you have not seen in the output.`

- [ ] **Step 1: Write the failing tests**

Create `tests/thymira/test_tools_files.py`:

```python
"""Workspace file tools: paged reads, literal edits, glob and grep (harness basics 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from thymira.schemas import new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation
from thymira.tools.builtins.files import EditFile, ReadFile
from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.builtins.search import Glob, Grep
from thymira.tools.models import ToolExecutionError


def _invocation(tmp_path: Path) -> ToolInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def test_read_file_returns_a_window_and_says_which_lines_it_showed(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "big.txt").write_text(
        "\n".join(f"row {i}" for i in range(1, 11)) + "\n", encoding="utf-8"
    )

    result = ReadFile().execute(invocation, {"path": "big.txt", "offset": 3, "limit": 2})

    assert result.stdout == "row 3\nrow 4\n[showing lines 3-4 of 10]"


def test_read_file_returns_the_whole_file_without_a_marker_when_it_fits(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "small.txt").write_text("a\nb\n", encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "small.txt"})

    assert result.stdout == "a\nb\n"


def test_edit_file_replaces_a_unique_literal_and_reports_the_count(tmp_path: Path):
    invocation = _invocation(tmp_path)
    target = invocation.workspace / "script.py"
    target.write_text("x = 1\ny = x + 1\n", encoding="utf-8")

    result = EditFile().execute(
        invocation,
        {
            "path": "script.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
            "description": "Bump x",
        },
    )

    assert result.success
    assert target.read_text(encoding="utf-8") == "x = 2\ny = x + 1\n"
    assert "1 replacement" in result.stdout


def test_edit_file_refuses_an_ambiguous_old_string_and_says_how_to_fix_it(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "dup.txt").write_text("a\na\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="appears 2 times.*replace_all"):
        EditFile().execute(
            invocation,
            {"path": "dup.txt", "old_string": "a", "new_string": "b", "description": "Rename"},
        )


def test_edit_file_refuses_a_missing_old_string(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "f.txt").write_text("hello\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="old_string was not found"):
        EditFile().execute(
            invocation,
            {"path": "f.txt", "old_string": "bye", "new_string": "x", "description": "Edit"},
        )


def test_edit_file_replace_all_replaces_every_occurrence(tmp_path: Path):
    invocation = _invocation(tmp_path)
    target = invocation.workspace / "dup.txt"
    target.write_text("a a a\n", encoding="utf-8")

    result = EditFile().execute(
        invocation,
        {
            "path": "dup.txt",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
            "description": "Rename all",
        },
    )

    assert target.read_text(encoding="utf-8") == "b b b\n"
    assert "3 replacements" in result.stdout


def test_glob_returns_files_only_matching_basenames_at_any_depth(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "src").mkdir()
    (invocation.workspace / "src" / "a.py").write_text("", encoding="utf-8")
    (invocation.workspace / "b.py").write_text("", encoding="utf-8")
    (invocation.workspace / "notes.md").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.py"})

    assert set(result.stdout.splitlines()) == {"src/a.py", "b.py"}


def test_glob_caps_the_result_and_says_how_many_were_omitted(tmp_path: Path):
    invocation = _invocation(tmp_path)
    for i in range(105):
        (invocation.workspace / f"f{i:03d}.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    lines = result.stdout.splitlines()
    assert len(lines) == 101
    assert lines[-1] == "[100 of 105 paths shown; narrow the pattern to see the rest]"


def test_grep_groups_matches_by_file_with_line_numbers(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("import os\nx = 1\n", encoding="utf-8")
    (invocation.workspace / "b.py").write_text("import sys\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": r"^import \w+", "include": "*.py"})

    assert result.stdout == "a.py:1: import os\nb.py:1: import sys"


def test_grep_rejects_an_invalid_regular_expression(tmp_path: Path):
    invocation = _invocation(tmp_path)

    with pytest.raises(ToolExecutionError, match="invalid regular expression"):
        Grep().execute(invocation, {"pattern": "("})


def test_builtins_registry_holds_the_eight_core_tools():
    names = {tool.name for tool in builtins_registry().tools()}

    assert {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "run_python",
        "profile_dataset",
        "run_experiment",
    } <= names
```

If `ToolRegistry` has no `tools()` accessor, read `runtime/tools/src/thymira/tools/registry.py` and use the accessor it does have (for example `registry.names()` or iterate `registry.capabilities()` ids); do not add one only for this test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools_files.py -q`
Expected: FAIL at import (`cannot import name 'EditFile'`, no module `search`).

- [ ] **Step 3: Implement paging and `EditFile` in `files.py`**

```python
_READ_DEFAULT_LIMIT = 2000
_GLOB_CAP = 100


class ReadFileArguments(BaseModel):
    """Arguments for read_file."""

    path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1, description="1-based first line to return.")
    limit: int = Field(
        default=_READ_DEFAULT_LIMIT,
        ge=1,
        le=20_000,
        description="Maximum number of lines to return. Defaults to 2000.",
    )


class EditFileArguments(BaseModel):
    """Arguments for edit_file."""

    path: str = Field(min_length=1)
    old_string: str = Field(min_length=1, description="Literal text to replace. Must match exactly.")
    new_string: str = Field(
        description="Literal replacement text. Use an empty string to delete the match."
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all matches. Defaults to false; then old_string must appear exactly once.",
    )
    description: str = DESCRIPTION_FIELD
```

`ReadFile.execute` becomes:

```python
    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Read the selected window of the file, marking a partial window."""
        path = _contained(Path(invocation.workspace), arguments["path"])
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {arguments['path']!r}: {exc}") from exc
        offset = int(arguments.get("offset", 1))
        limit = int(arguments.get("limit", _READ_DEFAULT_LIMIT))
        lines = content.splitlines(keepends=True)
        total = len(lines)
        window = lines[offset - 1 : offset - 1 + limit]
        if offset == 1 and len(window) == total:
            return ToolResult(success=True, stdout=content)
        shown_to = offset - 1 + len(window)
        body = "".join(window)
        if not body.endswith("\n") and body:
            body += "\n"
        return ToolResult(
            success=True, stdout=f"{body}[showing lines {offset}-{shown_to} of {total}]"
        )
```

`EditFile`:

```python
def _edit_capability() -> ToolCapability:
    return ToolCapability(
        id="edit_file",
        data_access=("workspace",),
        side_effects=("workspace_write",),
        external_effects=(),
        reversibility="reversible",
    )


@dataclass(frozen=True, slots=True)
class EditFile:
    """Replace literal text inside an existing workspace file."""

    name: str = "edit_file"
    description: str = (
        "Edit an existing UTF-8 text file inside the workspace by replacing literal text. "
        "old_string must match exactly and, unless replace_all is true, appear exactly once; "
        "the error tells you when it does not. Read the file first unless you just created or "
        "edited it."
    )
    arguments_model: type[BaseModel] = EditFileArguments
    capability: ToolCapability = field(default_factory=_edit_capability)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Apply the literal replacement after containment and uniqueness checks."""
        path = _contained(Path(invocation.workspace), arguments["path"])
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {arguments['path']!r}: {exc}") from exc
        old, new = arguments["old_string"], arguments["new_string"]
        count = content.count(old)
        if count == 0:
            raise ToolExecutionError(
                f"old_string was not found in {arguments['path']!r} — read the file, then retry"
            )
        if count > 1 and not arguments.get("replace_all", False):
            raise ToolExecutionError(
                f"old_string appears {count} times in {arguments['path']!r}; provide a more "
                "specific old_string or set replace_all to true"
            )
        updated = content.replace(old, new) if arguments.get("replace_all") else content.replace(old, new, 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_WRITE_BYTES:
            raise ToolExecutionError(f"edit exceeds {_MAX_WRITE_BYTES} bytes")
        path.write_bytes(encoded)
        replaced = count if arguments.get("replace_all") else 1
        noun = "replacement" if replaced == 1 else "replacements"
        return ToolResult(success=True, stdout=f"{replaced} {noun} in {arguments['path']}")
```

Replace the `ReadFile`/`WriteFile` descriptions with the verbatim texts from the Interfaces block. Add `EditFile` and `EditFileArguments` to `__all__`.

- [ ] **Step 4: Implement `search.py`**

```python
"""Workspace search tools: glob and grep (harness basics 1)."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from thymira.policies import ToolCapability
from thymira.tools.builtins.files import _contained
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult

GLOB_CAP = 100
GREP_CAP = 250
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024


class GlobArguments(BaseModel):
    """Arguments for glob."""

    pattern: str = Field(
        min_length=1,
        description=(
            'Glob pattern to match file paths against (e.g. "**/*.py", "src/**/*.csv"). A '
            'pattern with no "/" matches the basename at any depth.'
        ),
    )
    path: str = Field(default=".", description="Directory to search in, workspace-relative.")


class GrepArguments(BaseModel):
    """Arguments for grep."""

    pattern: str = Field(min_length=1, description="Python regular expression to search for.")
    path: str = Field(default=".", description="File or directory to search, workspace-relative.")
    include: str | None = Field(
        default=None,
        description='One glob filter for which files to search (e.g. "*.py"). Not a list.',
    )


def _workspace_files(root: Path, directory: Path) -> list[Path]:
    return [
        candidate
        for candidate in directory.rglob("*")
        if candidate.is_file()
        and candidate.resolve(strict=False).is_relative_to(root)
        and ".git" not in candidate.relative_to(root).parts
    ]


@dataclass(frozen=True, slots=True)
class Glob:
    """Find workspace files by path pattern."""

    name: str = "glob"
    description: str = (
        "Find files in the workspace whose paths match a glob pattern. Returns file paths only, "
        'never directories, in modification-time order; a pattern with no "/" matches the '
        "basename at any depth. At most 100 paths come back; a larger result says how many "
        "were omitted."
    )
    arguments_model: type[BaseModel] = GlobArguments
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="glob", data_access=("workspace",), external_effects=()
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return matching files, newest first, capped at ``GLOB_CAP``."""
        root = Path(invocation.workspace).resolve()
        directory = _contained(root, arguments.get("path", "."))
        if not directory.is_dir():
            raise ToolExecutionError("path is not a directory")
        pattern = arguments["pattern"]
        matches = [
            candidate
            for candidate in _workspace_files(root, directory)
            if _glob_matches(pattern, candidate.relative_to(root).as_posix())
        ]
        matches.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
        shown = matches[:GLOB_CAP]
        lines = [candidate.relative_to(root).as_posix() for candidate in shown]
        if len(matches) > GLOB_CAP:
            lines.append(
                f"[{GLOB_CAP} of {len(matches)} paths shown; narrow the pattern to see the rest]"
            )
        return ToolResult(success=True, stdout="\n".join(lines))


def _glob_matches(pattern: str, relative_posix: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatch(relative_posix.rsplit("/", 1)[-1], pattern)
    if pattern.startswith("**/"):
        tail = pattern[3:]
        return fnmatch.fnmatch(relative_posix, tail) or fnmatch.fnmatch(relative_posix, pattern)
    return fnmatch.fnmatch(relative_posix, pattern)


@dataclass(frozen=True, slots=True)
class Grep:
    """Search workspace file contents with a regular expression."""

    name: str = "grep"
    description: str = (
        "Search file contents in the workspace with a Python regular expression. Returns "
        'matching lines as "path:line: text", grouped by file, at most 250 matches; a capped '
        "result says so. Use read_file on a matched file for surrounding context."
    )
    arguments_model: type[BaseModel] = GrepArguments
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="grep", data_access=("workspace",), external_effects=()
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return the first ``GREP_CAP`` matches as ``path:line: text``."""
        root = Path(invocation.workspace).resolve()
        target = _contained(root, arguments.get("path", "."))
        try:
            regex = re.compile(arguments["pattern"])
        except re.error as exc:
            raise ToolExecutionError(f"invalid regular expression: {exc}") from exc
        include = arguments.get("include")
        files = [target] if target.is_file() else _workspace_files(root, target)
        files.sort(key=lambda candidate: candidate.relative_to(root).as_posix())
        lines: list[str] = []
        total = 0
        for candidate in files:
            relative = candidate.relative_to(root).as_posix()
            if include and not fnmatch.fnmatch(relative.rsplit("/", 1)[-1], include):
                continue
            if candidate.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    total += 1
                    if total <= GREP_CAP:
                        lines.append(f"{relative}:{number}: {line}")
        if total > GREP_CAP:
            lines.append(f"[{GREP_CAP} of {total} matches shown; narrow the pattern or path]")
        return ToolResult(success=True, stdout="\n".join(lines))


__all__ = ["Glob", "GlobArguments", "Grep", "GrepArguments"]
```

`_contained` is private in `files.py`; importing a private name across modules of the same member is acceptable here, but rename it to `contained_path` in `files.py`, keep `_contained = contained_path` for the existing call sites, and import `contained_path` in `search.py` (avoids `SLF001`-style lint on the import).

- [ ] **Step 5: Register the tools and set the remaining descriptions**

In `run_python.py`: import `EditFile` from `files` and `Glob, Grep` from `search`; add `EditFile(), Glob(), Grep()` to the `tools` tuple in `builtins_registry`; replace `RunPython.description` with the verbatim text from the Interfaces block. In `data_analysis.py` and `run_experiment.py` replace the two descriptions with their verbatim texts.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/thymira/test_tools_files.py tests/thymira/test_tools.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider`
Expected: PASS. Then the fast lane; a test that asserted an old description string must be updated to the new text.

- [ ] **Step 7: Lint, type-check, imports, commit**

Run: `uv run ruff check runtime/tools tests && uv run ruff format --check runtime/tools tests && uv run ty check runtime/tools && uv run python scripts/lint_imports.py`

```bash
git add runtime/tools tests/thymira/test_tools_files.py tests
git commit -m "feat(tools): paged read_file, edit_file, glob and grep with descriptions that teach the model"
```

---

### Task 4: Per-tool guidance and the persona line in the system prompt

**Files:**
- Create: `runtime/tools/src/thymira/tools/guidance.py`
- Modify: `runtime/tools/src/thymira/tools/__init__.py` (export), `runtime/agents/src/thymira/agents/prompts.py`, `runtime/agents/src/thymira/agents/runner.py:164-198`, `runtime/agents/src/thymira/agents/__init__.py` (export `PromptEnvironment`)
- Test: `tests/thymira/test_tools.py` (guidance), `tests/thymira/test_agents_prompts.py` (environment), `tests/thymira/test_agents_runner.py` (wiring)

**Interfaces:**
- Produces in `thymira.tools`: `tool_guidance(registry: ToolRegistry, names: Sequence[str]) -> tuple[str, ...]` returning one paragraph per named tool that has one, in the order given; unknown names raise `KeyError` (same contract as `build_agent_tools`). The paragraphs (verbatim, keyed by tool name, in `guidance.py`):
  - `read_file`: `Use the read_file tool — not run_python with open() — to inspect text files. Use offset and limit to continue reading a large file.`
  - `write_file`: `Use the write_file tool to create files or completely replace their contents. Existing files are overwritten, so read an existing file first and prefer edit_file for targeted changes.`
  - `edit_file`: `Use the edit_file tool for targeted changes to existing text files. It replaces literal old_string with new_string; by default old_string must appear exactly once. If it appears several times, provide a more specific old_string or set replace_all to true.`
  - `glob`: `Use the glob tool — not run_python with os.walk — to discover files by path pattern. A pattern with no "/" matches basenames at any depth. Results are files only, never directories, newest first.`
  - `grep`: `Use the grep tool — not run_python — to search file contents. Use read_file on a matched file when you need surrounding context.`
  - `run_python`: `Check the [exit code: N] marker on every run_python result; investigate a failure before moving on. Each call runs in a fresh interpreter: write files for anything the next call needs, and keep printed output short — long output is truncated.`
  - `profile_dataset`: `Use the profile_dataset tool to understand a registered dataset before modelling; do not read the raw file with read_file. Registered datasets are listed in the runtime context.`
  - `run_experiment`: `Use the run_experiment tool to train a baseline on a registered dataset; report only the metrics it returns, never a number you have not seen in its output.`
- Produces in `thymira.agents.prompts`: `PromptEnvironment(ThymiraModel)` with `agent_name: str`, `model: str`, `workspace: str`, `tool_guidance: tuple[str, ...] = ()`; `PromptBuilder.build(spec, task, events, *, environment: PromptEnvironment | None = None)`. With an environment, `assembled.system` is: the persona line `You are the {agent_name} agent of Thymira, powered by the {model} model. Your working directory is {workspace}.`, a blank line, the catalog prompt, and, when there is guidance, a blank line and the paragraphs joined by blank lines. Without an environment `system` is the catalog prompt alone (unchanged behaviour).
- `AgentRunner.run` builds the environment from `routing.choose(spec.role, task_kind, requested_tier=spec.tier).model` (pure, no provider; read `runtime/agents/src/thymira/agents/llm/routing.py:214-253` for the exact signature and the attribute holding the model id), `str(ctx.tool_context.workspace)` when a tool context is set (else `"(no workspace)"`), and `tool_guidance(ctx.tool_registry, spec.tool_allowlist)` when a registry is set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/thymira/test_tools.py`:

```python
from thymira.tools import tool_guidance
from thymira.tools.builtins.run_python import builtins_registry


def test_tool_guidance_returns_one_paragraph_per_named_tool_in_order():
    paragraphs = tool_guidance(builtins_registry(), ("run_python", "read_file"))

    assert len(paragraphs) == 2
    assert paragraphs[0].startswith("Check the [exit code: N] marker")
    assert paragraphs[1].startswith("Use the read_file tool")


def test_tool_guidance_skips_tools_without_a_paragraph():
    paragraphs = tool_guidance(builtins_registry(), ("git_status",))

    assert paragraphs == ()


def test_tool_guidance_refuses_an_unknown_tool():
    with pytest.raises(KeyError):
        tool_guidance(builtins_registry(), ("no_such_tool",))
```

Append to `tests/thymira/test_agents_prompts.py` (reuse the module's existing catalog/spec/task fixtures or helpers; read the file first and follow its naming):

```python
from thymira.agents import PromptEnvironment


def test_build_with_an_environment_prefixes_the_persona_line_and_appends_guidance(
    catalog, spec, task
):
    environment = PromptEnvironment(
        agent_name="coding",
        model="openai/gpt-5.6-luna",
        workspace="C:/runs/run_1/workspace",
        tool_guidance=("Check the [exit code: N] marker on every run_python result.",),
    )

    assembled = PromptBuilder(catalog).build(spec, task, [], environment=environment)

    assert assembled.system.startswith(
        "You are the coding agent of Thymira, powered by the openai/gpt-5.6-luna model. "
        "Your working directory is C:/runs/run_1/workspace."
    )
    assert catalog.system_prompt(spec.name) in assembled.system
    assert assembled.system.endswith("Check the [exit code: N] marker on every run_python result.")


def test_build_without_an_environment_keeps_the_catalog_prompt_only(catalog, spec, task):
    assembled = PromptBuilder(catalog).build(spec, task, [])

    assert assembled.system == catalog.system_prompt(spec.name)


def test_system_prefix_is_stable_across_tasks_for_the_same_environment(catalog, spec, task):
    environment = PromptEnvironment(agent_name="coding", model="m", workspace="w")
    other = task.model_copy(update={"objective": "something else"})

    first = PromptBuilder(catalog).build(spec, task, [], environment=environment)
    second = PromptBuilder(catalog).build(spec, other, [], environment=environment)

    assert first.system == second.system
```

Append to `tests/thymira/test_agents_runner.py` (follow the module's existing helpers for a catalog, a `ScriptedProvider`, a registry with a tool context; the `ScriptedProvider` records every call in `provider.calls` with the messages it received — read `runtime/agents/src/thymira/agents/llm/scripted.py:69-98` for the exact attribute names):

```python
def test_runner_sends_the_persona_line_and_tool_guidance_in_the_system_prompt(tmp_path):
    provider, spec, task, ctx = _wired_context(tmp_path)  # helper in this module, or build inline

    AgentRunner().run(spec, task, ctx)

    system_text = provider.calls[0].system  # adapt to the recorded call's field name
    assert f"You are the {spec.name} agent of Thymira" in system_text
    assert "Your working directory is" in system_text
    assert "Check the [exit code: N] marker" in system_text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_agents_prompts.py tests/thymira/test_agents_runner.py -q -k "guidance or environment or persona"`
Expected: FAIL at import / `TypeError: build() got an unexpected keyword argument 'environment'`.

- [ ] **Step 3: Implement `guidance.py`**

```python
"""Per-tool guidance paragraphs for the system prompt (harness basics 1).

Each paragraph tells the model when to use a tool, what its result looks like and the mistake to
avoid, in the style of a coding harness's system prompt. The registry is consulted so an unknown
name fails loudly, the same contract `build_agent_tools` enforces; a registered tool with no
paragraph simply contributes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.tools.registry import ToolRegistry

GUIDANCE: dict[str, str] = {
    "read_file": (
        "Use the read_file tool — not run_python with open() — to inspect text files. Use "
        "offset and limit to continue reading a large file."
    ),
    "write_file": (
        "Use the write_file tool to create files or completely replace their contents. Existing "
        "files are overwritten, so read an existing file first and prefer edit_file for targeted "
        "changes."
    ),
    "edit_file": (
        "Use the edit_file tool for targeted changes to existing text files. It replaces literal "
        "old_string with new_string; by default old_string must appear exactly once. If it "
        "appears several times, provide a more specific old_string or set replace_all to true."
    ),
    "glob": (
        "Use the glob tool — not run_python with os.walk — to discover files by path pattern. A "
        'pattern with no "/" matches basenames at any depth. Results are files only, never '
        "directories, newest first."
    ),
    "grep": (
        "Use the grep tool — not run_python — to search file contents. Use read_file on a "
        "matched file when you need surrounding context."
    ),
    "run_python": (
        "Check the [exit code: N] marker on every run_python result; investigate a failure "
        "before moving on. Each call runs in a fresh interpreter: write files for anything the "
        "next call needs, and keep printed output short — long output is truncated."
    ),
    "profile_dataset": (
        "Use the profile_dataset tool to understand a registered dataset before modelling; do "
        "not read the raw file with read_file. Registered datasets are listed in the runtime "
        "context."
    ),
    "run_experiment": (
        "Use the run_experiment tool to train a baseline on a registered dataset; report only "
        "the metrics it returns, never a number you have not seen in its output."
    ),
}


def tool_guidance(registry: ToolRegistry, names: Sequence[str]) -> tuple[str, ...]:
    """Return the guidance paragraphs for ``names``, in order, skipping tools without one.

    Raises:
        KeyError: a name the registry does not hold.
    """
    paragraphs: list[str] = []
    for name in names:
        registry.get(name)
        paragraph = GUIDANCE.get(name)
        if paragraph is not None:
            paragraphs.append(paragraph)
    return tuple(paragraphs)


__all__ = ["GUIDANCE", "tool_guidance"]
```

Export `tool_guidance` from `runtime/tools/src/thymira/tools/__init__.py`.

- [ ] **Step 4: Implement `PromptEnvironment` in `prompts.py`**

```python
class PromptEnvironment(ThymiraModel):
    """The Run-stable facts that open the system prompt: who the agent is, on what, where."""

    agent_name: str
    model: str
    workspace: str
    tool_guidance: tuple[str, ...] = ()

    def persona(self) -> str:
        """The one-line identity every request starts with."""
        return (
            f"You are the {self.agent_name} agent of Thymira, powered by the {self.model} "
            f"model. Your working directory is {self.workspace}."
        )
```

and in `PromptBuilder.build`:

```python
    def build(
        self,
        spec: AgentSpec,
        task: Task,
        events: Sequence[Event],
        *,
        environment: PromptEnvironment | None = None,
    ) -> AssembledPrompt:
        system = self._catalog.system_prompt(spec.name)
        if environment is not None:
            sections = [environment.persona(), system, *environment.tool_guidance]
            system = "\n\n".join(section for section in sections if section)
        surface = current_surface(events)
        history = [str(event.payload["text"]) for event in surface if event.payload.get("text")]
        user = "\n".join((*history, task.objective))
        return AssembledPrompt(
            system=system, user=user, surface_seqs=tuple(event.seq for event in surface)
        )
```

Update the module docstring: `system` is now a pure function of `(spec.name, environment)`, and the environment is constant for one agent within one Run, so prefix stability holds per Run. Add `PromptEnvironment` to `__all__` and to `thymira.agents.__init__`.

- [ ] **Step 5: Wire the runner**

In `AgentRunner.run`, before `assembled = …`:

```python
        environment = _environment(spec, task_kind, ctx)
        assembled = PromptBuilder(ctx.catalog).build(
            spec, task, ctx.event_log.events(), environment=environment
        )
```

and add the helper (module level, after `_usage_payload`):

```python
def _environment(spec: AgentSpec, task_kind: str, ctx: AgentContext) -> PromptEnvironment:
    """The Run-stable prompt environment for this spec: model, workspace, tool guidance."""
    choice = choose(spec.role, task_kind, requested_tier=spec.tier)
    workspace = str(ctx.tool_context.workspace) if ctx.tool_context is not None else "(no workspace)"
    guidance = (
        tool_guidance(ctx.tool_registry, spec.tool_allowlist)
        if ctx.tool_registry is not None
        else ()
    )
    return PromptEnvironment(
        agent_name=spec.name, model=choice.model, workspace=workspace, tool_guidance=guidance
    )
```

Import `choose` from `thymira.agents.llm.routing` and `tool_guidance` from `thymira.tools`; check the attribute name of the model id on `ModelChoice` (`routing.py`) and use it. If `choose` needs the provider's model resolution (an environment variable), read how `routed_model` calls it and replicate exactly those arguments so the previewed id equals the recorded `model.selected` one; add an assertion test in `test_agents_runner.py` that the persona's model equals the `model.selected` payload's model for the same run.

- [ ] **Step 6: Run the tests to verify they pass, then the fast lane**

Run: `uv run pytest tests/thymira/test_tools.py tests/thymira/test_agents_prompts.py tests/thymira/test_agents_runner.py -q` then the fast lane. Tests that asserted `assembled.system == catalog prompt` with a wired registry now need the environment; update them to assert the catalog prompt is contained.

- [ ] **Step 7: Lint, type-check, imports, commit**

```bash
git add runtime/tools runtime/agents tests
git commit -m "feat(agents,tools): persona line and per-tool guidance open every system prompt"
```

---

### Task 5: The runtime-context snapshot, a superseding user message

**Files:**
- Create: `runtime/agents/src/thymira/agents/runtime_context.py`
- Modify: `runtime/agents/src/thymira/agents/prompts.py` (fold keeps the latest snapshot), `runtime/agents/src/thymira/agents/runner.py` (append the event before building), `runtime/agents/src/thymira/agents/__init__.py`
- Test: `tests/thymira/test_agents_runtime_context.py` (new), `tests/thymira/test_agents_prompts.py`, `tests/thymira/test_agents_runner.py`

**Interfaces:**
- Produces `RUNTIME_CONTEXT_FORM = "runtime_context"` and `runtime_context_text(*, workspace: Path, artifact_store: ArtifactStore, sandbox_mode: str = "workspace-write") -> str` in `thymira.agents.runtime_context`. The text (exact):

```
Current runtime context. This snapshot supersedes earlier runtime-context snapshots.

Workspace: <workspace>
File sandbox: <sandbox_mode> — code tools may modify files under the workspace only; a blocked file operation is a policy denial, not a bug in the code.
Registered datasets: german_credit (1000 rows, 21 columns: checking_status, duration, …)
Artifacts present: datasets/german_credit.csv (dataset), profiles/german_credit.json (report)
```

  When there are no datasets the line is `Registered datasets: none — a dataset must be registered before profile_dataset or run_experiment can use it.`; when there are no artifacts, `Artifacts present: none`. Datasets are found through `artifact_store.list_active()` entries named `datasets/<name>.schema.json` (load the JSON with `artifact_store.load_json` and validate it as `thymira.tools.datasets.DatasetSchema`); list at most the first 8 columns then `…`. Artifacts are every active artifact except the schema JSON files, as `name (kind)`, sorted by name, at most 40 then `… and N more`.
- `AgentRunner.run` appends, before building the prompt and only when `ctx.tool_context is not None`, one event `EventType.AGENT_MESSAGE`, actor `ctx.actor`, `surface=EventSurface.MODEL_VISIBLE`, `subject_id=task.agent_id`, payload `{"text": <snapshot>, "form": RUNTIME_CONTEXT_FORM, "agent": spec.name, "task_id": task.id}`. Read how `Delegator.delegate` appends its MODEL_VISIBLE `agent.message` (`runtime/agents/src/thymira/agents/delegation.py:132-145`) and use the same keyword for the surface.
- `PromptBuilder.build` folds history so that of all surface events whose `payload.get("form") == RUNTIME_CONTEXT_FORM` only the last one contributes text, in its own position; every other event is unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/thymira/test_agents_runtime_context.py`:

```python
"""The runtime-context snapshot the runner sends before every step (harness basics 1)."""

from __future__ import annotations

from pathlib import Path

from thymira.agents.runtime_context import runtime_context_text
from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.datasets import register_dataset


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts", new_id("run"))


def test_snapshot_lists_registered_datasets_with_shape_and_first_columns(tmp_path: Path):
    store = _store(tmp_path)
    csv = tmp_path / "toy.csv"
    csv.write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
    register_dataset(store, csv, "toy", produced_by=new_id("agent"))

    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=store)

    assert text.startswith(
        "Current runtime context. This snapshot supersedes earlier runtime-context snapshots."
    )
    assert "Registered datasets: toy (2 rows, 3 columns: a, b, c)" in text
    assert "datasets/toy.csv (dataset)" in text
    assert "toy.schema.json" not in text.split("Artifacts present:")[1]


def test_snapshot_says_none_and_how_to_fix_it_when_nothing_is_registered(tmp_path: Path):
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "Registered datasets: none — a dataset must be registered" in text
    assert "Artifacts present: none" in text


def test_snapshot_names_the_sandbox_mode_and_that_a_denial_is_policy(tmp_path: Path):
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "File sandbox: workspace-write" in text
    assert "a policy denial, not a bug in the code" in text
```

Append to `tests/thymira/test_agents_prompts.py`:

```python
from thymira.agents.runtime_context import RUNTIME_CONTEXT_FORM
from thymira.events import InMemoryEventLog
from thymira.schemas import Actor, EventSurface, EventType


def test_build_keeps_only_the_latest_runtime_context_snapshot(catalog, spec, task):
    log = InMemoryEventLog(task.run_id)
    for text in ("snapshot one", "snapshot two"):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": text, "form": RUNTIME_CONTEXT_FORM},
            surface=EventSurface.MODEL_VISIBLE,
        )
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "a real message"},
        surface=EventSurface.MODEL_VISIBLE,
    )

    assembled = PromptBuilder(catalog).build(spec, task, log.events())

    assert "snapshot one" not in assembled.user
    assert assembled.user.index("snapshot two") < assembled.user.index("a real message")
```

Append to `tests/thymira/test_agents_runner.py`:

```python
def test_runner_appends_a_model_visible_runtime_context_before_the_step(tmp_path):
    provider, spec, task, ctx = _wired_context(tmp_path)

    AgentRunner().run(spec, task, ctx)

    snapshots = [
        event
        for event in ctx.event_log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("form") == RUNTIME_CONTEXT_FORM
    ]
    assert len(snapshots) == 1
    assert snapshots[0].surface is EventSurface.MODEL_VISIBLE
    started = next(e for e in ctx.event_log.events() if e.type is EventType.AGENT_STARTED)
    assert snapshots[0].seq < started.seq
    assert "Current runtime context." in provider.calls[0].user  # adapt to the recorded field
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/thymira/test_agents_runtime_context.py tests/thymira/test_agents_prompts.py tests/thymira/test_agents_runner.py -q -k "runtime_context or snapshot"`
Expected: FAIL at import.

- [ ] **Step 3: Implement `runtime_context.py`**

```python
"""The runtime-context snapshot: the dynamic facts a step must see, as one logged message.

The system prompt stays stable (`PromptEnvironment`); everything that changes during a Run —
registered datasets, artifacts, the sandbox mode — travels as a `user`-role message so the
provider's prefix cache survives and the log records exactly what the model was told. A later
snapshot supersedes an earlier one: `PromptBuilder` keeps only the latest in the fold, and the
text says so, following dsh's runtime-context message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.tools.datasets import DatasetSchema

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.state import ArtifactStore

RUNTIME_CONTEXT_FORM = "runtime_context"
MAX_COLUMNS_SHOWN = 8
MAX_ARTIFACTS_SHOWN = 40
_SCHEMA_PREFIX = "datasets/"
_SCHEMA_SUFFIX = ".schema.json"


def runtime_context_text(
    *, workspace: Path, artifact_store: ArtifactStore, sandbox_mode: str = "workspace-write"
) -> str:
    """Render the snapshot for one step from the artifact store's current contents."""
    active = sorted(artifact_store.list_active(), key=lambda artifact: artifact.name)
    datasets: list[str] = []
    artifacts: list[str] = []
    for artifact in active:
        name = artifact.name
        if name.startswith(_SCHEMA_PREFIX) and name.endswith(_SCHEMA_SUFFIX):
            schema = DatasetSchema.model_validate(artifact_store.load_json(name))
            columns = ", ".join(schema.columns[:MAX_COLUMNS_SHOWN])
            if len(schema.columns) > MAX_COLUMNS_SHOWN:
                columns += ", …"
            datasets.append(
                f"{schema.name} ({schema.row_count} rows, {len(schema.columns)} columns: {columns})"
            )
            continue
        artifacts.append(f"{name} ({artifact.kind.value})")
    dataset_line = (
        "Registered datasets: " + "; ".join(datasets)
        if datasets
        else (
            "Registered datasets: none — a dataset must be registered before profile_dataset "
            "or run_experiment can use it."
        )
    )
    if artifacts:
        shown = artifacts[:MAX_ARTIFACTS_SHOWN]
        rest = len(artifacts) - len(shown)
        artifact_line = "Artifacts present: " + ", ".join(shown)
        if rest:
            artifact_line += f" … and {rest} more"
    else:
        artifact_line = "Artifacts present: none"
    return "\n".join(
        (
            "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.",
            "",
            f"Workspace: {workspace}",
            f"File sandbox: {sandbox_mode} — code tools may modify files under the workspace "
            "only; a blocked file operation is a policy denial, not a bug in the code.",
            dataset_line,
            artifact_line,
        )
    )


__all__ = ["RUNTIME_CONTEXT_FORM", "runtime_context_text"]
```

Check `Artifact.kind` is an enum with `.value` (`packages/schemas/src/thymira/schemas/artifact.py`); if it is a plain string, drop `.value`.

- [ ] **Step 4: Fold only the latest snapshot in `PromptBuilder.build`**

Replace the `history` line with:

```python
        surface = current_surface(events)
        latest_snapshot_seq = max(
            (event.seq for event in surface if event.payload.get("form") == RUNTIME_CONTEXT_FORM),
            default=None,
        )
        history = [
            str(event.payload["text"])
            for event in surface
            if event.payload.get("text")
            and (
                event.payload.get("form") != RUNTIME_CONTEXT_FORM
                or event.seq == latest_snapshot_seq
            )
        ]
```

importing `RUNTIME_CONTEXT_FORM` from `thymira.agents.runtime_context` (no cycle: `runtime_context` imports nothing from `prompts`).

- [ ] **Step 5: Append the snapshot in the runner**

In `AgentRunner.run`, before `environment = …`:

```python
        if ctx.tool_context is not None:
            ctx.event_log.append(
                EventType.AGENT_MESSAGE,
                ctx.actor,
                {
                    "text": runtime_context_text(
                        workspace=ctx.tool_context.workspace,
                        artifact_store=ctx.tool_context.artifact_store,
                    ),
                    "form": RUNTIME_CONTEXT_FORM,
                    "agent": spec.name,
                    "task_id": task.id,
                },
                surface=EventSurface.MODEL_VISIBLE,
                subject_id=task.agent_id,
            )
```

(Use the exact `append` keyword the `Delegator` uses for the surface.) Export `runtime_context_text` and `RUNTIME_CONTEXT_FORM` from `thymira.agents`.

- [ ] **Step 6: Run the tests, then the fast lane**

Existing tests that count `agent.message` events or assert the exact `user` text with a wired tool context need updating (one extra MODEL_VISIBLE message per step). Assert on content, not on counts, where possible.

- [ ] **Step 7: Lint, type-check, imports, commit**

```bash
git add runtime/agents tests
git commit -m "feat(agents): a superseding runtime-context snapshot precedes every step"
```

---

### Task 6: The acceptance test: a recorded demo Run with sidecars and a workspace oracle

**Files:**
- Create: `tests/thymira/test_acceptance_demo_run.py`
- Create: `tests/thymira/acceptance/README.md`, `tests/thymira/acceptance/data.system-prompt.expected.md`, `tests/thymira/acceptance/data.tool-schemas.expected.json`, `tests/thymira/acceptance/experiment.system-prompt.expected.md`, `tests/thymira/acceptance/experiment.tool-schemas.expected.json`, `tests/thymira/acceptance/workspace.expected.txt`

**Interfaces:**
- Consumes: `run_thy` (`runtime/thy/src/thymira/thy`), `full_agent_catalog()` (`runtime/thy/src/thymira/thy/agents/__init__.py:78`), the real `data` spec (`data.py:27`: allowlist `("list_files", "read_file")`, output `DataProfile(columns, dtypes, missing, target_candidates)`) and `experiment` spec (`experiment.py:28`: five `mlflow_*` tools + `run_python`, output `ExperimentResult(parameters: dict[str, str], metrics: dict[str, float], seed, model_artifact_id, tracker_run_id: str)`), `builtins_registry()`, `register_dataset`, `ScriptedProvider`, `JsonlEventLog`, `verify_log(path) -> VerificationResult` (`packages/events/src/thymira/events/log.py:135`), `LocalArtifactStore`.
- `ScriptedProvider.calls` today records only `{"prompt", "system"}` and discards `tools` (`scripted.py:67-84`). First change in this task: record them. `self.calls: list[dict[str, Any]]`, and in `complete_turn` replace `del tools` with `self._last_tools = [tool.model_dump(mode="json") for tool in tools]` and append `{"prompt": prompt, "system": system, "tools": self._last_tools}` in `_next` (pass the list through, defaulting to `[]` for `complete`/`complete_structured`). Existing tests index `calls[i]["prompt"]`/`["system"]` and keep working.
- Two spec changes, in scope because they are what the tools are for: `DATA_AGENT_SPEC.tool_allowlist = ("glob", "read_file", "profile_dataset")` and `EXPERIMENT_AGENT_SPEC.tool_allowlist` gains `"run_experiment"`. Update `prompts/data.md` to say: "Use glob to find files and read_file for text files you need to see. Use profile_dataset on the registered dataset named in the runtime context; never read a dataset's raw rows with read_file." Update `prompts/experiment.md` to name `run_experiment` as the way to train the baseline. Update `tests/thymira/test_thy_agent_data.py` / `test_thy_agent_experiment.py` assertions on the allowlists.
- The dataset: `data/german_credit.csv`, 1000 rows, 21 columns, header `checking_status, duration_months, credit_history, purpose, credit_amount, savings_status, employment_since, installment_rate_pct, personal_status_sex, other_debtors, residence_since, property_magnitude, age, other_installment_plans, housing, existing_credits, job, num_dependents, own_telephone, foreign_worker, is_high_risk`; the target is `is_high_risk`.
- Produces: the sidecars, normalized so they are stable: replace the absolute workspace path with `{{workspace}}` (both `str(workspace)` and `workspace.as_posix()`), every `run_…`/`agent_…`/`artifact_…`/`tool_…`/`task_…`/`session_…`/`project_…` id with `{{id}}`, and the model id with `{{model}}`. Set `THYMIRA_SNAPSHOT=record` to (re)write the sidecars; otherwise they are compared byte for byte and a diff is shown on failure. The scripted `final_result` for the data agent is `{"columns": [<the 21 names above>], "dtypes": {"duration_months": "Int64", "credit_amount": "Int64", "age": "Int64", "is_high_risk": "Int64"}, "missing": {}, "target_candidates": ["is_high_risk"]}`; for the experiment agent, `{"parameters": {"seed": "7", "model": "logistic_regression"}, "metrics": <copy the metrics dict printed by run_experiment's stdout in the recorded run, e.g. {"accuracy": …}>, "seed": 7, "model_artifact_id": None, "tracker_run_id": "run_" + "a" * 32}`. The first call per agent is identified by the persona line: `call["system"].startswith("You are the data agent")` / `"You are the experiment agent"`.

- [ ] **Step 1: Write the test skeleton (it fails until the sidecars exist)**

```python
"""Acceptance: the demo Run, recorded (harness basics 1).

Real: ThyGraph, Delegator/AgentRunner, ToolManager + Gate, the builtin tools with the local
Python sandbox, a LocalArtifactStore and a JsonlEventLog under tmp_path, the checked-in
examples/credit-risk project and data/german_credit.csv. Faked: only the model (ScriptedProvider).

What is pinned: (1) the exact system prompt and tool schemas sent to the model on the first
request of each agent (sidecars under tests/thymira/acceptance/); (2) the artifacts the Run
leaves behind (workspace.expected.txt), which is the independent oracle — model prose and tool
text do not prove the external effect; (3) the log verifies. Set THYMIRA_SNAPSHOT=record to
refresh the sidecars after an intentional prompt or tool change, and review the diff.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.events import JsonlEventLog, verify_log
from thymira.policies import ActionRule, Decision, Gate, Policy, PolicyEngine, RiskProfile, auto_approve, load_policy_stack
from thymira.schemas import Run, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import AgentTask, PlanOutput, SynthesisNarrative, ThyAgentKind, ThyInput, ThyPhase, run_thy
from thymira.thy.agents import full_agent_catalog
from thymira.tools import ToolContext
from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.datasets import register_dataset

pytestmark = pytest.mark.integration

ACCEPTANCE_DIR = Path(__file__).resolve().parent / "acceptance"
REPO = Path(__file__).resolve().parents[2]
RECORD = os.environ.get("THYMIRA_SNAPSHOT") == "record"

_PLAN_PASSES = Policy(
    name="plan-passes",
    version="1.0",
    action_rules=(
        ActionRule(
            id="TEST-PLAN",
            action_types=("plan.proposed",),
            decision=Decision.PASS,
            reason="acceptance: THY's plan needs no review",
        ),
    ),
)


def _normalize(text: str, workspace: Path, model: str) -> str:
    text = text.replace(str(workspace), "{{workspace}}").replace(workspace.as_posix(), "{{workspace}}")
    text = re.sub(r"\b(run|agent|artifact|tool|task|session|project)_[0-9a-f]{32}\b", "{{id}}", text)
    return text.replace(model, "{{model}}")


def _expect(name: str, actual: str) -> None:
    path = ACCEPTANCE_DIR / name
    if RECORD:
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    assert path.read_text(encoding="utf-8") == actual, f"{name} drifted; run with THYMIRA_SNAPSHOT=record and review the diff"


def test_demo_run_records_what_the_model_saw_and_what_it_left_behind(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(REPO / "examples" / "credit-risk", workspace)
    run = Run(id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"),
              prompt="Assess credit-risk applicants and recommend a model.")
    log = JsonlEventLog(tmp_path / "events.jsonl", run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    register_dataset(store, REPO / "data" / "german_credit.csv", "german_credit", produced_by=new_id("agent"))
    gate = Gate(PolicyEngine(load_policy_stack().merged_with(_PLAN_PASSES)), log, approver=auto_approve)
    registry = builtins_registry()
    tool_context = ToolContext(run_id=run.id, agent_id=new_id("agent"), workspace=workspace, event_log=log,
                               gate=gate, artifact_store=store,
                               risk_profile=RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0))
    catalog = full_agent_catalog()
    columns = [
        "checking_status", "duration_months", "credit_history", "purpose", "credit_amount",
        "savings_status", "employment_since", "installment_rate_pct", "personal_status_sex",
        "other_debtors", "residence_since", "property_magnitude", "age", "other_installment_plans",
        "housing", "existing_credits", "job", "num_dependents", "own_telephone", "foreign_worker",
        "is_high_risk",
    ]
    provider = ScriptedProvider([
        PlanOutput(tasks=(
            AgentTask(id="profile", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE,
                      instruction="Profile the german_credit dataset."),
            AgentTask(id="train", agent=ThyAgentKind.EXPERIMENT, phase=ThyPhase.EXECUTE,
                      instruction="Train a logistic-regression baseline on german_credit predicting is_high_risk."),
        )),
        # data agent
        LLMToolCall(id="d1", name="profile_dataset", arguments={"dataset": "german_credit"}),
        LLMToolCall(id="d2", name="final_result", arguments={
            "columns": columns,
            "dtypes": {"duration_months": "Int64", "credit_amount": "Int64", "age": "Int64", "is_high_risk": "Int64"},
            "missing": {},
            "target_candidates": ["is_high_risk"],
        }),
        # experiment agent
        LLMToolCall(id="e1", name="run_experiment", arguments={
            "dataset": "german_credit", "target_column": "is_high_risk",
            "description": "Train the logistic-regression baseline",
        }),
        LLMToolCall(id="e2", name="final_result", arguments={
            "parameters": {"seed": "7", "model": "logistic_regression"},
            "metrics": {"accuracy": 0.0},  # replace with the value run_experiment printed in the recorded run
            "seed": 7, "model_artifact_id": None, "tracker_run_id": "run_" + "a" * 32,
        }),
        SynthesisNarrative(tradeoffs="A linear baseline is interpretable.", limitations="Single split."),
    ])

    output = run_thy(ThyInput(run=run), catalog, log, provider=provider, project_dir=workspace,
                     gate=gate, artifact_store=store, tool_registry=registry, tool_context=tool_context)

    assert output.error is None
    assert output.recommendation is not None
    assert store.verify() == []
    assert verify_log(tmp_path / "events.jsonl").valid  # read packages/events for the exact signature

    selected = next(e for e in log.events() if e.type is EventType.MODEL_SELECTED)
    model = str(selected.payload["model"])  # check the payload key in routing.ModelChoice.event_payload()
    for agent_name in ("data", "experiment"):
        call = next(c for c in provider.calls if c["system"].startswith(f"You are the {agent_name} agent"))
        _expect(f"{agent_name}.system-prompt.expected.md", _normalize(call["system"], workspace, model) + "\n")
        _expect(f"{agent_name}.tool-schemas.expected.json",
                json.dumps(call["tools"], indent=1, sort_keys=True) + "\n")
    listing = "\n".join(f"{a.name} ({a.kind.value})" for a in sorted(store.list_active(), key=lambda a: a.name)) + "\n"
    _expect("workspace.expected.txt", _normalize(listing, workspace, model))
```

Replace every `{...}` and `<…>` above with the real values after reading the referenced modules; the `final_result` arguments must validate against the agent's output schema (`data.py` / `experiment.py`), and the data agent's allowlist must include `profile_dataset` (if the real `data` spec only lists `list_files, read_file`, add `glob` and `profile_dataset` to `DATA_AGENT_SPEC.tool_allowlist` in `runtime/thy/src/thymira/thy/agents/data.py` — that is in scope, it is what the data agent is for — and update `prompts/data.md` to say "Use profile_dataset on the registered dataset; use glob and read_file only for other files").

- [ ] **Step 2: Record the sidecars**

Run: `THYMIRA_SNAPSHOT=record uv run pytest tests/thymira/test_acceptance_demo_run.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider` (PowerShell: `$env:THYMIRA_SNAPSHOT='record'; uv run pytest …`).
Expected: PASS and five files under `tests/thymira/acceptance/`. Open each: the system prompt must start with the persona line, contain the catalog prompt and the guidance paragraphs; the tool schemas must list `description` as required for the mutating tools; the workspace listing must contain the dataset, its profile report, the model and metrics artifacts and `analysis.md`.

- [ ] **Step 3: Run the test in compare mode**

Run: `uv run pytest tests/thymira/test_acceptance_demo_run.py -q --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider`
Expected: PASS (byte-identical sidecars). Then change one word in `GUIDANCE["run_python"]`, run again, confirm it FAILS with the drift message, and revert.

- [ ] **Step 4: Write `tests/thymira/acceptance/README.md`**

```markdown
# Recorded acceptance Run

`test_acceptance_demo_run.py` drives the real ThyGraph with the builtin tools and a scripted
model over `examples/credit-risk`. These files are what it pins:

- `<agent>.system-prompt.expected.md` — the exact system prompt sent on that agent's first request
  (workspace path, ids and model id normalized to `{{workspace}}`, `{{id}}`, `{{model}}`).
- `<agent>.tool-schemas.expected.json` — the tool definitions sent with it.
- `workspace.expected.txt` — the artifacts the Run leaves behind: the independent oracle. Model
  prose and tool text do not prove the external effect; this listing does.

Refresh after an intentional change with `THYMIRA_SNAPSHOT=record`, and review the diff in the
pull request like any other code change.
```

- [ ] **Step 5: Commit**

```bash
git add tests/thymira/test_acceptance_demo_run.py tests/thymira/acceptance runtime/thy
git commit -m "test(acceptance): record the demo Run — prompts, tool schemas and the workspace it leaves"
```

---

### Task 7: Changelog, map and member README

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]`), `AGENTS.md` (repository map, `runtime/tools` line), `runtime/tools/README.md` (tool list)

- [ ] **Step 1: Write the entries**

`CHANGELOG.md` under `[Unreleased]`:

```markdown
### Added
- Harness basics 1 (ADR-0013): every sub-agent's system prompt opens with a persona line
  (agent, model, working directory) and per-tool guidance; a runtime-context snapshot
  (workspace, sandbox mode, registered datasets, artifacts) precedes every step as a
  superseding, logged, model-visible message; `edit_file`, `glob` and `grep` tools; `read_file`
  paging; a mandatory 5-10 word `description` on `run_python`, `write_file`, `edit_file` and
  `run_experiment`; tool results rendered with `[exit code: N]` last; and a recorded acceptance
  Run whose prompts, tool schemas and resulting artifacts are committed sidecars.
```

`AGENTS.md`: in the `runtime/tools` map line, replace "`run_python`/file/git/MLflow/dataset tools" with "`run_python`, `read_file`/`write_file`/`edit_file`/`glob`/`grep`, git, MLflow and dataset tools, and the per-tool prompt guidance (`tool_guidance`)".

`runtime/tools/README.md`: add the three tools to its tool table with one line each, using the descriptions from Task 3.

- [ ] **Step 2: Full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run python scripts/lint_imports.py && uv run python scripts/check_roadmap.py && uv run pytest -q -m "not slow" --basetemp=C:/Users/lucas/AppData/Local/Temp/thy-bt1 -p no:cacheprovider`
Expected: all clean; the fast lane passes with the new tests added.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md AGENTS.md runtime/tools/README.md
git commit -m "docs: changelog, map and tools README for harness basics 1"
```

---

## Out of scope (next PR)

Failure as data across the Run (`turn/end` with a reason, the three failure classes), the real gate (`allowed-once | rejected | cancelled | unavailable`, `never` before answerers, `interrupt()`, same-call escalation), dataset registration inside the Inspect node from `.thymira/config.yaml` (this PR's acceptance test registers it in setup), the verbatim log with export-time redaction (decision 1), the fail-closed sandbox (decision 2), the log format version (decision 6), `register_dataset` refusing the shipped `data/german_credit.csv` (two ragged rows), and `run_experiment`'s default training code being unable to handle categorical columns.
