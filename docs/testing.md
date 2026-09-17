# Testing recorded model sessions

Acceptance sessions are committed as JSONL fixtures beside their prompt and tool-schema
sidecars. The fixture contains the model turns only; the test runs the real Thymira graph and
checks the persisted event log, artifacts and workspace manifest independently of model prose.

`replay` is the default and is read-only. Set `THYMIRA_SNAPSHOT=refresh` only after an intentional
prompt, schema, dataset or training change, then review every fixture diff. `record` may create a
missing fixture but refuses to overwrite an existing one. Volatile run identifiers, caches, MLflow
state and generated scripts are excluded or normalized so two clean replays remain comparable.

The test must use `ScriptedProvider`; acceptance tests never call a paid model or require a key.
An artifact or workspace result is evidence only when it is persisted and digest-verified; text
returned by the model cannot stand in for an external effect.
