# ADR-0013 sandbox block: local refusal and exact mode authorization

1. Reproduce writes outside the workspace through the local backend under both restricted modes.
2. Refuse those modes before starting the child; retain explicit development execution and
   truthful requested-mode/enforcement evidence.
3. Propagate runtime-owned modes through Python, experiment, inspection and Git tools. Declare
   unconfined capabilities; never add a model-controlled mode or bypass the existing policy.
4. Bind exact human approval to the requested mode and verify it independently in MIRA,
   including restart/reconfiguration and contradictory completion evidence.
5. Migrate real-training tests to explicit development fixtures, and pin the production-default
   composed refusal without inventing successful model artifacts or relaxing governance.
6. Review with Sol and Claude Code Opus, run all checks and full coverage (base 90.77%), and commit
   the bounded slice locally. Push and PR creation require the user's explicit instruction.

## Remaining sandbox slices

- Make a confined backend usable with container-native interpreters, paths and dependencies;
  prove workspace write boundaries, read-only behavior and unavailable-backend refusal.
- Verify confinement claims, process/resource boundaries and coherent permission metadata across
  all execution surfaces before resolving A19. Do not infer `full` from command-line flags alone.
- Expose production configuration only through runtime-owned composition, preserving exact
  approval identity, and exercise the composed Run under the actual backend.

Then follow the agreed order: record fidelity and credential exclusion, reasoning token/digest
evidence without persisted content, and durable task/dependency/recovery coordination. Only a
defect blocking the active block's implementation or verification interrupts that order.
