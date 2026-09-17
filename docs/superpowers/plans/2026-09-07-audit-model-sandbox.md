# ADR-0013 sandbox block: isolate model audit execution

1. Reproduce `AuditModel` executing a harmless crafted model in the host through the real
   Tool Manager. Keep the regression red before changing the execution path.
2. Add a private, versioned request/prediction protocol and a sandbox worker for model loading,
   attribute access and predictions. Preserve feature order and numeric-label behavior.
3. Keep metric, subgroup, calibration and drift calculations in the host, accepting only a
   bounded prediction envelope validated against the staged dataset.
4. Wire runtime-owned modes, capability metadata, registry injection and observed enforcement.
   Refuse read-only staging and unsupported default local execution without host fallback.
5. Preserve numerical regression tests in the proper integration lane; add refusal, invalid
   response, approval/mode and real-container evidence checks. CompareModels and MIRA remain
   outside the implementation scope because neither deserializes models.
6. Review with Sol and Terra, build the current worker into the image, run all checks and full
   coverage (base 91.46%), then commit locally. Publication requires explicit user instruction.

Every concurrent pytest invocation must use its own `--basetemp` under the system temporary
directory. Freeze production files before the final gate and coverage run.

The remaining sandbox slices cover effective quotas, all execution boundaries, Git image support
and runtime-owned production configuration. Later work remains in the agreed order: record
fidelity and credential exclusion, reasoning token/digest evidence without content, then durable
task/dependency/recovery coordination.
