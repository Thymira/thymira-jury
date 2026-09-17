# Built-in experiment reproducibility evidence

1. Reproduce A23's missing producer metadata and its acceptance of invalid thread declarations.
2. Capture seed, installed versions, effective estimator arguments and split configuration in
   the default training subprocess; constrain numerical workers and record observed counts.
3. Validate the invocation-specific evidence before recording `model.trained`; preserve
   well-formed parallel-pool observations for A23 to reject. Fingerprint the actual generated
   script; keep arbitrary custom code outside the default provenance claim.
4. Validate A23 metadata values and contradictions, preserving valid historical aliases.
5. Replay the composed demo and refresh only outcomes produced by actual execution. Retain
   custom-code warnings and the unchanged Policy Engine's human approval boundary.
6. Review independently, run fast and full tests with coverage, lint, types, import contracts,
   notes and packaging gates; commit the bounded change locally.

## Follow-up order

After closing A23, begin the ADR-0013 sandbox block: effective confinement, refusal when it
cannot be applied, and coherent permissions, addressing A19. Split it into small behavior-tested
PRs. Next come record fidelity (what the model saw, credential exclusion, export redaction,
log versioning), reasoning evidence through token counts/digests without persisted content,
and durable task coordination, dependencies and recovery. Only defects blocking the active
block's implementation or verification interrupt this sequence; record other findings as
pending work. Existing review, local commit, push and merge authorization rules still apply.

Owners: tools produces execution facts; MIRA checks recorded metadata; Core remains the sole
composition and transition owner. The regression boundary uses real tools/training/storage and
scripted LLM responses. No live model call is required because no LLM gateway behavior changes.
