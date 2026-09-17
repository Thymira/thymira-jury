# Recorded acceptance Run

`test_acceptance_demo_run.py` drives the production composition for a complete Run:
`RunService` dispatches through `InlineDispatcher` and the real Core graph, whose route includes
`RiskInterviewService` intake and risk classification, THY's builtin tools, deterministic MIRA
controls, and the final Gate. `ScriptedProvider` supplies the model turns, including the
classification response; the intake facts describe the actual credit-risk activity and its
affected people and sensitive attributes. The human answers are authenticated and recorded as
non-automatic before the parked Run is resumed. The controlled MIRA audit-agent roster is
explicitly empty (`specs=()`), so this acceptance boundary exercises deterministic controls while
the optional regulatory LLM fan-out remains covered by its dedicated tests.

`demo.session.jsonl` is the committed sequence of model turns; these files are what it pins:

- `<agent>.system-prompt.expected.md` — the exact system prompt sent on that agent's first request
  (workspace path, ids and model id normalized to `{{workspace}}`, `{{id}}`, `{{model}}`).
- `<agent>.tool-schemas.expected.json` — the tool definitions sent with it.
- `workspace.expected.json` — a SHA-256 manifest of the files the Run leaves behind: the
  independent oracle. Model prose and tool text do not prove the external effect; this manifest
  does. The input dataset keeps its exact digest; generated artifacts use a runtime-verified
  digest marker because model serialization and numerical report bytes can differ by platform.
  Volatile prompt-log artifacts (covered by the prompt sidecars), MLflow state, Python caches and
  generated tool scripts are excluded. `store.verify()` still checks every artifact's actual
  SHA-256 digest.

`metrics.expected.json` remains the numerical oracle for the explicit development-mode
German-credit training regression in `test_tools_experiment_repro.py`. That real training test
compares accuracy within 0.02 because logistic-regression fits can differ across BLAS builds.
The default production Run no longer trains with an unconfined local subprocess, so it does not
refresh or claim these metrics. An intentional numerical-baseline change needs a reviewed update
to this oracle.

Replay is the default and never writes fixtures. After an intentional change, use
`THYMIRA_SNAPSHOT=refresh` and review the diff in the pull request like any other code change.

The complete graph test is marked `slow` because it performs the real Core, intake, approval,
THY, MIRA, and Gate sequence. It keeps all writable state under `tmp_path`. The prompt and
tool-schema sidecars pin what the model saw; the independent workspace oracle verifies the
dataset, schema and profile actually produced. The rejected experiment creates no model,
metrics or analysis artifact. Replay is read-only and never refreshes fixtures.

The recorded outcome is `BLOCKED`. A3/A6/A15 pass: tools have matching human approvals and
risk classification precedes execution. A18 independently verifies
`profile/german_credit.json` against its registered dataset and captured schema and records
their digests. The local backend refuses `workspace_write` before launching the training child,
records exit 125 and `unusable` enforcement, and leaves the requested mode unchanged. A19 reports
that refusal at critical severity, so the final Policy Engine decision is `BLOCK`. A23 is not
applicable because no model was trained.

The Run needs exactly two human approvals for the two tool calls. An approval cannot supply
missing confinement. The experiment agent reports empty metrics and no model; Summarize retains
that unscored attempt for MIRA without proposing a model or failing before the audit. Passing
this test proves that the default registry, refusal evidence, approvals and blocked outcome agree.
