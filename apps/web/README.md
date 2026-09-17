# thymira-web

The Thymira web console: a local browser client of the Thymira API. It lists Runs, creates one,
answers its risk interview, and shows each Run's overview, plan, agents, reviews, tool calls,
artifacts, MIRA audit, live events and model usage.

| | |
|---|---|
| Import name | `thymira.web` |
| Owner (MVP roadmap) | P5 (CLI / UX / QA) |
| Priority | P1 |
| Location | `apps/web/` |

## Run it

Start the API, then the console, from the repository root:

```bash
uv run thymira-api --workspace examples/credit-risk
uv run thymira-web --api-url http://127.0.0.1:8000
```

Open <http://127.0.0.1:8080> and paste the API token when asked. The API prints where it wrote the
token at start-up (by default `<workspace>/.thymira/runtime/api-token`). `just web` runs the same
console script.

## How it is built

- `thymira.web.app` is a Starlette application: `/` and `/static/*` serve the console and `/api/*`
  forwards to the API. There is no build step; the views are ES modules under `static/js/`.
- The console holds no Run state and no credential. The browser keeps the token in
  `sessionStorage` for the tab and sends it as `Authorization`; the console forwards that header
  unchanged, drops cookies and every other non-allowlisted header, and never adds one.
- Only the API's route roots are forwarded, one plain path segment at a time. A foreign `Host`
  name and a cross-origin state-changing request are refused, and every response carries a
  same-origin Content-Security-Policy.
- Views render Run data as text nodes, never as HTML. Artifact content comes from the API as a
  digest-verified, redacted projection.
- Request bodies are streamed to the API and bounded: 1 MiB, or 256 MiB for a dataset upload.
- The run list is loaded once, on a filter change and on "Refresh", never on a timer: the API
  verifies every Run's event log to answer it. A Run's header appears from its record while its
  event page, which the API verifies and redacts in full, is still loading.

## Runs

Creating a Run starts it: the API dispatches it as soon as it is recorded. A Run waiting for an
interview answer marks its Interview tab (answer there, Ctrl+Enter), and a Run waiting for a review
marks its Approvals tab. "Run again" starts a new Run with the same prompt.

## Project inputs

A Run reads what the project declares: `.thymira/context.md` and the datasets
`.thymira/config.yaml` lists. The console edits both, beside the prompt on the New run page and
under each Run's Inputs tab, through the API's `/project` routes (`docs/contracts/api-v0.1.md`); it
writes nothing itself.

- **Datasets.** Add a CSV or Parquet file under a name, optionally with its target column. The API
  reads the file with the reader registration uses before declaring it, so every declared file is
  registered as a source, with its `source_path`, when the next Run's Inspect step starts. Replace
  the file, change the target or stop declaring it from the same list; "Use in prompt" inserts the
  name into the prompt.
- **Context.** The editor saves `context.md` only if nobody changed it since it was loaded
  (Ctrl+S). When redaction masked part of it, the editor is read-only: edit the file directly.
- Changes reach the next Run. A Run that already registered a dataset keeps its own copy, and its
  Inputs tab lists the sources it registered.

## Rules

- Imports no runtime member: HTTP to the API only (import-linter contract "Clients own no state").
- Everything is English; tests live in `tests/thymira/test_web.py`.
