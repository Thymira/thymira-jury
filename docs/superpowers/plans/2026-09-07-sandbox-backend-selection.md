# F6.7/F6.2 vertical slice: runtime-owned sandbox backend selection and resolved spec evidence

1. Add `SandboxSettings.from_env()`/`build_sandbox()` and `configured_builtins_registry()` so a
   composition root selects `ContainerSandbox` vs `LocalSubprocessSandbox` from
   `THYMIRA_SANDBOX_*` environment variables, defaulting to `container`; validate argv-bound
   values (image, memory, cpus, pids limit) so an environment variable can never inject a docker
   flag.
2. Add `ResolvedExecutionSpec` and `scrub_environment()`: both backends record what they actually
   resolved (image, mount, network, memory, cpus, pids limit, forwarded/excluded environment
   names) and scrub credential-shaped and interpreter-bootstrap names from a caller-supplied
   environment before either backend uses it; the local backend applies its own `PATH` last.
3. Fix `run_container_lifecycle`'s cleanup `finally` block, which used to overwrite an
   already-confirmed successful outcome when `docker rm` failed; `cleanup_confirmed` is now a
   separate fact and the observed `exit_code`/`child_confirmed` survive a cleanup failure.
4. Thread the two new facts through `ToolResult` → `recordable_result` → the `tool.completed`
   payload, and route all nine `ToolResult` construction sites across the sandbox-backed tools
   through one `with_sandbox_evidence()` helper.
5. Add MIRA control A29 to independently recompute, from replayed `tool.completed` events alone,
   that a `PARTIAL`/`FULL` execution carries a spec, that a confined mode's network/mount are
   consistent, and that no forwarded name is credential-shaped; a failed cleanup is a separate
   MEDIUM finding. MIRA keeps its own credential-fragment literal (F11.3).
6. Wire `apps/api/src/thymira/api/deps.py` and `runtime/core/src/thymira/core/worker.py` to
   `configured_builtins_registry()`; prove it end-to-end against real Docker through Tool
   Manager for `run_python` and `git_status`, plus the read-only mount, pids-limit and
   memory-limit boundaries, each independently observed by the confined child, the replayed
   `events.jsonl`, and MIRA's A29.
7. Refuse a bootstrap-name (`PATH`, `LD_PRELOAD`, `*_PROXY`, ...) from a project `.env` at both
   entry-point loaders — the API raises, the CLI (which spawns no child) skips — and pin the two
   blocklists equal with a mechanical test.
8. Pin the acceptance demo to `THYMIRA_SANDBOX_BACKEND=local` explicitly (the only test in the
   suite that executes real code through the production composition root), review, run `just
   check`, then the full suite. Commit this slice locally; publish only on explicit instruction.

The [DSH acceptance record](2026-09-07-dsh-acceptance.md) governs foundation completion.
This slice does not close A19 (both backends still report `PARTIAL`, never `FULL`), does not
prove effective bind-workspace quotas (Docker applies none to a bind mount; `workspace_quota`
stays declared `unenforced`), does not give container timeout evidence a distinct fact from
`UNUSABLE`/125, does not exercise `run_experiment`/`inspect_model`/`audit_model` against the
container backend, and does not reach the packaged compose deployment (its containers have no
Docker socket). F6.1/F6.5/F6.7 stay open on those points.
