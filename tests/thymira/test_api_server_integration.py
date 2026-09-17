"""Integration tests for the local API server composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from thymira.api import server
from thymira.api.server import build_local_app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_PROJECT_CONFIG = """\
project:
  name: local-api-test
  domain: local_api
governance:
  frameworks: []
"""


def _workspace(tmp_path: Path) -> Path:
    """Create the smallest valid Thymira workspace for the API composition root."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / ".thymira"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(_PROJECT_CONFIG, encoding="utf-8")
    return tmp_path


def test_local_app_serves_health_check_for_a_workspace(tmp_path: Path) -> None:
    """The local entrypoint composes a real FastAPI app over local dependencies."""
    workspace = _workspace(tmp_path)

    app = build_local_app(workspace)
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app.state.runtime_deps.project_resolution is not None
    assert app.state.runtime_deps.project_resolution.workspace == workspace / ".thymira"
    assert (workspace / ".thymira" / "runtime" / "runs").is_dir()


def test_local_app_honours_explicit_state_root(tmp_path: Path) -> None:
    """Container and CI callers can keep mutable state outside the source workspace."""
    workspace = _workspace(tmp_path / "workspace")
    state_root = tmp_path / "state"

    app = build_local_app(workspace, state_root=state_root)

    assert (state_root / "runs").is_dir()
    assert not (workspace / ".thymira" / "runtime").exists()
    assert app.state.runtime_deps.project_resolution is not None


def test_server_main_sets_a_bounded_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API process gives durable writers a bounded window to finish on termination.

    ``build_local_app`` is faked out below, but ``main`` still resolves the real API credential
    against ``--workspace`` before it gets there (F13.3), so the workspace has to be a throwaway
    ``tmp_path`` -- a real one (such as ``"."``) would mint and persist an actual
    ``.thymira/runtime/api-token`` in the caller's working directory, and a second run in the same
    checkout would then fail with ``FileExistsError`` on that leftover token file.

    ``main`` also really calls ``load_env_file`` (untested here; ``test_api_env.py`` covers it),
    which -- with no ``--env-file`` given -- searches upward from the working directory and writes
    straight into ``os.environ`` with no ``monkeypatch``-reversible seam of its own. Run from a
    worktree nested under a checkout that carries a real ``.env``, that search would find and load
    it, silently rebinding every later test's ``THYMIRA_*`` environment for the rest of the
    session. This test does not exercise env-file loading, so it is stubbed to a no-op.
    """
    observed: dict[str, object] = {}

    def fake_build(
        _workspace: Path,
        *,
        state_root: Path | None = None,
        principal_resolver: object | None = None,
    ) -> object:
        del state_root, principal_resolver
        return object()

    def fake_run(_app: object, **kwargs: object) -> None:
        observed.update(kwargs)

    def fake_load_env_file(_env_file: Path | None) -> Path | None:
        return None

    monkeypatch.setattr(server, "build_local_app", fake_build)
    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    monkeypatch.setattr(server, "load_env_file", fake_load_env_file)

    assert server.main(["--workspace", str(tmp_path), "--port", "8123"]) == 0
    assert observed["timeout_graceful_shutdown"] == 5


def test_replace_credential_recovers_a_leftover_token_and_cleans_up_on_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--replace-credential`` starts despite a stopped server's leftover token file.

    Reproduces the restart failure this fix addresses: a token file already sits at the resolved
    state root (as a stopped or crashed server would leave one) before ``main`` runs. On a clean
    exit the token this process minted is removed, so a later start with no flag finds nothing.

    ``main`` also really calls ``load_env_file`` with no ``--env-file`` given here; stubbed for the
    same reason as ``test_server_main_sets_a_bounded_graceful_shutdown`` above -- unstubbed, it
    would find and load this checkout's real ``.env``, silently rebinding every later test's
    ``THYMIRA_*`` environment (routing included) for the rest of the session.
    """

    def fake_build(
        _workspace: Path,
        *,
        state_root: Path | None = None,
        principal_resolver: object | None = None,
    ) -> object:
        del state_root, principal_resolver
        return object()

    def fake_run(_app: object, **kwargs: object) -> None:
        del kwargs

    def fake_load_env_file(_env_file: Path | None) -> Path | None:
        return None

    monkeypatch.setattr(server, "build_local_app", fake_build)
    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    monkeypatch.setattr(server, "load_env_file", fake_load_env_file)

    state_root = tmp_path / ".thymira" / "runtime"
    state_root.mkdir(parents=True)
    token_path = state_root / "api-token"
    token_path.write_text("leftover-token-value-from-a-stopped-server\n", encoding="utf-8")

    exit_status = server.main(
        ["--workspace", str(tmp_path), "--port", "8126", "--replace-credential"]
    )

    assert exit_status == 0
    assert not token_path.exists()


def test_without_the_flag_a_leftover_token_still_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unchanged default: no flag, no recovery -- the process refuses exactly as it does today.

    ``load_env_file`` is stubbed for the same reason as the two tests above: unstubbed, this call
    still runs before the credential check refuses the start, and would load this checkout's real
    ``.env`` into the process.
    """

    def fake_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("build_local_app must not run when the credential is refused")

    def fake_load_env_file(_env_file: Path | None) -> Path | None:
        return None

    monkeypatch.setattr(server, "build_local_app", fake_build)
    monkeypatch.setattr(server, "load_env_file", fake_load_env_file)

    state_root = tmp_path / ".thymira" / "runtime"
    state_root.mkdir(parents=True)
    token_path = state_root / "api-token"
    token_path.write_text("leftover-token-value-from-a-stopped-server\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        server.main(["--workspace", str(tmp_path), "--port", "8127"])

    assert excinfo.value.code == 2
    assert token_path.read_text(encoding="utf-8") == "leftover-token-value-from-a-stopped-server\n"
