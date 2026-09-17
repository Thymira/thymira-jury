"""Repository-wide pytest configuration.

`--basetemp=.pytest-tmp/<name>` (the documented way to run concurrent suites) fails right
after `just clean` because pytest creates the base temporary directory without its parents;
create the parent here so the nested form always works.

The suite also refuses to emit telemetry. `create_app` calls `thymira.observability.configure`,
which builds a live Langfuse client whenever both `LANGFUSE_*` keys are in the environment — so a
developer who exports real keys would, without this, ship a trace of every test that builds an app
to their own Langfuse project. Tests never reach the network (AGENTS.md), and that has to hold for
telemetry too, so tracing is hard-disabled for the whole session.

Similarly, `configured_builtins_registry()` (the only production seam that reads
`THYMIRA_SANDBOX_*`, via `SandboxSettings.from_env()`) reads the real process environment when a
test builds a production composition root without overriding it -- a developer's or CI's own
`THYMIRA_SANDBOX_MODE=danger_full_access` would otherwise silently switch every subprocess tool a
test exercises into an unconfined mode, changing approval flows and, worse, letting a "test" run
real, unconfined training. Every `THYMIRA_SANDBOX_*` name is cleared here for the whole session; a
test that needs one sets it explicitly with `monkeypatch.setenv`, which reverts to this cleared
state afterward rather than to whatever the developer's shell happened to export.

The same holds for model routing: `ModelRoutePolicy.from_environment()`,
`thymira.agents.llm.routing` and every provider seam read `THYMIRA_MODEL*`,
`THYMIRA_{AGENT,THY,MIRA}_MODEL`, `THYMIRA_{AGENT,THY,MIRA}_MIN_TIER` and
`THYMIRA_ALLOWED_MODEL_ROUTES` from the real process environment absent an explicit override --
including a `.env` a nested worktree's own `litellm` may pull in via its upward `load_dotenv()`
search (this repository's worktrees live under a parent checkout that carries a real `.env`, so
`load_dotenv()`'s default upward search finds it from any nested working directory). Clearing
these names once is not enough on its own: `LiteLLMProvider` imports the `litellm` package lazily
on first construction, and importing it runs `litellm`'s own module-level `load_dotenv()`, which
(with nothing already set) repopulates these exact names from that real `.env` the first time any
test in the session builds a real provider -- silently rebinding every later test's model routing
to a real model id. Importing `litellm` here first consumes that one-time module-level side effect
up front, so the later `import litellm` inside `LiteLLMProvider.__init__` is a cached no-op that
never calls `load_dotenv()` again; only then is it safe to clear the names below for the session.
`litellm` is always installed (it is `thymira.agents`' only production gateway), so the import is
not guarded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from thymira.agents.llm.routing import set_model_overrides

_SANDBOX_ENV_VAR_NAMES = (
    "THYMIRA_SANDBOX_BACKEND",
    "THYMIRA_SANDBOX_IMAGE",
    "THYMIRA_SANDBOX_MEMORY",
    "THYMIRA_SANDBOX_CPUS",
    "THYMIRA_SANDBOX_PIDS_LIMIT",
    "THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES",
    "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES",
    "THYMIRA_SANDBOX_MODE",
)

_MODEL_ROUTE_ENV_VAR_NAMES = (
    "THYMIRA_MODEL",
    "THYMIRA_MODEL_FAST",
    "THYMIRA_MODEL_STANDARD",
    "THYMIRA_MODEL_FRONTIER",
    "THYMIRA_AGENT_MODEL",
    "THYMIRA_THY_MODEL",
    "THYMIRA_MIRA_MODEL",
    "THYMIRA_ORCHESTRATOR_MODEL",
    "THYMIRA_AGENT_MIN_TIER",
    "THYMIRA_THY_MIN_TIER",
    "THYMIRA_MIRA_MIN_TIER",
    "THYMIRA_ALLOWED_MODEL_ROUTES",
)


def pytest_configure(config: pytest.Config) -> None:
    """Ensure the parent of a nested ``--basetemp`` exists, and keep the suite offline."""
    basetemp = config.getoption("basetemp", default=None)
    if basetemp:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)
    # Set rather than defaulted: an ambient "true" from the developer's shell must not win.
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
    # An ambient THYMIRA_SANDBOX_* (a developer's shell, or CI) must not silently change which
    # backend or mode a test's production composition root resolves to; see the module docstring.
    for name in _SANDBOX_ENV_VAR_NAMES:
        os.environ.pop(name, None)
    # Consume litellm's one-time module-level `load_dotenv()` side effect now, before clearing
    # below, so the first real LiteLLMProvider construction later in the session imports an
    # already-cached module instead of silently reloading these names from a parent `.env`
    # (a nested worktree's upward search); see the module docstring.
    import litellm  # noqa: F401  # imported only to force load_dotenv() now, once

    # An ambient THYMIRA_*MODEL*, THYMIRA_*_MIN_TIER or THYMIRA_ALLOWED_MODEL_ROUTES (a developer's
    # shell, CI, or litellm's own upward `load_dotenv()` from a nested worktree) must not silently
    # bind a test's model routing to a real model id or policy; see the module docstring.
    for name in _MODEL_ROUTE_ENV_VAR_NAMES:
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def _reset_model_route_overrides() -> None:
    """Clear routing's live model-override cache before every test.

    `set_model_overrides` (`thymira.agents.llm.routing`) is a plain in-process cache, not an
    environment variable `monkeypatch.setenv` would auto-revert: any test that builds a production
    API composition (`build_default_deps` primes this cache from its own settings store on every
    call) or calls `set_model_overrides` directly would otherwise leak a value into whichever test
    runs next in the same session, silently overriding that later test's own `monkeypatch.setenv`
    on a `THYMIRA_*MODEL*` name -- the override cache is consulted before the environment.
    """
    set_model_overrides({})
