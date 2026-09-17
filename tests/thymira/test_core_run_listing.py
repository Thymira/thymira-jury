"""Behavioural tests for ``RunService.list_runs`` status hydration and paging.

Regression coverage for finding #56-3: the local store records only a Run's *creation* status,
so a listing must hydrate the current status (from the event chain, via ``RunController``) before
it filters and pages, or a ``status`` query is either empty or returns stale bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.core import RunService, SessionService
from thymira.events import sha256_text
from thymira.schemas import Actor, Decision, EventType, RunStatus, new_id
from thymira.state import LocalRunStore, LocalSessionRepository

if TYPE_CHECKING:
    from pathlib import Path


def _service(tmp_path: Path) -> tuple[RunService, SessionService]:
    """Build an event-backed RunService over an isolated local store."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path))
    service = RunService(
        store,
        sessions,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    return service, sessions


def _complete(service: RunService, run_id: str) -> None:
    """Drive a freshly created Run all the way to its terminal COMPLETED state."""
    service.advance(run_id, RunStatus.PLANNING)
    service.advance(run_id, RunStatus.RUNNING)
    service.advance(run_id, RunStatus.AUDITING)
    service.complete(run_id, actor=Actor.system())


def test_list_runs_status_filter_matches_the_current_status_not_the_creation_status(
    tmp_path: Path,
) -> None:
    """A ``status`` query reflects the hydrated current status, not the frozen creation status."""
    service, sessions = _service(tmp_path)
    session = sessions.create_session(project_id=new_id("project"), client="cli")
    still_created = service.create_run(session.id, "open", actor=Actor.system(), workspace=tmp_path)
    completed = service.create_run(session.id, "done", actor=Actor.system(), workspace=tmp_path)
    _complete(service, completed.id)

    # Before the fix ?status=completed was always empty (the frozen creation status is CREATED).
    done = service.list_runs(status=RunStatus.COMPLETED)
    assert [run.id for run in done.items] == [completed.id]
    assert done.items[0].status is RunStatus.COMPLETED

    # Before the fix ?status=created returned the completed Run with a body reading COMPLETED.
    created = service.list_runs(status=RunStatus.CREATED)
    assert [run.id for run in created.items] == [still_created.id]
    assert all(run.status is RunStatus.CREATED for run in created.items)


def test_list_runs_pages_across_a_boundary_under_a_status_filter(tmp_path: Path) -> None:
    """Paging with a status filter is consistent because hydration precedes filtering and paging."""
    service, sessions = _service(tmp_path)
    session = sessions.create_session(project_id=new_id("project"), client="cli")
    completed_ids: set[str] = set()
    for index in range(3):
        done = service.create_run(
            session.id, f"done-{index}", actor=Actor.system(), workspace=tmp_path
        )
        _complete(service, done.id)
        completed_ids.add(done.id)
        # Interleave a Run that stays CREATED so a naive post-paging filter would mis-size a page.
        service.create_run(session.id, f"open-{index}", actor=Actor.system(), workspace=tmp_path)

    expected = [
        run.id
        for run in sorted(
            (service.get_run(run_id) for run_id in completed_ids),
            key=lambda run: (run.created_at, run.id),
        )
    ]

    collected: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        page = service.list_runs(status=RunStatus.COMPLETED, limit=2, cursor=cursor)
        pages += 1
        collected.extend(run.id for run in page.items)
        assert all(run.status is RunStatus.COMPLETED for run in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert collected == expected
    assert set(collected) == completed_ids
    assert pages >= 2


def test_get_run_projects_child_ids_from_authoritative_events(tmp_path: Path) -> None:
    """A Run view exposes agents, tools and artifacts recorded after its creation."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path))
    service = RunService(
        store,
        sessions,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create_session(project_id=new_id("project"), client="cli")
    run = service.create_run(session.id, "inspect", actor=Actor.system(), workspace=tmp_path)
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool_id = new_id("tool")
    artifact_id = new_id("artifact")
    store.append(
        run.id,
        EventType.AGENT_STARTED,
        Actor.system(),
        {"agent": "data", "task_id": task_id},
        expected_version=None,
        subject_id=agent_id,
    )
    store.append(
        run.id,
        EventType.TOOL_STARTED,
        Actor.system(),
        {"tool": "read_file", "tool_call_id": tool_id},
        expected_version=None,
        subject_id=tool_id,
    )
    store.append(
        run.id,
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"artifact_ids": [artifact_id]},
        expected_version=None,
        subject_id=tool_id,
    )

    projected = service.get_run(run.id)

    assert projected.agent_ids == (agent_id,)
    assert projected.task_ids == (task_id,)
    assert projected.tool_call_ids == (tool_id,)
    assert projected.artifact_ids == (artifact_id,)


def test_get_run_projects_audit_and_experiment_metadata_from_authoritative_events(
    tmp_path: Path,
) -> None:
    """A Run view includes the audit and experiment records emitted after creation."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path))
    service = RunService(
        store,
        sessions,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create_session(project_id=new_id("project"), client="cli")
    run = service.create_run(session.id, "audit", actor=Actor.system(), workspace=tmp_path)
    experiment_id = new_id("experiment")
    artifact_id = new_id("artifact")
    finding_id = new_id("finding")
    decision_id = new_id("decision")
    store.append(
        run.id,
        EventType.EXPERIMENT_STARTED,
        Actor.system(),
        {"experiment_id": experiment_id},
        expected_version=None,
        subject_id=experiment_id,
    )
    store.append(
        run.id,
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {"artifact_id": artifact_id},
        expected_version=None,
        subject_id=artifact_id,
    )
    store.append(
        run.id,
        EventType.AUDIT_FINDING,
        Actor.system(),
        {"finding_id": finding_id},
        expected_version=None,
        subject_id=finding_id,
    )
    store.append(
        run.id,
        EventType.POLICY_DECISION,
        Actor.system(),
        {"id": decision_id, "decision": Decision.WARNING.value},
        expected_version=None,
    )

    projected = service.get_run(run.id)

    assert projected.experiment_ids == (experiment_id,)
    assert projected.artifact_ids == (artifact_id,)
    assert projected.finding_ids == (finding_id,)
    assert projected.policy_decision_id == decision_id
    assert projected.final_decision is Decision.WARNING
