# credit-risk — stable project context for THY and MIRA

This project supports offline research on the registered German Credit dataset. The individual
Run prompt defines whether the activity is descriptive analysis or model training and names its
required deliverables; this file does not override that Run-specific scope.

## Registered dataset

- Logical name: `german_credit`.
- Workspace path: use the exact path mapped in `.thymira/config.yaml`; never infer or hardcode a
  filesystem path from the logical name.
- Shape: 998 historical observations, 20 predictors, and one binary target.
- Target: `is_high_risk`, where `0` means good credit risk and `1` means high credit risk.
- Data categories: credit history, account status, loan purpose and amount, employment, housing,
  and demographic attributes.
- Sensitive attributes requiring careful treatment: age, `personal_status_sex`, and
  `foreign_worker`.

## Project boundary and governance

- Purpose: offline data-science research assessing this historical dataset and, only when a Run
  explicitly requests it, training an experimental classification model.
- Affected population: historical German credit applicants represented in the dataset.
- Decision effect: outputs inform later human research decisions only. No Run in this project may
  approve, deny, rank, or otherwise affect a live applicant.
- Deployment context: local research environment; generated models and reports are not deployed
  or connected to an operational credit system.
- Autonomy: tools may autonomously calculate, plot, train an explicitly requested experimental
  model, and render artifacts, but they cannot make credit decisions or deploy a model.
- Human oversight: a Lead Data Scientist and Credit Risk Manager may stop or reject the work and
  must review artifacts before any downstream feature engineering, model use, or deployment.
- Jurisdiction: Spain and the European Union for this research project; the historical records
  describe applicants in Germany.
- Potential consequences: misleading analysis, discriminatory feature choices, invalid model
  conclusions, privacy harm, or later misuse of an experimental artifact. None of those artifacts
  is authorized for operational use by completing a Run.
- Applicable governance: EU AI Act, credit-risk model governance, privacy safeguards, and explicit
  review of sensitive-attribute use.

## Execution conventions

- Use the registered dataset and target contracts rather than relying on prose or guessed paths.
- Treat each Run prompt's requested filenames and artifact types as exact completion criteria.
- Record transformations, exclusions, seeds, validation choices, metrics, and limitations whenever
  a Run trains a model.
- A successful status is meaningful only when requested artifacts are registered and verifiable.
