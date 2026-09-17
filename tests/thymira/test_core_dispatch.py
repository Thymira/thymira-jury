"""Execution dispatcher tests for RA-CORE-10."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from thymira.core import (
    ExecutionDispatcher,
    ExecutionDispatchError,
    InlineDispatcher,
    MiraControlPlane,
    RunController,
    RunNotResumableError,
    RunService,
    RuntimeState,
    RunTransitionKind,
    StateCheckpointer,
    Subgraph,
    SubgraphDeps,
    build_runtime_graph,
)
from thymira.core.control_plane import RunEventLog
from thymira.core.phases import Phase
from thymira.core.sessions import SessionService
from thymira.core.usage import UsageLedger
from thymira.events import sha256_text, verify_events
from thymira.policies import Gate, PolicyEngine, load_default_policy
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    EventType,
    Run,
    RunStatus,
    new_id,
)
from thymira.state import (
    LocalArtifactStore,
    LocalCheckpointRepository,
    LocalRecordRepository,
    LocalRunStore,
    LocalSessionRepository,
)
from thymira.tools import ToolManager, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.core import GraphFactory


class _RecordingDispatcher:
    """Small fake proving RunService receives, rather than constructs, its dispatcher."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.resumed: list[str] = []

    def submit(self, run_id: str) -> None:
        """Record the submitted Run id."""
        self.submitted.append(run_id)

    def resume(self, run_id: str) -> None:
        """Record the resumed Run id."""
        self.resumed.append(run_id)


class _TerminalFailingDispatcher:
    """Simulate a late dispatch error after the Run has completed."""

    def __init__(self, controller: RunController) -> None:
        self._controller = controller

    def submit(self, run_id: str) -> None:
        """Complete the Run, then raise the late dispatcher error."""
        for command in (
            RunTransitionKind.START,
            RunTransitionKind.BEGIN_EXECUTION,
            RunTransitionKind.BEGIN_AUDIT,
            RunTransitionKind.BEGIN_REPORTING,
            RunTransitionKind.COMPLETE,
        ):
            self._controller.advance(run_id, command)
        raise ExecutionDispatchError(f"late failure for {run_id}")

    def resume(self, run_id: str) -> None:
        """Reject unsupported resume calls in this purpose-built fake."""
        raise AssertionError(f"unexpected resume for {run_id}")


class _RecordingGraph:
    """A compiled-graph double that records where the dispatcher resumes it."""

    def __init__(self) -> None:
        self.resumed_as: list[str] = []

    def get_state(self, config: object) -> SimpleNamespace:
        """Return a resumable snapshot that has already reached a stop."""
        del config
        return SimpleNamespace(values={"run_id": "x"}, next=())

    def update_state(self, config: object, values: object, as_node: str) -> None:
        """Record the node the dispatcher re-entered the graph as."""
        del config, values
        self.resumed_as.append(as_node)

    def invoke(self, state: object, config: object, durability: str) -> dict[str, object]:
        """Accept the dispatcher's invocation without executing anything."""
        del state, config, durability
        return {}


class _ThySubgraph:
    """Scripted THY graph for the inline execution test."""

    name = "test-thy"

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "test-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Advance the shared state without accessing private dependencies."""
        del deps
        return state.model_copy(update={"phase": Phase.PREPARATION})


class _MiraSubgraph:
    """Scripted MIRA graph that emits the expected audit completion fact."""

    name = "test-mira"

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "test-mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Record audit completion and return the unchanged shared state."""
        prior_events = deps.event_log.events()
        prior_hash = prior_events[-1].hash if prior_events else None
        revision = sum(event.type is EventType.AUDIT_COMPLETED for event in prior_events) + 1
        deps.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "status": "passed",
                "audit_revision": revision,
                "audit_report": {
                    "run_id": state.run_id,
                    "status": "passed",
                    "controls": [],
                    "findings": [],
                    "terminal_hash": prior_hash,
                    "policy_sha256": deps.gate.engine.policy_sha256,
                },
            },
            subject_id=state.run_id,
            producer="test.mira",
        )
        return state


class _CrashingThySubgraph:
    """A THY double whose defect raises an exception type the boundary once let escape."""

    name = "crashing-thy"

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "crashing-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Fail the way an ordinary programming defect does, not with a structural error."""
        del state, deps
        raise AttributeError("'NoneType' object has no attribute 'plan'")


def _service(
    tmp_path: Path,
    *,
    dispatcher: ExecutionDispatcher | None = None,
) -> tuple[RunService, SessionService, LocalRunStore]:
    """Build a RunService with the local repositories used by this test module."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))
    service = RunService(
        store,
        sessions,
        dispatcher=dispatcher,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    return service, sessions, store


def _inline_dispatcher(
    tmp_path: Path, store: LocalRunStore, *, thy: Subgraph | None = None
) -> InlineDispatcher:
    """Build a per-Run graph factory with all runtime dependencies explicitly injected."""

    def graph_factory(run):
        event_log = RunEventLog(store, run.id)
        deps = SubgraphDeps(
            event_log=event_log,
            gate=Gate(PolicyEngine(load_default_policy()), event_log),
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
            tool_manager=ToolManager(ToolRegistry()),
            record_repository=LocalRecordRepository(tmp_path / "records"),
            usage_ledger=UsageLedger(),
        )
        return build_runtime_graph(
            thy if thy is not None else _ThySubgraph(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=RunController(store),
        )

    return InlineDispatcher(store, graph_factory)


def _pause(store: LocalRunStore, run_id: str) -> None:
    """Pause a started Run through the one public path that can reach a resumable condition.

    ``RunController.advance`` accepts only ordinary progress commands, and ``PAUSE`` is not one of
    them, so the bounded ``pause_run`` control-plane action is how a test reaches a state a
    ``RESUME`` is legal from.
    """
    MiraControlPlane(store, PolicyEngine(load_default_policy())).handle(
        ActionIntent(
            id=new_id("intent"),
            run_id=run_id,
            requester=Actor(kind=ActorKind.AGENT, id="mira", authenticated=True),
            action_kind=ActionKind.PAUSE_RUN,
            subject_kind="run",
            subject_id=run_id,
            purpose="Pause the Run so the recorded resume names its cause.",
            idempotency_key=new_id("intent"),
        )
    )


def test_run_service_submits_to_injected_dispatcher(tmp_path: Path) -> None:
    """RunService delegates submission and does not construct a dispatcher itself."""
    dispatcher = _RecordingDispatcher()
    service, sessions, _ = _service(tmp_path, dispatcher=dispatcher)
    session = sessions.create(project_id=new_id("project"), client="test")

    run = service.create_run(
        session.id,
        "Profile the dataset",
        actor=Actor.system(),
        workspace=tmp_path,
    )

    assert dispatcher.submitted == [run.id]


def test_run_service_keeps_a_completed_run_when_dispatch_fails_late(tmp_path: Path) -> None:
    """A late dispatch error must not overwrite an already terminal Run."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))
    service = RunService(
        store,
        sessions,
        dispatcher=_TerminalFailingDispatcher(RunController(store)),
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create(project_id=new_id("project"), client="test")

    created = service.create_run(
        session.id,
        "Keep terminal state",
        actor=Actor.system(),
        workspace=tmp_path,
    )

    assert created.status is RunStatus.CREATED
    assert service.get_run(created.id).status is RunStatus.COMPLETED
    assert not any(event.type is EventType.RUN_FAILED for event in store.events(created.id))


def test_run_service_resumes_through_injected_dispatcher(tmp_path: Path) -> None:
    """RunService delegates checkpoint continuation without owning graph execution."""
    dispatcher = _RecordingDispatcher()
    service, sessions, store = _service(tmp_path, dispatcher=dispatcher)
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(
        session.id,
        "Resume a checkpoint",
        actor=Actor.system(),
        workspace=tmp_path,
    )
    RunController(store).advance(run.id, RunTransitionKind.START)
    _pause(store, run.id)

    resumed = service.resume(run.id, actor=Actor.system())

    assert resumed.id == run.id
    assert dispatcher.resumed == [run.id]


def test_run_service_refuses_to_resume_an_active_run(tmp_path: Path) -> None:
    """A second dispatcher cannot be started while the first still owns an active Run."""
    dispatcher = _RecordingDispatcher()
    service, sessions, store = _service(tmp_path, dispatcher=dispatcher)
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(
        session.id,
        "Already executing",
        actor=Actor.system(),
        workspace=tmp_path,
    )
    RunController(store).advance(run.id, RunTransitionKind.START)
    events_before = store.events(run.id)

    with pytest.raises(RunNotResumableError, match="already active"):
        service.resume(run.id, actor=Actor.system())

    assert dispatcher.resumed == []
    assert store.events(run.id) == events_before


def test_resume_rejects_a_tampered_event_log_before_dispatch(tmp_path: Path) -> None:
    """Resume verifies the authoritative log before writing or dispatching anything."""
    dispatcher = _RecordingDispatcher()
    service, sessions, _ = _service(tmp_path, dispatcher=dispatcher)
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(
        session.id,
        "Resume safely",
        actor=Actor.system(),
        workspace=tmp_path,
    )
    event_path = tmp_path / "runs" / run.id / "events.jsonl"
    original = event_path.read_text(encoding="utf-8")
    event_path.write_text(original.replace("Resume safely", "Tampered prompt"), encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        service.resume(run.id, actor=Actor.system())

    assert dispatcher.resumed == []
    # Creation now includes the immutable Run-bound model-route snapshot before dispatch.
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 2
    assert run.status is RunStatus.CREATED


def test_resume_of_terminal_run_is_an_idempotent_noop(tmp_path: Path) -> None:
    """A terminal Run is returned unchanged and never submitted again."""
    dispatcher = _RecordingDispatcher()
    service, sessions, store = _service(tmp_path, dispatcher=dispatcher)
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(
        session.id,
        "Already complete",
        actor=Actor.system(),
        workspace=tmp_path,
    )
    controller = RunController(store)
    for command in (
        RunTransitionKind.START,
        RunTransitionKind.BEGIN_EXECUTION,
        RunTransitionKind.BEGIN_AUDIT,
        RunTransitionKind.BEGIN_REPORTING,
        RunTransitionKind.COMPLETE,
    ):
        controller.advance(run.id, command)

    resumed = service.resume(run.id, actor=Actor.system())

    assert resumed.status is RunStatus.COMPLETED
    assert dispatcher.resumed == []


def test_inline_dispatcher_completes_and_persists_run_events(tmp_path: Path) -> None:
    """An inline submission drives the composition graph to a terminal, verified Run."""
    service, sessions, store = _service(tmp_path)
    dispatcher = _inline_dispatcher(tmp_path, store)
    service = RunService(
        store,
        sessions,
        dispatcher=dispatcher,
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create(project_id=new_id("project"), client="test")

    created = service.create_run(
        session.id,
        "Profile the dataset",
        actor=Actor.system(),
        workspace=tmp_path,
        background=True,
    )

    assert created.status is RunStatus.CREATED
    completed = service.get_run(created.id)
    assert completed.status is RunStatus.COMPLETED
    events = store.events(created.id)
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert verify_events(events).valid


def test_a_thy_defect_of_any_exception_type_becomes_a_recorded_failed_run(tmp_path: Path) -> None:
    """Any crash inside the graph is a recorded terminal failure, never a Run with no end.

    The boundary used to catch only ``(OSError, RuntimeError, ValueError)``, so an ordinary
    programming defect -- ``AttributeError``, ``KeyError``, ``TypeError`` -- escaped
    ``ExecutionDispatchError``, ``RunService`` never recorded ``run.failed``, and the Run stayed
    RUNNING forever with no terminal event on its append-only log.
    """
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))
    service = RunService(
        store,
        sessions,
        dispatcher=_inline_dispatcher(tmp_path, store, thy=_CrashingThySubgraph()),
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create(project_id=new_id("project"), client="test")

    created = service.create_run(
        session.id, "Crash inside THY", actor=Actor.system(), workspace=tmp_path
    )

    assert service.get_run(created.id).status is RunStatus.FAILED
    events = store.events(created.id)
    failed = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert "'NoneType' object has no attribute 'plan'" in failed.payload["error"]
    assert verify_events(events).valid


def test_a_thy_defect_raised_on_a_resume_still_ends_the_run(tmp_path: Path) -> None:
    """``resume`` catches exactly what ``submit`` catches, so the human's answer is not spent."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))
    service = RunService(
        store,
        sessions,
        dispatcher=_RecordingDispatcher(),
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create(project_id=new_id("project"), client="test")
    created = service.create_run(
        session.id, "Resume into a defect", actor=Actor.system(), workspace=tmp_path
    )

    def _crashing_factory(run: Run) -> NoReturn:
        del run
        raise KeyError("thy_progress")

    with pytest.raises(ExecutionDispatchError, match="inline resume failed"):
        InlineDispatcher(store, _crashing_factory).resume(created.id)


def test_inline_dispatcher_resume_wraps_a_failure_like_submit(tmp_path: Path) -> None:
    """resume() must be as safe as submit() (bug-hunt: it previously had no safety net at all).

    A resume-time failure -- a bad checkpoint, a ``ThyExecutionError`` from the composed graph, any
    other structural error -- must become ``ExecutionDispatchError`` here, the same as ``submit``,
    so ``RunService`` can record a terminal ``RUN_FAILED`` instead of the Run being left with no
    terminal event at all.
    """
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))
    service = RunService(
        store,
        sessions,
        dispatcher=_RecordingDispatcher(),
        policy_sha256=sha256_text("policy"),
        graph_definition_hash=sha256_text("graph"),
    )
    session = sessions.create(project_id=new_id("project"), client="test")
    created = service.create_run(
        session.id, "Resume then fail", actor=Actor.system(), workspace=tmp_path
    )

    def _failing_factory(run: Run) -> NoReturn:
        del run
        raise RuntimeError("graph rebuild failed")

    dispatcher = InlineDispatcher(store, _failing_factory)

    with pytest.raises(ExecutionDispatchError, match="inline resume failed: graph rebuild failed"):
        dispatcher.resume(created.id)


@pytest.mark.parametrize(
    ("resume_from", "node"),
    [
        ("information", "start"),
        # An approved question-limit review re-enters the interview body, not the Gate: the Run
        # never reached a policy decision the Gate's outgoing edge could route on.
        ("interview_limit", "start"),
        ("approval", "gate"),
        ("execution", "execution_gate"),
        (None, "gate"),
    ],
)
def test_inline_dispatcher_resumes_at_the_node_the_transition_names(
    tmp_path: Path, resume_from: str | None, node: str
) -> None:
    """The recorded resume cause selects the node, so a park inside THY re-enters before THY."""
    service, sessions, store = _service(tmp_path, dispatcher=_RecordingDispatcher())
    session = sessions.create(project_id=new_id("project"), client="test")
    run = service.create_run(session.id, "Resume", actor=Actor.system(), workspace=tmp_path)
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    _pause(store, run.id)
    payload = {} if resume_from is None else {"resume_from": resume_from}
    controller.advance(run.id, RunTransitionKind.RESUME, payload=payload)
    graph = _RecordingGraph()
    # The double implements only the three methods the dispatcher calls, not the whole
    # ``CompiledStateGraph`` surface the factory type names.
    factory = cast("GraphFactory", lambda _run: graph)

    InlineDispatcher(store, factory).resume(run.id)

    assert graph.resumed_as == [node]
