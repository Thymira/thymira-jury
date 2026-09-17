"""Production adapter from durable work notifications to the fenced owner loop.

The transport carries only the two identifiers in :class:`~thymira.schemas.WorkNotification`.
Every state lookup, claim, owner acquisition and settlement happens through the injected
``LifecycleRepository`` and :class:`~thymira.core.lifecycle_worker.LifecycleOwnerLoop`; no broker
message is trusted as a state or authority snapshot.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from thymira.agents.settlement import finalize_parked_invocation
from thymira.core.control_plane import RunController, RunEventLog
from thymira.core.graph.state import initial_runtime_state
from thymira.core.lifecycle_worker import (
    ControlInputConsumer,
    GoalBoardCommandConsumer,
    LifecycleOwnerLoop,
    TerminalAuditor,
    WorkExecutor,
)
from thymira.core.run_state import RunTransitionKind
from thymira.schemas import (
    Actor,
    EventType,
    ExecutionOutcomeKind,
    FailureCause,
    StopReason,
    WorkItem,
    WorkNotification,
    WorkResult,
)
from thymira.state import LifecycleError, RunHandle

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from langchain_core.runnables import RunnableConfig

    from thymira.core.dispatch import GraphFactory
    from thymira.state import LifecycleRepository, LocalRunStore


class LiveOwnerContext:
    """Expose only the currently bound process-owned Run handle to graph factories.

    A graph factory is called with a serialisable ``Run`` for ordinary read composition.  Worker
    execution binds this context for the duration of one claimed work item, letting artifact,
    checkpoint, event-log and controller factories resolve the same live owner.  A factory that
    runs outside the owner loop, or asks for another Run, fails closed.
    """

    def __init__(self) -> None:
        self._current: ContextVar[RunHandle | None] = ContextVar(
            "thymira_lifecycle_owner", default=None
        )

    @contextmanager
    def bind(self, owner: RunHandle) -> Iterator[None]:
        """Bind one live owner for a single synchronous graph execution."""
        owner.assert_live()
        token: Token[RunHandle | None] = self._current.set(owner)
        try:
            yield
        finally:
            self._current.reset(token)

    def for_run(self, run_id: str) -> RunHandle:
        """Return the live bound owner for ``run_id`` or raise a closed-boundary error."""
        owner = self._current.get()
        if owner is None or owner.run_id != run_id:
            raise LifecycleError(f"no live owner is bound for Run {run_id}")
        owner.assert_live()
        return owner

    def current(self) -> RunHandle:
        """Return the currently bound live owner for owner-bound callbacks."""
        owner = self._current.get()
        if owner is None:
            raise LifecycleError("no live owner is bound for the lifecycle callback")
        owner.assert_live()
        return owner


class LifecycleGraphExecutor:
    """Invoke the composed graph from the owner loop and return a durable typed outcome."""

    def __init__(
        self,
        repository: LifecycleRepository,
        run_store: LocalRunStore,
        graph_factory: GraphFactory,
        owner_context: LiveOwnerContext,
    ) -> None:
        self._repository = repository
        self._run_store = run_store
        self._graph_factory = graph_factory
        self._owner_context = owner_context

    def execute(self, work: WorkItem, live_owner: RunHandle) -> WorkResult:
        """Execute one claimed work item while every graph write sees the live owner."""
        live_owner.assert_live()
        run = self._repository.get_run(work.run_id)
        if work.kind not in {"run.execute", "control.apply"}:
            raise LifecycleError(f"unsupported lifecycle work kind {work.kind!r}")
        try:
            with self._owner_context.bind(live_owner):
                graph = self._graph_factory(run)
                config = cast("RunnableConfig", {"configurable": {"thread_id": run.id}})
                if work.kind == "run.execute":
                    graph.invoke(initial_runtime_state(run), config, durability="sync")
                elif work.kind == "control.apply":
                    graph.invoke(None, config, durability="sync")
        except Exception as exc:  # noqa: BLE001  # graph boundary records every ordinary failure
            cause = FailureCause(
                code="graph_execution_failed",
                phase="worker.execute",
                exception_type=type(exc).__name__,
                message=str(exc) or type(exc).__name__,
                effects_may_have_occurred=True,
            )
            self._record_failure(live_owner, cause)
            return WorkResult(kind=ExecutionOutcomeKind.FAILED, cause=cause)
        return WorkResult(
            kind=ExecutionOutcomeKind.SUCCEEDED,
            payload={"run_id": run.id, "work_id": work.work_id},
        )

    def _record_failure(self, owner: RunHandle, cause: FailureCause) -> None:
        """Record a failed Run through the owner-bound transition controller."""
        controller = RunController(self._run_store, writer=owner.append_transition)
        if controller.current_state(owner.run_id).condition.value == "terminal":
            return
        finalize_parked_invocation(
            RunEventLog(self._run_store, owner.run_id, writer=owner.append),
            actor=Actor.system(),
            stop_reason=StopReason.ABNORMAL,
            diagnostics=cause.message,
        )
        controller.advance(
            owner.run_id,
            RunTransitionKind.FAIL,
            payload={"error": cause.message, "cause": cause.to_json_dict()},
        )
        owner.append(
            EventType.RUN_FAILED,
            Actor.system(),
            {"error": cause.message, "cause": cause.to_json_dict()},
            expected_version=self._repository.version(owner.run_id),
        )


@runtime_checkable
class LifecycleNotificationConsumer(Protocol):
    """Consume strict two-field work notifications with transport acknowledgements."""

    def consume(self, handler: Callable[[WorkNotification], None]) -> None:
        """Deliver notifications until the transport closes or the process stops."""
        ...


class LifecycleRunWorker:
    """Drive durable notifications through one shared owner-loop implementation."""

    def __init__(
        self,
        consumer: LifecycleNotificationConsumer,
        owner_loop: LifecycleOwnerLoop,
        executor: WorkExecutor | Callable[[WorkItem, RunHandle], WorkResult],
    ) -> None:
        self._consumer = consumer
        self._owner_loop = owner_loop
        self._executor = executor

    def run(self) -> None:
        """Block while the notification transport delivers work."""
        self._consumer.consume(self._deliver)

    def _deliver(self, notification: WorkNotification) -> None:
        """Adapt the result-bearing handler to a transport callback acknowledged by return."""
        self.handle(notification)

    def handle(self, notification: WorkNotification) -> WorkResult | None:
        """Claim and execute one notification, returning ``None`` for duplicate or busy delivery."""
        return self._owner_loop.process(
            notification.work_id,
            notification.run_id,
            self._executor,
        )


def build_lifecycle_worker(
    consumer: LifecycleNotificationConsumer,
    repository: LifecycleRepository,
    worker_id: str,
    executor: WorkExecutor | Callable[[WorkItem, RunHandle], WorkResult],
    *,
    goal_board_consumer: GoalBoardCommandConsumer | None = None,
    control_consumer: ControlInputConsumer | None = None,
    terminal_auditor: TerminalAuditor | None = None,
    owner_loop: LifecycleOwnerLoop | None = None,
) -> LifecycleRunWorker:
    """Compose the production worker from one repository, owner loop and executor.

    ``executor`` receives the durable :class:`~thymira.schemas.WorkItem` and live
    :class:`~thymira.state.RunHandle` when it is called through the loop.  The adapter does not
    expose or accept serialisable owner fields, so a notification cannot bypass the live lock and
    epoch checks.
    """
    resolved_owner_loop = owner_loop or LifecycleOwnerLoop(
        repository,
        worker_id,
        goal_board_consumer=goal_board_consumer,
        control_consumer=control_consumer,
        terminal_auditor=terminal_auditor,
    )
    return LifecycleRunWorker(
        consumer,
        resolved_owner_loop,
        executor,
    )


__all__ = [
    "LifecycleGraphExecutor",
    "LifecycleNotificationConsumer",
    "LifecycleRunWorker",
    "LiveOwnerContext",
    "build_lifecycle_worker",
]
