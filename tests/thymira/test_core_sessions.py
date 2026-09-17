"""Acceptance tests for RA-CORE-01 session and project bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from thymira.core import (
    ProjectResolver,
    SessionNotFoundError,
    SessionProjectMismatchError,
    SessionService,
    load_project_config,
)
from thymira.schemas import Framework, new_id
from thymira.state import InMemorySessionRepository

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_session_service_creates_and_attaches_a_run() -> None:
    """Creating a session and attaching a Run preserves the association."""
    service = SessionService(InMemorySessionRepository())
    session = service.create_session(project_id=new_id("project"), client="cli")
    run_id = new_id("run")

    attached = service.attach_run(session.id, run_id)

    assert attached.run_ids == (run_id,)
    assert service.get_session(session.id).run_ids == (run_id,)


def test_session_service_attach_run_is_idempotent() -> None:
    """Retrying the same association does not duplicate the Run id."""
    service = SessionService(InMemorySessionRepository())
    session = service.create_session(project_id=new_id("project"), client="cli")
    run_id = new_id("run")

    service.attach_run(session.id, run_id)
    attached = service.attach_run(session.id, run_id)

    assert attached.run_ids == (run_id,)


def test_session_service_validates_unknown_and_cross_project_sessions() -> None:
    """Session lookup rejects missing ids and ids belonging to another project."""
    service = SessionService(InMemorySessionRepository())
    project_id = new_id("project")
    session = service.create_session(project_id=project_id, client="cli")

    with pytest.raises(SessionNotFoundError):
        service.get_session(new_id("session"))
    with pytest.raises(SessionProjectMismatchError):
        service.resolve(project_id=new_id("project"), client="cli", session_id=session.id)


def test_project_config_and_resolver_load_credit_risk_governance() -> None:
    """The reference project resolves its identity and configured frameworks."""
    project_path = REPO_ROOT / "examples/credit-risk"

    config = load_project_config(project_path / ".thymira/config.yaml")
    resolution = ProjectResolver().resolve(project_path)

    assert config.project.name == "credit-risk"
    assert resolution.project_id.startswith("project_")
    assert resolution.frameworks == (Framework.EU_AI_ACT, Framework.CREDIT_RISK)
