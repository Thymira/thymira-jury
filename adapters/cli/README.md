# thymira-cli

The `thymira` CLI — first official interface and a thin client of the Thymira API (thymira run / status / runs / audit / experiment / approve / reject). Contains no THY/MIRA logic.

| | |
|---|---|
| Import name | `thymira.cli` |
| Owner (MVP roadmap) | P5 (CLI / UX / QA) |
| MVP priority | P0 |
| Location | `adapters/cli/` |

## Status

The initial CLI shell and the week-one `run` and `status` commands are implemented. Running
`thymira` or `thymira --help` displays the branded welcome screen, global options and commands.
`run` creates a run through `POST /runs` and receives a publication receipt; `status` retrieves it
through `GET /runs/{id}`. Lifecycle mutations consume `202` enqueue receipts, observe the
correlated durable turn through the event API, and then read the resulting Run. `plan` follows the
same receipt path before rendering the persisted board from `GET /runs/{id}/plan`. The CLI reports
a clear connection error until the FastAPI endpoints are available and never invents local run
state.

## Development

Prepare the complete workspace and run the CLI from the repository root:

```bash
just setup
just thymira
just thymira --help
just thymira --version
just thymira run "Analyze iris.csv and train a baseline classifier"
just thymira status run_4f28c60d5d5a4fe5b14c5e68931f1234
```

The CLI is intentionally a thin adapter. It must remain executable without importing runtime
members; commands call the Thymira API through `httpx`. The API defaults to
`http://127.0.0.1:8000` and can be changed with the `THYMIRA_API_URL` environment variable.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
