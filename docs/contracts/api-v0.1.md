# Thymira Run API v0.1

*Status: frozen for MVP week 1. Owner: P1 (Runtime / Tech Lead). This document defines the
HTTP boundary; route handlers and persistence are implemented in the following roadmap days.*

The API is the only door to the runtime. Clients do not own Run state. The domain records in
`thymira.schemas` are the response contract; `thymira.api` contains only request models and
response envelopes.

## Common rules

- Base paths are unversioned in the local MVP: `/runs`. A public version prefix is a later API
  decision, not a reason to duplicate the first contract now.
- Requests and responses use `application/json` and UTF-8.
- Timestamps are UTC ISO-8601 strings.
- Identifiers use the shared prefixed format (`run_…`, `session_…`, and so on).
- Unknown request fields are rejected with `422 Unprocessable Entity`.
- Domain records are serialized from `thymira.schemas`; the API does not redefine `Run` or
  `Event`.
- Large outputs are artifacts. The canonical event log carries exact model-visible structured
  evidence after source credential scrubbing; API event pages and exports carry redacted
  projections with explicit source provenance rather than chain-of-thought.

## `POST /runs`

Creates a new Run in `CREATED` status.

Request body:

```json
{
  "prompt": "Analyze this dataset",
  "project_id": "project_0123456789abcdef0123456789abcdef",
  "session_id": "session_0123456789abcdef0123456789abcdef",
  "client": "cli"
}
```

The optional `Idempotency-Key` header makes retries safe. The first request creates and dispatches
one Run; a later request with the same key returns that same Run instead of creating or dispatching
another one. An empty key is rejected with `422`.

Write requests may include the optional `X-Thymira-Actor` header. The MVP records its value as the
authenticated human actor. When it is absent, the trusted `system` actor is recorded. A later
authentication task replaces this resolver without changing route contracts.

Only `prompt` is required. The current CLI sends only `prompt`; when the optional project or
session identifiers are absent, the runtime resolves the configured project and creates or
reuses a session for the client.

Response: `201 Created`, body is a committed publication envelope. The `run` member is the
canonical `thymira.schemas.Run` record and `receipt` proves that its session and initial work
publication is durable:

```json
{
  "run": {"id": "run_0123456789abcdef0123456789abcdef", "status": "CREATED"},
  "receipt": {
    "publication_id": "publication_0123456789abcdef0123456789abcdef",
    "run_id": "run_0123456789abcdef0123456789abcdef",
    "session_id": "session_0123456789abcdef0123456789abcdef",
    "work_ids": [],
    "committed_at": "2026-09-08T10:00:00Z"
  }
}
```

Until the API owns the durable lifecycle it answers with the canonical `thymira.schemas.Run`
record alone. A client reads the envelope when the response carries `run` or `receipt`, and the
Run record otherwise; an envelope that carries an incomplete or foreign `receipt` is still
refused. Only `run --headless`, which observes the publication's correlated turn, requires the
receipt and refuses a response without one.

Errors:

- `422`: empty prompt, malformed identifier, or unknown field.
- `404`: explicitly supplied project or session does not exist.

## `GET /runs`

Lists the Runs belonging to the project configured for this API instance. The optional
`project_id` query parameter may repeat that configured id; another project is not selected
implicitly.

Query parameters:

- `project_id`: optional configured project id.
- `status`: optional Run status filter.
- `limit`: optional page size; defaults to `50` and must be at least `1`.
- `cursor`: optional opaque cursor returned by the previous page.

Response: `200 OK`, body is a page envelope containing canonical `thymira.schemas.Run` records:

```json
{
  "items": [],
  "next_cursor": null
}
```

Errors:

- `404`: the requested project is not configured for this API.
- `422`: invalid status, limit, cursor or project identifier.

## `GET /runs/{run_id}`

Returns the canonical Run record, including its status and child-record identifiers.

Response: `200 OK`, body is `thymira.schemas.Run`.

Errors:

- `404`: the Run does not exist.
- `422`: malformed path identifier.

## `POST /runs/{run_id}/plan`

Enqueues durable plan generation for a Run and records the proposal through the lifecycle owner.
The request does not itself return the plan body or authorize execution.

Response: `202 Accepted`, body is an `EnqueueReceipt`:

```json
{
  "input_id": "input_0123456789abcdef0123456789abcdef",
  "run_id": "run_0123456789abcdef0123456789abcdef",
  "accepted": true,
  "duplicate": false,
  "durable_at": "2026-09-08T10:00:00Z"
}
```

After the correlated `turn.ended` event is durable, read the latest plan from
`GET /runs/{run_id}/plan`.

Response: `200 OK`, body is the complete durable plan board. It includes `run_id`, `plan_id`,
`revision`, `goal`, `goal_revision`, `goal_status`, ordered `items`, `completions`, `mode`,
`review_feedback`, `rounds`, bounded continuation fields, and an optional `artifact` containing
the advisory artifact reference (`artifact_id`, `run_id`, `revision`, `sha256`, `storage_key`,
`advisory`) plus its verified rendered `content`. A Run without a persisted plan returns `404`
with code `plan_not_found`.

Errors:

- `404`: the Run does not exist or is outside the API's configured project.
- `422`: malformed path identifier.

## `POST /runs/{run_id}/resume`

Requests continuation from the last durable checkpoint. It does not create a second Run and is
idempotent while an existing resume is already in progress.

Request body: empty or `{}`.

Response: `202 Accepted`, body is an `EnqueueReceipt`. The receipt acknowledges durable
acceptance only; observe its correlated `turn.ended` event and then read `GET /runs/{run_id}`.
Until the API owns the durable lifecycle it applies the mutation inside the request and answers
with the resulting `thymira.schemas.Run` record, which is already the settled snapshot. A client
reads a complete Run record as that settled answer and validates every other body strictly as an
`EnqueueReceipt`.

Errors:

- `404`: the Run does not exist.
- `409`: the Run has no resumable checkpoint or is still waiting for human approval.
- `422`: malformed path identifier.

Human approval remains a separate decision channel; resuming an approval-gated Run never bypasses
the Policy Engine or the human approval event.

## `POST /runs/{run_id}/approve` and `POST /runs/{run_id}/reject`

Resolve the pending human decision for a Run in `WAITING_FOR_APPROVAL`. The API sends the answer
through the existing Gate, which appends the `human.approval` evidence before the RunController
continues or blocks the Run. The endpoints never change a policy decision and never authorize a
Run directly from an LLM response.

Request body:

```json
{
  "actor": "risk-reviewer",
  "note": "Reviewed the evidence."
}
```

`actor` is optional for backwards compatibility and `note` is optional. The preferred identity is
the `X-Thymira-Actor` header; when no header is supplied, the legacy body actor is still accepted.
When neither is supplied, the `system` actor is used. Route authentication and authorization are a
later API task.

Response: `202 Accepted`, body is an `EnqueueReceipt`. The receipt acknowledges durable
acceptance only; observe its correlated `turn.ended` event and then read `GET /runs/{run_id}`.
Until the API owns the durable lifecycle it applies the mutation inside the request and answers
with the resulting `thymira.schemas.Run` record, which is already the settled snapshot. A client
reads a complete Run record as that settled answer and validates every other body strictly as an
`EnqueueReceipt`.

`approve` records an accepted decision and resumes the graph from its checkpoint. `reject`
records a rejected decision and transitions the Run to `BLOCKED`.

Errors:

- `404`: the Run does not exist or is outside the API's configured project.
- `409`: the Run is not waiting for an unresolved approval, or its approval evidence is invalid.
- `422`: malformed path identifier or request body.

## `GET /runs/{run_id}/events`

Returns an ordered, bounded page from the Run's hash-chained event log.

Query parameters:

- `after_seq`: optional last-seen sequence number; defaults to `-1` (the beginning).
- `limit`: optional page size; defaults to `100`, maximum `1000`.

Response: `200 OK` with this envelope:

```json
{
  "run_id": "run_0123456789abcdef0123456789abcdef",
  "items": [],
  "next_after_seq": null,
  "has_more": false
}
```

The `items` values are redacted `EventProjection` records in ascending `seq` order. They carry
`projection: "redacted"` and `source_hash`, which identifies the canonical local event; `hash` and
`prev_hash` are intentionally absent because the exported payload is redacted.

Errors:

- `404`: the Run does not exist.
- `422`: invalid `after_seq` or `limit`.

When `follow=1` is supplied or the request `Accept` header includes `text/event-stream`, the
same route returns `200 OK` with `Content-Type: text/event-stream` and
`X-Thymira-Redacted-Stream: event-projection-v1`. The optional `since` query parameter identifies
the last event already received; `Last-Event-ID` takes precedence when a client reconnects. Both
cursors use the event's integer `seq`, and the stream sends only events whose sequence is greater
than the selected cursor. Each message contains the sequence as its SSE `id`, the EventType value
as `event`, and the redacted EventProjection as `data`. A canonical validated `turn.ended` event
records a settled turn; a Run terminal event may close an unbound stream. No transport sentinel
stands in for a missing durable turn-end record.

## `GET /runs/{run_id}/artifacts`

Lists every artifact recorded in the Run's manifest, active and superseded revisions alike, ordered
by creation time.

Response: `200 OK`:

```json
{
  "run_id": "run_0123456789abcdef0123456789abcdef",
  "items": []
}
```

Each item is a `thymira.schemas.Artifact` record (`id`, `name`, `kind`, `uri`, `sha256`,
`size_bytes`, `media_type`, `produced_by`, `created_at`, lineage and validity fields). A superseded
revision keeps its record with `valid: false` and its `invalidated_reason`.

Errors:

- `404`: the Run does not exist or is outside the API's configured project.
- `409`: the Run's artifact manifest cannot be read (`run_integrity_error`).
- `422`: malformed path identifier.

## `GET /runs/{run_id}/artifacts/{artifact_id}`

Returns one artifact and its content. The API's response boundary serves JSON only, so the content
is a projection rather than a raw download: the stored bytes are read within a 2 MiB bound, their
length and sha256 are recomputed against the manifest, and only then are they returned as UTF-8
text (when the bytes are NUL-free valid UTF-8) or standard base64, after export redaction.

Response: `200 OK`:

```json
{
  "run_id": "run_0123456789abcdef0123456789abcdef",
  "artifact": {"id": "artifact_0123456789abcdef0123456789abcdef", "sha256": "…"},
  "encoding": "utf-8",
  "content": "# EDA\n",
  "redacted": false,
  "withheld_reason": null,
  "projection": "redacted"
}
```

`artifact.sha256` names the bytes the API verified. When `redacted` is `true`, redaction changed the
text and `content` no longer hashes to that digest. Binary content whose base64 form redaction
would alter is withheld: `content` is `null` and `withheld_reason` explains why.

Errors:

- `404`: the Run is unknown or out of scope (`run_not_found`), or the artifact is not recorded for
  it (`artifact_not_found`).
- `409`: the stored bytes do not match the manifest (`artifact_tampered`), or the revision's content
  cannot be read, as for a superseded revision (`artifact_unavailable`).
- `413`: the artifact is larger than the content bound (`artifact_too_large`).
- `422`: malformed path identifier, or an identifier that is not an artifact
  (`invalid_artifact_id`).

## Project inputs: `/project/context` and `/project/datasets`

These routes change the project, not a Run. Every Run re-reads `.thymira/context.md` and the
`datasets` that `.thymira/config.yaml` declares when it starts, so a change reaches the next Run; a
Run that already registered a dataset keeps its own content-addressed copy. Reads need the `READ`
permission and changes need `WRITE`. Every route answers `503 project_not_configured` when the API
has no project workspace.

### `GET /project/context` and `PUT /project/context`

`GET` returns the context document as a redacted projection, with the sha256 of the stored bytes:

```json
{
  "project_id": "project_0123456789abcdef0123456789abcdef",
  "text": "# Credit risk\n",
  "sha256": "…",
  "exists": true,
  "size_bytes": 14,
  "redacted": false,
  "projection": "redacted"
}
```

A project without the file reads as empty text, `exists: false` and the sha256 of no bytes.

`PUT` takes `{"text": "…", "expected_sha256": "…"}` and replaces the document atomically only
while the stored bytes still have `expected_sha256`; the response is the saved document. When
`redacted` is `true`, the text a client holds carries masks, and the API refuses to write them over
the values they hide: edit the file directly.

Errors:

- `409`: the document changed after it was read (`context_conflict`), or it cannot be read or
  written (`context_unreadable`, `context_unwritable`).
- `413`: the UTF-8 text is over 256 KiB (`context_too_large`).
- `422`: the text carries an export-redaction mask (`invalid_context`), or the body is malformed.

### `GET /project/datasets`

Response: `200 OK`:

```json
{
  "project_id": "project_0123456789abcdef0123456789abcdef",
  "items": [
    {
      "name": "german_credit",
      "path": "data/applications.csv",
      "target": "is_high_risk",
      "present": true,
      "size_bytes": 64321,
      "modified_at": "2026-09-11T09:12:00Z"
    }
  ]
}
```

Errors: `409` when `config.yaml` cannot be loaded or validated (`project_config_invalid`).

### `PUT /project/datasets/{name}`

The request body is the file itself (`application/octet-stream`). The `filename` query parameter is
required and its suffix must be `.csv` or `.parquet`; the optional `target` query parameter names
the column the dataset predicts. `name` uses lowercase letters, digits, `_` and `-`.

The API streams the body into a staging file inside the project's `data/` directory and refuses it
past 256 MiB, the dataset registration ceiling. It then reads the file with the reader registration
uses and checks `target` against its columns; only then does it move the file to its declared path
and write the declaration into `config.yaml`, atomically, keeping the file's leading comments and
refusing any change that would not validate as a `ProjectConfig`. A new dataset is stored at
`data/<name><suffix>`. A replacement keeps the declared path when the suffix matches, and keeps the
declared target while it is still a column. Every declared file is therefore one the next Run's
Inspect step registers as a source with its `source_path`, as MIRA's A18 control requires.

Response: `200 OK`, the declared dataset and the shape registration will capture:

```json
{
  "project_id": "project_0123456789abcdef0123456789abcdef",
  "dataset": {"name": "german_credit", "path": "data/applications.csv", "target": "is_high_risk"},
  "rows": 1000,
  "columns": ["age", "credit_amount", "is_high_risk"]
}
```

Errors:

- `400`: the client closed the upload before the body arrived (`upload_interrupted`).
- `409`: the file or `config.yaml` could not be written (`dataset_unwritable`).
- `413`: the file is over 256 MiB (`dataset_too_large`).
- `422`: an invalid name or suffix, an empty or unreadable file, a `target` that is not a column, or
  a change that would make `config.yaml` invalid (`invalid_dataset`).

### `PATCH /project/datasets/{name}` and `DELETE /project/datasets/{name}`

`PATCH` takes `{"target": "column"}`, or `{"target": null}` to clear it, checks the column against
the file's header and returns the updated dataset. `DELETE` stops declaring the dataset and returns
the remaining list; the file stays on disk, because past Runs' evidence names its path.

Errors: `404` for an undeclared name (`dataset_not_found`), `409` when `config.yaml` cannot be
written (`project_config_unwritable`), `422` for a target that is not a column (`invalid_dataset`).

## Error envelope

Errors use this shape when a structured response is possible:

```json
{
  "code": "run_not_found",
  "message": "Run 'run_…' was not found.",
  "details": {}
}
```

The error `code` is stable for clients; `message` is explanatory and may change.

The API maps domain failures consistently: `404` means the requested resource is not found, `409`
means the requested operation conflicts with the current Run state, and `422` means the request
could not be validated. All structured errors use `application/problem+json`.

## Compatibility decisions for contract 0.5

- Prefixed UUID4 identifiers remain the MVP strategy. A time-ordered identifier requires a
  separately versioned migration.
- The `Event` records returned by `GET /runs/{run_id}/events` carry the Contract 0.3 envelope:
  `event_id`, `schema_version`, `producer`, `producer_version`, `correlation_id`, `causation_id`
  and `authorization_context_sha256` alongside `seq`, `type`, `actor`, `surface`, `payload`,
  `prev_hash` and `hash`. Clients read the envelope; they never construct events.
- `PolicyDecision` no longer carries a human resolution. A separate `Approval` record holds
  `approved`/`approved_by` and references one authorization context, so a decision and its
  approval are two records in the response, not one.
- `Run.final_decision` remains a denormalized read field. Runtime code must keep it consistent
  with the referenced `PolicyDecision`.
- Event payload size is an operational boundary, not a new domain field. Large content belongs
  in artifacts; the event store/API will enforce its configured limit when implemented.
- SSE uses a polling subscription over the MVP local EventStore. A future push bus can implement
  the same subscription seam without changing this HTTP contract.
- `Experiment.tracker_run_id` stores the MLflow run id. Parameters and metrics are snapshots of
  the MLflow record, while artifact ids point to Thymira's verifiable artifact records.
