"""thymira.core: provenance, phase control and session lifecycle tests."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import get_type_hints

import pytest

from thymira.core import (
    PHASE_ORDER,
    Phase,
    ProjectResolver,
    RunEventLog,
    RunGraphState,
    RunNotFoundError,
    RunService,
    SessionNotFoundError,
    SessionProjectMismatchError,
    SessionService,
    can_reopen,
    capture_run_environment,
    hash_inputs,
    initial_graph_state,
    next_phase,
    phase_precedes,
    source_control,
)
from thymira.core import runs as runs_module
from thymira.core.provenance import SourceControl
from thymira.events import sha256_text, verify_events
from thymira.schemas import Actor, EventType, Framework, Run, RunStatus, new_id
from thymira.state import (
    InMemorySessionRepository,
    LocalRunStore,
    LocalSessionRepository,
    SessionRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime_service(
    tmp_path: Path,
) -> tuple[RunService, SessionService, LocalRunStore]:
    """Build the audited local runtime boundary used by lifecycle tests."""
    store = LocalRunStore(tmp_path / "runs")
    session_repository = LocalSessionRepository(tmp_path)
    sessions = SessionService(session_repository)
    service = RunService(
        store,
        sessions,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    return service, sessions, store


def test_capture_records_runtime_inputs_and_git_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = capture_run_environment(cwd=REPO_ROOT, inputs={"case": "a" * 64, "dataset": None})
    assert env.runtime.python.count(".") == 2
    assert "pydantic" in env.runtime.dependencies
    assert env.source_control.available is True
    assert env.source_control.commit is not None
    assert len(env.source_control.commit) == 40
    assert env.inputs == {"case": "a" * 64, "dataset": None}
    assert "evidence, not a verdict" in env.notes[0]
    # Outside a repository capture degrades gracefully (GIT_DIR points at nothing).
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-such-git-dir"))
    outside = source_control(tmp_path)
    assert outside.available is False
    assert outside.commit is None


def test_hash_inputs_is_canonical() -> None:
    first = hash_inputs(case={"b": 1, "a": 2}, policy=None)
    second = hash_inputs(case={"a": 2, "b": 1}, policy=None)
    assert first == second
    assert len(first["case"]) == 64


def test_phase_order_and_scope_aware_next() -> None:
    assert PHASE_ORDER[0] is Phase.UNDERSTANDING
    assert phase_precedes(Phase.PREPARATION, Phase.MODELING)
    assert not phase_precedes(Phase.MODELING, Phase.PREPARATION)
    assert next_phase(Phase.UNDERSTANDING, requires_modeling=True) is Phase.PREPARATION
    assert next_phase(Phase.UNDERSTANDING, requires_modeling=False) is Phase.REPORTING
    assert next_phase(Phase.EVALUATION, requires_modeling=True) is Phase.REPORTING
    assert next_phase(Phase.REPORTING, requires_modeling=True) is None


def test_reopen_is_backward_only_budgeted_and_justified() -> None:
    ok, _ = can_reopen(
        Phase.EVALUATION, Phase.MODELING, reopen_count=0, max_reopens=2, justification="leak"
    )
    assert ok
    assert not can_reopen(
        Phase.PREPARATION, Phase.MODELING, reopen_count=0, max_reopens=2, justification="x"
    )[0]
    assert not can_reopen(
        Phase.MODELING, Phase.UNDERSTANDING, reopen_count=0, max_reopens=2, justification="x"
    )[0]
    assert not can_reopen(
        Phase.EVALUATION, Phase.MODELING, reopen_count=2, max_reopens=2, justification="x"
    )[0]
    assert not can_reopen(
        Phase.EVALUATION, Phase.MODELING, reopen_count=0, max_reopens=2, justification="  "
    )[0]


def test_session_service_creates_and_persists_a_session() -> None:
    repository = InMemorySessionRepository()
    service = SessionService(repository)
    project_id = new_id("project")

    session = service.create(project_id=project_id, client="cli")

    assert session.project_id == project_id
    assert session.client == "cli"
    assert session.run_ids == ()
    assert service.get(session.id) == session
    assert isinstance(repository, SessionRepository)


def test_session_service_creates_a_session_when_no_id_is_provided() -> None:
    service = SessionService(InMemorySessionRepository())

    session = service.resolve(project_id=new_id("project"), client="api")

    assert session.id.startswith("session_")
    assert session.client == "api"


def test_session_service_resolves_an_explicit_session_for_another_client() -> None:
    repository = InMemorySessionRepository()
    service = SessionService(repository)
    project_id = new_id("project")
    session = service.create(project_id=project_id, client="cli")

    resolved = service.resolve(project_id=project_id, client="web", session_id=session.id)

    assert resolved == session


def test_session_service_rejects_an_unknown_explicit_session() -> None:
    service = SessionService(InMemorySessionRepository())
    session_id = new_id("session")

    with pytest.raises(SessionNotFoundError, match="was not found"):
        service.resolve(project_id=new_id("project"), client="cli", session_id=session_id)


def test_session_service_rejects_a_session_from_another_project() -> None:
    service = SessionService(InMemorySessionRepository())
    session = service.create(project_id=new_id("project"), client="cli")

    with pytest.raises(SessionProjectMismatchError, match="belongs to project"):
        service.resolve(project_id=new_id("project"), client="cli", session_id=session.id)


def test_run_service_creates_and_associates_a_run(tmp_path: Path) -> None:
    service, sessions, events = _runtime_service(tmp_path)
    project_id = new_id("project")
    session = sessions.create(project_id=project_id, client="cli")

    run = service.create_run(
        session.id,
        "Inspect the dataset",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    assert run.project_id == project_id
    assert run.status is RunStatus.CREATED
    assert service.get(run.id) == run
    session = sessions.get(run.session_id)
    assert run.id in session.run_ids
    assert events.events(run.id)[0].type is EventType.RUN_STARTED


def test_run_service_reuses_an_explicit_session(tmp_path: Path) -> None:
    service, sessions, _ = _runtime_service(tmp_path)
    project_id = new_id("project")
    session = sessions.create(project_id=project_id, client="cli")

    run = service.create_run(
        session.id,
        "Profile the columns",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    assert run.session_id == session.id
    assert sessions.get(session.id).run_ids == (run.id,)


def test_run_service_rejects_an_unknown_run(tmp_path: Path) -> None:
    service, _, _ = _runtime_service(tmp_path)

    with pytest.raises(RunNotFoundError, match="was not found"):
        service.get(new_id("run"))


def test_run_service_applies_a_valid_transition(tmp_path: Path) -> None:
    service, sessions, _ = _runtime_service(tmp_path)
    session = sessions.create(project_id=new_id("project"), client="cli")
    run = service.create_run(
        session.id,
        "Train a baseline",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    updated = service.transition(run.id, RunStatus.PLANNING)

    assert updated.status is RunStatus.PLANNING
    assert updated.started_at is not None
    assert service.get(run.id) == updated


def test_run_service_rejects_an_invalid_transition(tmp_path: Path) -> None:
    service, sessions, _ = _runtime_service(tmp_path)
    session = sessions.create(project_id=new_id("project"), client="cli")
    run = service.create_run(
        session.id,
        "Train a baseline",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    with pytest.raises(ValueError, match="transition"):
        service.transition(run.id, RunStatus.COMPLETED)

    assert service.get(run.id).status is RunStatus.CREATED


def test_initial_graph_state_contains_runtime_run_context(tmp_path: Path) -> None:
    service, sessions, _ = _runtime_service(tmp_path)
    session = sessions.create(project_id=new_id("project"), client="cli")
    run = service.create_run(
        session.id,
        "Inspect the dataset",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    state: RunGraphState = initial_graph_state(run)

    assert state == {
        "run_id": run.id,
        "session_id": run.session_id,
        "project_id": run.project_id,
        "prompt": run.prompt,
        "status": RunStatus.CREATED,
    }


def test_run_graph_state_annotations_are_runtime_resolvable() -> None:
    hints = get_type_hints(RunGraphState)

    assert hints["status"] is RunStatus


def test_run_service_create_run_records_provenance_and_start_event(tmp_path: Path) -> None:
    """The event-backed API persists a run and its auditable start evidence."""
    runs = LocalRunStore(tmp_path / "runs")
    session_repository = LocalSessionRepository(tmp_path)
    sessions = SessionService(session_repository)
    service = RunService(
        runs,
        sessions,
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
    )
    session = sessions.create_session(project_id=new_id("project"), client="cli")

    run = service.create_run(
        session.id,
        "Inspect the dataset",
        actor=Actor.system(),
        workspace=REPO_ROOT,
    )

    event = runs.events(run.id)[0]
    assert run.status is RunStatus.CREATED
    assert event.type is EventType.RUN_STARTED
    assert {"run", "run_environment", "policy_sha256", "graph_definition_hash"} <= set(
        event.payload
    )
    assert event.payload["policy_sha256"] == "a" * 64
    assert event.payload["graph_definition_hash"] == "b" * 64
    assert run.id in sessions.get_session(session.id).run_ids


def test_run_service_complete_emits_terminal_event(tmp_path: Path) -> None:
    """Completion updates the Run and appends a matching lifecycle event."""
    service, sessions, events = _runtime_service(tmp_path)
    session = sessions.create_session(project_id=new_id("project"), client="cli")
    run = service.create_run(session.id, "Profile", actor=Actor.system(), workspace=REPO_ROOT)
    service.advance(run.id, RunStatus.PLANNING)
    service.advance(run.id, RunStatus.RUNNING)
    service.advance(run.id, RunStatus.AUDITING)

    completed = service.complete(run.id, actor=Actor.system())

    assert completed.status is RunStatus.COMPLETED
    assert events.events(run.id)[-1].type is EventType.RUN_COMPLETED


def test_project_resolver_loads_workspace_governance() -> None:
    """Project resolution exposes the configured project identity and frameworks."""
    resolution = ProjectResolver().resolve(REPO_ROOT / "examples/credit-risk")

    assert resolution.config.project.name == "credit-risk"
    assert resolution.project_id.startswith("project_")
    assert resolution.frameworks == (Framework.EU_AI_ACT, Framework.CREDIT_RISK)


def test_project_resolution_separates_the_config_directory_from_the_project_root() -> None:
    """``workspace`` is the ``.thymira`` directory; ``project_dir`` is the root that contains it.

    Everything that scopes real work -- ``load_project_context``, every ``ToolContext`` -- appends
    ``.thymira`` itself, so handing it ``workspace`` points it at ``.thymira/.thymira``: Inspect
    silently loads no project context and a delegated agent's file tools are sandboxed inside the
    config directory, unable to reach ``data/``. This pins the two apart.
    """
    project_root = REPO_ROOT / "examples/credit-risk"

    resolution = ProjectResolver().resolve(project_root)

    assert resolution.workspace == (project_root / ".thymira").resolve()
    assert resolution.project_dir == project_root.resolve()
    assert (resolution.project_dir / ".thymira" / "config.yaml").is_file()
    assert (resolution.project_dir / "data").is_dir()


def test_project_resolver_id_is_derived_from_a_posix_style_path(tmp_path: Path) -> None:
    """The project id must not depend on the OS-native path separator.

    A workspace path hashed with `str(Path(...))` yields backslash-joined segments on Windows
    and slash-joined ones on POSIX, so the same project would resolve to a different id per
    platform. This pins the identity to `.as_posix()` so it stays stable across both.
    """
    project_dir = tmp_path / "proj"
    thymira_dir = project_dir / ".thymira"
    thymira_dir.mkdir(parents=True)
    (thymira_dir / "config.yaml").write_text(
        "project:\n  name: proj\n  domain: test\n", encoding="utf-8"
    )

    resolution = ProjectResolver().resolve(project_dir)

    expected_identity = f"{thymira_dir.resolve().as_posix()}::proj"
    assert resolution.project_id == f"project_{sha256_text(expected_identity)[:32]}"


def test_run_event_log_appends_from_concurrent_threads_without_losing_a_writer(
    tmp_path: Path,
) -> None:
    """Every concurrent append lands: none is refused for a version it never had a stake in.

    ``ToolManager.execute`` appends ``tool.started`` and ``tool.completed`` around every call, and
    a THY turn that dispatches more than one tool call appends from more than one thread. Reading
    the version back and passing it made each append a read-then-write across the store's lock, so
    a loser got a stale-version ``ValueError`` out of the tool boundary -- failing the Run and, on
    the ``tool.completed`` side, leaving an unpairable ``tool.started`` behind (MIRA's A9). The
    version is now resolved inside the store's own lock, so the chain stays gapless *and* whole.
    """
    store = LocalRunStore(tmp_path / "runs")
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Inspect the credit-risk dataset.",
    )
    store.create(run, actor=Actor.system())
    log = RunEventLog(store, run.id)

    thread_count = 16
    barrier = threading.Barrier(thread_count)
    failures: list[BaseException] = []
    guard = threading.Lock()

    def _append(index: int) -> None:
        barrier.wait()
        try:
            log.append(EventType.TOOL_STARTED, Actor.system(), {"index": index})
        except Exception as exc:  # noqa: BLE001  # any escape here is the regression
            with guard:
                failures.append(exc)

    threads = [threading.Thread(target=_append, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    events = log.events()
    assert [event.seq for event in events] == list(range(len(events)))
    assert sum(1 for event in events if event.type is EventType.TOOL_STARTED) == thread_count
    assert verify_events(events).valid


def test_a_run_records_the_commit_its_workspace_was_on(tmp_path: Path) -> None:
    """`Run.git_commit` existed in the Contract and was never written by anything.

    The commit was captured as provenance and put in the creation event's payload only, so the
    field on the record itself stayed empty — and the trace, which reads the Run, had no version
    to report. Provenance is now captured before the Run is built rather than after.
    """
    service, sessions, _ = _runtime_service(tmp_path)
    session = sessions.create(project_id=new_id("project"), client="cli")

    run = service.create_run(
        session.id, "Inspect the dataset", actor=Actor.system(), workspace=REPO_ROOT
    )

    assert run.git_commit is not None
    assert len(run.git_commit) == 40


def test_a_run_outside_a_repository_simply_has_no_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance degrades gracefully, and an absent commit is not an error.

    Stubbed rather than pointed at a non-repository directory: `--basetemp` puts `tmp_path` inside
    this repository, and git's upward walk would legitimately find its commit from there.
    """
    service, sessions, _ = _runtime_service(tmp_path)
    session = sessions.create(project_id=new_id("project"), client="cli")
    captured = capture_run_environment(cwd=REPO_ROOT)
    monkeypatch.setattr(
        runs_module,
        "capture_run_environment",
        lambda **_: captured.model_copy(update={"source_control": SourceControl(available=False)}),
    )

    run = service.create_run(
        session.id, "Inspect the dataset", actor=Actor.system(), workspace=tmp_path
    )

    assert run.git_commit is None
