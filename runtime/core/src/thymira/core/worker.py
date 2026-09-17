"""Production lifecycle worker composition.

The worker consumes only the strict ``{work_id, run_id}`` notification. It resolves all other
state from the local lifecycle repository, claims the durable item, acquires the Run's process
owned lock and invokes the graph through owner checked event, artifact and checkpoint facades.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from thymira.core.control_plane import RunController
from thymira.core.graph.adapters import build_runtime_graph_factory
from thymira.core.lifecycle_adapter import (
    LifecycleGraphExecutor,
    LifecycleRunWorker,
    LiveOwnerContext,
    build_lifecycle_worker,
)
from thymira.core.lifecycle_worker import BoundedControlInputConsumer, LifecycleOwnerLoop
from thymira.core.rabbitmq import RabbitMqConsumer, RabbitMqSettings
from thymira.core.run_state import RunTransitionKind
from thymira.schemas import (
    ControlInput,
    ControlInputKind,
    EventSurface,
    EventType,
    JsonObject,
    ModelRoutePolicy,
)
from thymira.state import (
    ArtifactStore,
    LifecycleBackendUnavailableError,
    LifecycleRepository,
    LocalArtifactStore,
    LocalBoardRepository,
    LocalCheckpointRepository,
    LocalDagRepository,
    LocalLifecycleRepository,
    LocalRecordRepository,
    LocalRunStore,
    LocalSessionRepository,
    RunHandle,
)
from thymira.tools import ToolManager
from thymira.tools.builtins import configured_builtins_registry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.core.dispatch import GraphFactory
    from thymira.core.lifecycle_adapter import LifecycleNotificationConsumer
    from thymira.policies import Gate, PolicyEngine
    from thymira.schemas import Run
    from thymira.tools import ToolRegistry


def resolve_run_model_route_policy(
    run: Run,
    current_policy: ModelRoutePolicy,
    *,
    session_present: bool = True,
) -> ModelRoutePolicy:
    """Resolve a worker's effective policy from the durable Run identity.

    A mutable Session row cannot replace the policy captured in the Run creation event. The worker
    checks Session existence only as a liveness prerequisite: a missing Session returns an
    unavailable empty policy, so recovery infrastructure cannot dispatch a detached Run. A later
    composition policy can only narrow the bound identity.
    """
    if not session_present:
        return ModelRoutePolicy.unavailable()
    return run.model_route_policy.narrowed_by(current_policy)


class _OwnerControlConsumer:
    """Apply authenticated non-board controls in the one canonical owner loop.

    Transition controls use ``RunController`` with the live owner writer. Content controls are
    recorded as owner-authored ``agent.message`` facts for the graph's next turn; the signed
    authority envelope is deliberately excluded from that evidence. PLAN and DELEGATE remain a
    separate goal-board hook, so this consumer cannot accidentally consume them as generic text.
    """

    _TRANSITIONS: ClassVar[dict[ControlInputKind, RunTransitionKind]] = {
        ControlInputKind.RESUME: RunTransitionKind.RESUME,
        ControlInputKind.CANCEL: RunTransitionKind.CANCEL,
        ControlInputKind.HOST_PAUSE: RunTransitionKind.PAUSE,
    }

    def __init__(self, run_store: LocalRunStore, repository: LifecycleRepository) -> None:
        self._run_store = run_store
        self._repository = repository

    def apply_control(self, command: ControlInput, live_owner: RunHandle) -> JsonObject:
        """Apply one closed non-board control under the live Run owner."""
        live_owner.assert_live()
        if command.kind in {ControlInputKind.PLAN, ControlInputKind.DELEGATE}:
            raise ValueError("goal-board controls require the goal-board consumer")
        transition = self._TRANSITIONS.get(command.kind)
        if transition is not None:
            controller = RunController(self._run_store, writer=live_owner.append_transition)
            payload = {
                "control_input_id": command.input_id,
                "control_kind": command.kind.value,
                "control_payload": command.payload,
                "initiator": command.authority.actor.to_json_dict(),
            }
            if transition is RunTransitionKind.PAUSE:
                controller.pause_owned(live_owner.run_id, payload=payload)
            else:
                controller.advance(live_owner.run_id, transition, payload=payload)
        else:
            live_owner.append(
                EventType.AGENT_MESSAGE,
                command.authority.actor,
                {
                    "control_input_id": command.input_id,
                    "control_kind": command.kind.value,
                    "control_payload": command.payload,
                },
                surface=EventSurface.MODEL_VISIBLE,
                subject_id=live_owner.run_id,
                expected_version=self._repository.version(live_owner.run_id),
                producer="thymira.core.lifecycle",
                producer_version="0.1",
            )
        return {
            "input_id": command.input_id,
            "kind": command.kind.value,
            "run_id": live_owner.run_id,
        }


def _require_local_lifecycle_backend() -> None:
    """Reject distributed lifecycle configuration until the PostgreSQL owner is composed."""
    backend = os.environ.get("THYMIRA_LIFECYCLE_BACKEND", "local").strip().lower()
    if backend != "local":
        raise LifecycleBackendUnavailableError(
            "the worker requires THYMIRA_LIFECYCLE_BACKEND=local until the PostgreSQL "
            "advisory-lock lifecycle repository is composed; refusing an unsafe mixed backend"
        )
    if any(key.startswith("THYMIRA_POSTGRES_") for key in os.environ):
        raise LifecycleBackendUnavailableError(
            "PostgreSQL settings are present but no PostgreSQL lifecycle owner is composed; "
            "refusing mixed local ownership and PostgreSQL checkpoints"
        )


def build_production_lifecycle_worker(
    root: Path,
    consumer: LifecycleNotificationConsumer,
    *,
    worker_id: str,
    graph_factory: GraphFactory | None = None,
) -> LifecycleRunWorker:
    """Compose the shipped worker with one local lifecycle repository and owner-bound graph."""
    _require_local_lifecycle_backend()
    root = Path(root).resolve()
    run_store = LocalRunStore(root / "runs")
    repository_root = root / "repositories"
    session_repository = LocalSessionRepository(repository_root)
    lifecycle_repository = LocalLifecycleRepository(
        root,
        run_store=run_store,
        session_repository=session_repository,
    )
    owner_context = LiveOwnerContext()
    control_consumer = _OwnerControlConsumer(run_store, lifecycle_repository)
    # The graph factory carries the terminal auditor the owner loop needs, and its model-step
    # hook must reach that same loop.  Compose the factory first and resolve the loop through
    # ``owner_loops`` so neither wiring direction is lost.
    owner_loops: list[LifecycleOwnerLoop] = []

    def intercept_next_model_step() -> tuple[str, ...]:
        """Claim live next-step controls through the owner loop composed below."""
        if not owner_loops:
            return ()
        return owner_loops[0].intercept_next_model_step(owner_context.current())

    if graph_factory is None:
        graph_factory = _build_graph_factory(
            root,
            run_store,
            owner_context,
            before_model_request=intercept_next_model_step,
        )
    terminal_auditor = getattr(graph_factory, "audit_terminal", None)
    if terminal_auditor is not None and not callable(terminal_auditor):
        raise TypeError("the lifecycle graph factory exposes a non-callable terminal auditor")
    owner_loop = LifecycleOwnerLoop(
        lifecycle_repository,
        worker_id,
        control_consumer=BoundedControlInputConsumer(
            {
                kind: control_consumer.apply_control
                for kind in ControlInputKind
                if kind not in {ControlInputKind.PLAN, ControlInputKind.DELEGATE}
            }
        ),
        terminal_auditor=terminal_auditor,
    )
    owner_loops.append(owner_loop)
    executor = LifecycleGraphExecutor(
        lifecycle_repository,
        run_store,
        graph_factory,
        owner_context,
    )
    return build_lifecycle_worker(
        consumer,
        lifecycle_repository,
        worker_id,
        executor,
        owner_loop=owner_loop,
    )


def _build_graph_factory(
    root: Path,
    run_store: LocalRunStore,
    owner_context: LiveOwnerContext,
    *,
    before_model_request: Callable[[], Sequence[str]] | None = None,
) -> GraphFactory:
    """Build the production graph with every per-Run writer resolved from the live owner."""
    from thymira.core.control_plane import RunEventLog  # noqa: PLC0415  # lazy worker wiring
    from thymira.mira.kb import load_default_regulation_store  # noqa: PLC0415  # lazy worker wiring
    from thymira.mira.tools import (  # noqa: PLC0415  # lazy worker wiring
        build_mira_tool_registry,
    )
    from thymira.policies import (  # noqa: PLC0415  # lazy worker wiring
        Gate,
        PolicyEngine,
        load_default_policy,
    )

    policy_engine: PolicyEngine = PolicyEngine(load_default_policy())
    tool_registry: ToolRegistry = configured_builtins_registry()
    tool_manager = ToolManager(tool_registry)
    mira_tool_registry = build_mira_tool_registry(load_default_regulation_store())
    record_repository = LocalRecordRepository(root / "repositories")
    checkpoint_repository = LocalCheckpointRepository(root / "repositories")
    session_repository = LocalSessionRepository(root / "repositories")
    plan_repository = LocalBoardRepository(root / "boards")
    dag_repository = LocalDagRepository(root / "dag")
    # The worker's current policy can revoke a Run route, but it is never allowed to widen the
    # immutable snapshot persisted in the Run creation event.
    current_route_policy = ModelRoutePolicy.from_environment()

    def gate_factory(run_id: str) -> Gate:
        owner = owner_context.for_run(run_id)
        return Gate(policy_engine, RunEventLog(run_store, run_id, writer=owner.append))

    def artifact_store_factory(run_id: str) -> ArtifactStore:
        return LocalArtifactStore(root / "artifacts" / run_id, run_id)

    def route_policy_for(run: Run) -> ModelRoutePolicy:
        return resolve_run_model_route_policy(
            run,
            current_route_policy,
            session_present=session_repository.get(run.session_id) is not None,
        )

    # ``owner_context`` is shared with the executor through the factory closure below. The
    # executor binds it before calling this graph factory, so this provider cannot be called by a
    # request thread or by a graph built for another Run.
    return build_runtime_graph_factory(
        run_store,
        gate_factory=gate_factory,
        artifact_store_factory=artifact_store_factory,
        tool_manager=tool_manager,
        tool_registry=tool_registry,
        mira_tool_registry=mira_tool_registry,
        record_repository=record_repository,
        checkpoint_repository=checkpoint_repository,
        plan_repository=plan_repository,
        dag_repository=dag_repository,
        owner_provider=owner_context.for_run,
        owner_binder=owner_context.bind,
        before_model_request=before_model_request,
        route_policy=route_policy_for,
    )


def run_worker_main() -> None:  # pragma: no cover - exercised as a real subprocess
    """Run the production lifecycle worker from environment configuration."""
    root = Path(os.environ.get("THYMIRA_RUN_ROOT", "runs")).resolve()
    worker_id = os.environ.get("THYMIRA_WORKER_ID", f"worker-{os.getpid()}")
    consumer = RabbitMqConsumer(RabbitMqSettings.from_env())
    worker = build_production_lifecycle_worker(root, consumer, worker_id=worker_id)
    worker.run()


if __name__ == "__main__":  # pragma: no cover
    run_worker_main()


__all__ = [
    "build_production_lifecycle_worker",
    "resolve_run_model_route_policy",
    "run_worker_main",
]
