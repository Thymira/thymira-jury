# thymira-tools

Tool Manager and Permission Policy in front of every tool: run_python, read/write/edit/list files, glob, grep, git_*, MLflow (start_run/log_param/log_metric/log_artifact/end_run); MCP is the exposure standard.

| | |
|---|---|
| Import name | `thymira.tools` |
| Owner (MVP roadmap) | P3 (Tools / Execution / MLflow) |
| MVP priority | P0 |
| Location | `runtime/tools/` |

## Status

Implemented and tested (`tests/thymira/test_tools.py`): the `Tool` protocol and
`ToolContext`/`ToolResult` contracts, the `ToolRegistry`, and the `ToolManager` — which validates
arguments, then authorises every call through the `Gate` (`check_capability` + `allows_execution`,
fail-closed), records the `tool.started` / `tool.denied` / `tool.completed` lifecycle with the
validated model-visible arguments, and spills large outputs. API/export surfaces redact copies. A
`REQUIRE_HUMAN_REVIEW` tool call waits for a human's
one-shot approval of its exact validated arguments; a rejected or automatic answer never
authorizes execution. The built-in tools (`run_python`, `read_file`/`write_file`/`edit_file`/
`list_files`, `glob`, `grep`, git and worktree tools, MLflow tracker tools, dataset/SQL/statistics/
profile tools, `run_experiment`, `audit_model`/`compare_models`/`inspect_model`), the
`LocalSubprocessSandbox`/`ContainerSandbox` and the MCP server (`tools/mcp/server.py`) are
implemented. `thymira.tools.guidance.tool_guidance` (harness basics 1, ADR-0013) adds per-tool
prompt guidance for the sub-agent's system prompt.

`query_sql` loads exactly one registered Dataset Artifact into the in-memory table `dataset`.
Before DuckDB executes model-authored SQL, a fail-closed relation guard permits only that table and
CTEs or subqueries derived from it; table functions, filename replacement scans, unknown or
qualified relations are refused. DuckDB external access is independently disabled as a second
barrier.

The local backend cannot enforce filesystem confinement. It refuses `read_only` and
`workspace_write` before spawning, returning exit 125 and `unusable` enforcement. Only explicit
`danger_full_access` can launch a local child; it reports `partial` enforcement and declares
host filesystem/network effects. Subprocess tools expose a runtime-owned `mode`, and
`builtins_registry(subprocess_mode=...)` can override it at construction. Models cannot set this
mode in tool arguments. Selecting a mode never authorizes execution: the default policy blocks
external effects, and inherited restrictions remain enforced. Default API and worker registries
request confinement, so their local subprocess calls now fail closed (ADR-0013).

Approval requests, intent digests, start events and denials bind to the requested mode as well
as the validated arguments. `ToolCall.sandbox_mode` retains that request; `tool.completed`
separately records the backend's reported mode and the requested mode. Reconfiguring a tool
cannot broaden an existing human approval. MIRA independently checks the recorded identity and
execution mode.

`ContainerSandbox` executes `run_python`, `run_experiment`, `inspect_model` and `audit_model`
using the locally built `thymira:dev` image (`just docker-build`). It clears the CLI entrypoint,
disables implicit pulling, uses one helper/worker identity (non-root on native POSIX; uid 0 inside
the private volume on Docker Desktop) and mounts the workspace at `/workspace`. Python
commands and embedded input/output paths are workspace-relative; the image supplies the locked
training and inspection dependencies. These tools refuse `read_only` before staging files.

Docker creation, attached execution, inspection and forced cleanup are distinct operations.
Only an inspected child exit is a completed execution; missing Docker/image, failed startup,
timeouts and cleanup failures return exit 125 with `unusable` enforcement. Cleanup has a separate
five-second budget so an expired execution deadline still permits forced removal. The ordinary
bind-mounted container path reports `partial`, including ordinary child failures. A non-networked
quota-backed execution reports `full` only after Docker's worker profile, private tmpfs volume,
mount identity, bounded publication, and cleanup have all been verified. The former
`filesystem_limit` option is removed: a writable-layer limit does not quota the bind-mounted
workspace.

Both backends bound combined stdout/stderr capture to eight MiB per subprocess invocation,
before Tool Manager spilling. Runtime code can configure a positive `output_limit_bytes` on
the backend. An overrun stops execution and returns exit 125 with a named output-limit failure;
it never becomes a successful truncated result. Docker reports `unusable` and removes its
container; explicit local development execution retains `partial`. Docker lifecycle and cleanup
output are also bounded, and daemon log storage is disabled. This is an output budget, not a
workspace filesystem quota or a claim of full confinement.
Capture startup or I/O errors retain the requested mode and report `unusable` instead of
discarding the sandbox evidence.

The real Docker tests cover non-root execution, workspace writes, read-only preservation, blocked
network access, memory limits and timeout cleanup, plus training and inspection through the real
Tool Manager and audit. They skip absent prerequisites, but fail execution regressions when
Docker and the image exist. Verified full confinement across all execution surfaces and
runtime-owned production backend configuration remain follow-up work. The API and
worker registries still use safe local refusal by default.

`audit_model` treats model deserialization, attribute access and prediction as code execution.
Its worker receives a normalized Parquet dataset and model bytes under fresh workspace paths;
the host receives only a versioned, size-limited prediction envelope. After checking its rows,
features, labels and finite probabilities, the host computes overall, subgroup, calibration and
drift metrics. All subgroup metrics use the same prediction vector as the overall score.
Backend and post-execution failures retain sandbox evidence and produce no successful audit
report. `CompareModels` continues ranking tracker JSON metrics, and MIRA's deterministic controls
continue reading evidence without loading models. Rebuild the image after adding the worker.

`thymira.tools.refusals` exposes the pure refusal vocabulary owned by `thymira.schemas`.
Constraint denials record the tool and agent identities beside the reason. Budget denials name
the guard's decision separately from the call's capability decision; a guard returning a plain
string or no decision id records `decision_id: null`.

The built-in/default `run_experiment` path records A23 evidence on `model.trained`: the seed
actually used by the built-in classifier, the actual installed library versions observed by the
child process, the effective classifier estimator arguments and split configuration, the observed
thread-pool facts covering fit and predict, and `training_script_sha256` for the generated script
bytes. When explicitly permitted to execute, the baseline observes a non-empty `num_threads: 1`
pool and passes A23. Valid
parallel or empty/unavailable observations remain recorded so MIRA can fail A23; malformed or
missing fresh sidecars fail the tool. This evidence is emitted only for the built-in/default path.
Custom code retains the A23 missing-metadata finding; it does not inherit the built-in baseline
evidence.

The run-wide `ExecutionConstraints.requires_human_review` flag requires a separate approval for
every tool call, including capabilities that would otherwise pass. The Policy Engine carries
inherited restrictions into the recorded capability decision before the Gate requests review.
Approval to start THY never supplies a tool ticket or clears the flag. Other constraints remain
hard refusals, and an approved ticket cannot bypass newly inherited restrictions. This allows
the shipped credit-risk CR-001 policy to progress through explicit per-call approvals.

## Tools

Git status, diff, log and commit can use an injected `ContainerSandbox` for standalone workspace
repositories with a real contained `.git` directory. Confined calls reject gitfiles, linked
repositories and symlink/reparse-point Git directories before execution. The image includes Git;
commands trust exactly `/workspace` for its ownership check, ignore system configuration and
disable terminal prompts. Read-only calls disable optional index writes. Repository-local hooks,
filters and signing configuration can execute inside the sandbox, so every Git tool declares
`code_execution`; `--no-ext-diff` does not disable text conversion. Results retain actual
`PARTIAL`/`UNUSABLE` evidence and the A19 finding. Default local confined execution still refuses;
runtime-owned production backend selection and effective workspace quotas remain pending.

Container commits require repository-local `user.name` and `user.email`; host identity and
credentials are not imported. Explicit `paths` commit only those paths, preserving unrelated
staging. Paths are literal file/directory names; Git magic and wildcard expansion are disabled.
Omitting paths retains whole-workspace staging and commit behavior. A failed commit
can leave its selected paths staged; a later explicit-path commit does not include them unless
selected. Git/worktree construction rejects invalid sandbox modes. Preflight refusals and
backend exceptions report `UNUSABLE` without claiming an actual execution mode; the capability,
ToolCall and requested-mode event field still retain the request.

Worktree create/list/remove refuse `READ_ONLY` and `WORKSPACE_WRITE` before host mutation.
Portable container worktrees are not supported, including container development mode; no external
shared Git directory is mounted as a workaround. Explicit local `DANGER_FULL_ACCESS` retains
worktrees below `.thymira/worktrees/`, with relative command paths and validated names/refs.
Directory markers, trailing-dot aliases, option-like refs and NUL arguments are rejected.
Supporting confined worktrees requires a later design for their Git metadata and path semantics.

`profile_dataset` saves a REPORT artifact with `profile_version: 1` and a `source` declaration:
`artifact`, `sha256`, `schema_sha256`, `max_rows`, and `columns` (ordered selection or null).
It verifies that the registered dataset and captured schema are active and unchanged before
describing that source. The report retains its shape, missingness, cardinality, frequencies,
histograms and correlations. MIRA independently checks those facts against the declared source;
the tool's success or its own numbers never establish audit correctness.

Three tools added for harness basics 1 (ADR-0013); descriptions are the exact model-facing text
each tool declares (`runtime/tools/src/thymira/tools/builtins/{files,search}.py`).

| Tool | Description |
|---|---|
| `edit_file` | Edit an existing UTF-8 text file inside the workspace by replacing literal text. old_string must match exactly and, unless replace_all is true, appear exactly once; the error tells you when it does not. Read the file first unless you just created or edited it. |
| `glob` | Find files in the workspace whose paths match a glob pattern. Returns file paths only, never directories, in stable path order; a pattern with no "/" matches the basename at any depth. At most 100 paths come back; a larger result says how many were omitted. |
| `grep` | Search file contents in the workspace with a Python regular expression. Returns matching lines as "path:line: text", grouped by file, at most 250 matches; a capped result says so. Use read_file on a matched file for surrounding context. |

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
