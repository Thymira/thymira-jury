# ADR-0013 sandbox block: bound host subprocess output

1. Reproduce unbounded capture through the real local and container backends before changing
   the capture path. Both nine-MiB runs currently succeed and return the entire output.
2. Implement one bounded process-capture helper for stdout and stderr, process termination,
   timeout handling and bounded cleanup. Test exact limits, combined streams and error paths.
3. Wire it into local execution and every Docker lifecycle operation. Preserve interpreter
   selection, clean environments, actual sandbox evidence and forced container cleanup.
4. Disable Docker's separate log storage; verify attached output still reaches the runtime.
5. Verify configured/default limits, ordinary output, failure evidence through Tool Manager,
   real container cleanup, and below-limit large-output spilling.
6. Freeze source, review, run `just check` and the full suite with coverage (base 91.50%),
   then commit locally. Publish only on explicit instruction.

Concurrent pytest runs use different temporary directories. Git portability and worktree
regressions are retained in the separate `sandbox-git-boundary` worktree for the next slice.
Remaining sandbox work includes effective workspace quotas and production configuration.
The broader agreed sequence then continues with record fidelity and credential exclusion,
reasoning token/digest evidence without content, and durable task/dependency/recovery state.
