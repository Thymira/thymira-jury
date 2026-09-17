# Snapshot fixtures

Snapshot fixtures are reviewed evidence, not disposable test output. Keep model-facing prompts and
tool schemas in readable sidecars, keep scripted turns in a committed JSONL session, and keep an
independent workspace/artifact oracle. Replay is read-only; fixture updates require an explicit
`THYMIRA_SNAPSHOT=refresh` and a reviewed diff. Do not commit credentials, raw chain-of-thought or
volatile run identifiers.
