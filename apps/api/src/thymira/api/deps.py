"""Dependency composition for the Thymira HTTP boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 - FastAPI inspects dependency annotations at runtime.

from thymira.agents.llm.routing import MODEL_OVERRIDES_NAMESPACE, set_model_overrides
from thymira.api.lifecycle import AuthorityProofBuilder
from thymira.api.subscription import EventSubscription, PollingEventSubscription
from thymira.core import (
    AuditFreshnessService,
    ExecutionDispatcher,
    GoalBoardService,
    GraphFactory,
    IdempotencyJournal,
    InlineDispatcher,
    MiraControlPlane,
    ProjectResolution,
    ProjectResolver,
    RiskInterviewModelContext,
    RiskInterviewService,
    RunEventLog,
    RunService,
    SessionService,
    UsageLedgerRegistry,
    build_risk_interview_model_context,
    build_runtime_graph_factory,
    composed_graph_definition_hash,
)
from thymira.mira import canonical_graph_definition_hash
from thymira.mira.kb import load_default_regulation_store
from thymira.mira.tools import build_mira_tool_registry
from thymira.policies import (
    Approver,
    Gate,
    Policy,
    PolicyEngine,
    load_default_policy,
    load_policy,
    load_policy_stack,
)
from thymira.schemas import Actor, Id, ModelRoutePolicy, Run
from thymira.state import (
    ArtifactStore,
    BoardRepository,
    CheckpointRepository,
    DagRepository,
    EventStore,
    HmacAuthorityVerifier,
    LocalArtifactStore,
    LocalBoardRepository,
    LocalCheckpointRepository,
    LocalDagRepository,
    LocalLifecycleRepository,
    LocalRecordRepository,
    LocalRunRepository,
    LocalRunStore,
    LocalSessionRepository,
    LocalSettingsStore,
    LocalWorkspaceRegistry,
    RecordRepository,
    RunRepository,
    SessionRepository,
    resolve_authority_secret,
)
from thymira.tools import ToolManager, ToolRegistry
from thymira.tools.builtins import configured_builtins_registry

if TYPE_CHECKING:
    from thymira.agents.llm.base import LLMProvider
    from thymira.api.permissions import PrincipalResolver
    from thymira.events import EventLog
    from thymira.mira.kb import RegulationStore
    from thymira.schemas import ControlInput, Event

GateFactory = Callable[..., Gate]
ArtifactStoreFactory = Callable[[Id], ArtifactStore]
ActorFactory = Callable[[], Actor]


class _RunStoreEventStore:
    """Expose the authoritative LocalRunStore history through the EventStore protocol."""

    def __init__(self, store: LocalRunStore) -> None:
        self._store = store

    def open(self, run_id: Id) -> EventLog:
        """Open the event log owned by ``run_id``'s LocalRunStore directory."""
        return RunEventLog(self._store, run_id)

    def read(self, run_id: Id) -> list[Event]:
        """Read the verified event history for ``run_id``."""
        return self._store.events(run_id)


@dataclass(frozen=True, slots=True)
class RuntimeDeps:
    """All runtime handles assembled once by the API composition root."""

    run_service: RunService
    audit_freshness: AuditFreshnessService
    risk_interview: RiskInterviewService
    session_service: SessionService
    run_store: LocalRunStore
    lifecycle_repository: LocalLifecycleRepository
    run_repository: RunRepository
    session_repository: SessionRepository
    record_repository: RecordRepository
    event_store: EventStore
    event_subscription: EventSubscription
    checkpoint_repository: CheckpointRepository
    artifact_store_factory: ArtifactStoreFactory
    board_repository: BoardRepository
    dag_repository: DagRepository
    goal_board: GoalBoardService
    gate_factory: GateFactory
    actor_factory: ActorFactory
    project_resolution: ProjectResolution | None
    tool_registry: ToolRegistry
    mira_tool_registry: ToolRegistry
    tool_manager: ToolManager
    dispatcher: ExecutionDispatcher
    idempotency_journal: IdempotencyJournal
    idempotency_lock: RLock
    settings_store: LocalSettingsStore
    workspace_registry: LocalWorkspaceRegistry
    # ``repr=False`` is belt and braces beside ``BearerTokenRegistry.__repr__``: this dataclass's
    # generated repr reaches every log line and traceback that formats the dependency graph, and a
    # resolver added later must not leak a credential through it.
    principal_resolver: PrincipalResolver = field(repr=False)
    authority_proof_builder: AuthorityProofBuilder = field(repr=False)


def _project_policy(project_resolution: ProjectResolution | None) -> Policy:
    """Layer each configured framework's packaged policy, then the project's own overlay, on base.

    Without this, every Gate call ran the bare base policy regardless of the configured project,
    so a project's ``.thymira/policies.yaml`` (its own finding/action rules) was loaded and unit
    tested but never actually reached the Gate a live Run's THY plan and MIRA findings go through.
    A framework with no packaged default (not every ``Framework`` ships one) is skipped rather than
    raising -- a project may declare a framework MIRA classifies findings against without a
    dedicated policy existing yet.
    """
    if project_resolution is None:
        return load_default_policy()
    names: list[str] = []
    for framework in project_resolution.frameworks:
        name = framework.value.lower()
        try:
            load_default_policy(name)
        except FileNotFoundError:
            continue
        names.append(name)
    stack = load_policy_stack(*names)
    overlay_path = project_resolution.workspace / "policies.yaml"
    if overlay_path.is_file():
        stack = stack.merged_with(load_policy(overlay_path))
    return stack


def _require_composition_credentials(principal_resolver: PrincipalResolver) -> None:
    """Refuse a composition that could serve an anonymous caller.

    F13.3 removed the permissive resolver, so this is a hard requirement rather than a default.
    """
    if principal_resolver is None:
        raise TypeError("principal_resolver is required; anonymous API composition is disabled")


def _lifecycle_composition(
    root: Path,
    *,
    run_store: LocalRunStore,
    session_repository: LocalSessionRepository,
    authority_secret: bytes | None,
    issuer_process_id: str,
    issuer_key_id: str,
    authority_verifier: Callable[[ControlInput], bool] | None,
) -> tuple[Callable[[ControlInput], bool], LocalLifecycleRepository, AuthorityProofBuilder]:
    """Compose the durable lifecycle boundary: verifier, repository and proof builder.

    A caller-supplied ``authority_verifier`` still wins; otherwise the process signing secret backs
    both the verifier the repository fails closed on and the builder every route signs with. With
    no secret supplied the composition establishes its own private key below ``root``, exactly as
    it establishes its own bearer token, so no composition ever runs with an unsigned authority.
    """
    if not authority_secret:
        authority_secret, _ = resolve_authority_secret(root)
    verifier = authority_verifier
    if verifier is None:
        verifier = HmacAuthorityVerifier(
            authority_secret,
            issuer_process_id=issuer_process_id,
            issuer_key_id=issuer_key_id,
        )
    repository = LocalLifecycleRepository(
        root,
        run_store=run_store,
        session_repository=session_repository,
        authority_verifier=verifier,
    )
    proof_builder = AuthorityProofBuilder(
        authority_secret,
        issuer_process_id=issuer_process_id,
        issuer_key_id=issuer_key_id,
    )
    return verifier, repository, proof_builder


def build_default_deps(  # noqa: PLR0915  # one API composition root owns every runtime handle
    root: Path,
    *,
    workspace: Path | None = None,
    dispatcher: ExecutionDispatcher | None = None,
    graph_factory: GraphFactory | None = None,
    graph_definition_hash: str | Callable[[], str] | None = None,
    principal_resolver: PrincipalResolver,
    authority_secret: bytes | None = None,
    authority_issuer_process_id: str = "thymira-api",
    authority_issuer_key_id: str = "api-authority-v1",
    mira_tool_registry: ToolRegistry | None = None,
    regulation_store: RegulationStore | None = None,
    regulation_path: Path | None = None,
    model_route_policy: ModelRoutePolicy | None = None,
    authority_verifier: Callable[[ControlInput], bool] | None = None,
    provider: LLMProvider | None = None,
) -> RuntimeDeps:
    """Build the local MVP dependencies below one caller-owned root directory.

    The local Run event history remains authoritative through ``LocalRunStore``. The exposed
    ``EventStore`` is an adapter over that history, so this composition never creates a second
    event log for the same Run. MIRA receives its own explicit one-tool registry over the configured
    regulation store; absent a caller-supplied store, the packaged deterministic local JSONL corpus
    is used and no PostgreSQL connection is required. ``principal_resolver`` is required and has no
    default: F13.3 forbids a composition that serves an anonymous caller, so the permissive mode
    that used to exist behind ``None`` is gone rather than guarded -- there is no branch left to
    reach. ``thymira.api.credential.resolve_process_credential`` builds the production one. The
    sandbox backend for every code-executing tool (``run_python``, Git,
    ``run_experiment``, ``inspect_model``, ``audit_model``) is selected from the process
    environment at call time (``THYMIRA_SANDBOX_BACKEND`` and its siblings, defaulting to
    ``container``) through :func:`~thymira.tools.builtins.configured_builtins_registry`; the
    ``.env`` file itself is still loaded only by ``thymira.api.server.main``, never here.
    """
    _require_composition_credentials(principal_resolver)
    root = Path(root)
    workspace_registry = LocalWorkspaceRegistry(root / "workspace-registry")
    project_resolution = (
        ProjectResolver(workspace_registry).resolve(workspace) if workspace is not None else None
    )
    # Always install a composition-owned snapshot. An unset operator allowlist is an explicit
    # empty policy, so normal server startup cannot accidentally create a permissive Session.
    if model_route_policy is None:
        model_route_policy = ModelRoutePolicy.from_environment()
    run_store = LocalRunStore(root / "runs")
    settings_store = LocalSettingsStore(root / "settings")
    # A live model-choice override takes effect through routing.py's in-process cache, not a
    # settings-store read on every routed call (see set_model_overrides' own docstring for why);
    # prime it once here so a value a prior process persisted survives this process's restart.
    _existing_model_overrides = settings_store.get(MODEL_OVERRIDES_NAMESPACE)
    set_model_overrides(
        {} if _existing_model_overrides is None else _existing_model_overrides.values
    )
    repository_root = root / "repositories"
    run_repository = LocalRunRepository(repository_root)
    session_repository = LocalSessionRepository(repository_root)
    authority_verifier, lifecycle_repository, authority_proof_builder = _lifecycle_composition(
        root,
        run_store=run_store,
        session_repository=session_repository,
        authority_secret=authority_secret,
        issuer_process_id=authority_issuer_process_id,
        issuer_key_id=authority_issuer_key_id,
        authority_verifier=authority_verifier,
    )
    session_service = SessionService(
        session_repository,
        model_route_policy=model_route_policy,
    )
    record_repository = LocalRecordRepository(repository_root)
    event_store: EventStore = _RunStoreEventStore(run_store)
    event_subscription = PollingEventSubscription(event_store)
    checkpoint_repository = LocalCheckpointRepository(repository_root)
    board_repository = LocalBoardRepository(root / "boards")
    dag_repository = LocalDagRepository(root / "dag")
    tool_registry = configured_builtins_registry()
    tool_manager = ToolManager(tool_registry)
    policy_engine = PolicyEngine(_project_policy(project_resolution))
    usage_ledgers = UsageLedgerRegistry()
    if mira_tool_registry is not None and (
        regulation_store is not None or regulation_path is not None
    ):
        raise ValueError("provide mira_tool_registry or a regulation store, not both")
    resolved_mira_tool_registry = mira_tool_registry
    if resolved_mira_tool_registry is None:
        resolved_store = (
            regulation_store
            if regulation_store is not None
            else load_default_regulation_store(regulation_path)
        )
        resolved_mira_tool_registry = build_mira_tool_registry(resolved_store)

    def gate_factory(
        run_id: Id,
        *,
        approver: Approver | None = None,
        human: Actor | None = None,
    ) -> Gate:
        """Build a Gate with an optional caller-supplied approval response.

        Passing both `approver=` and `human=` makes the Gate answer a pending review in process
        and record the answer under `human` -- exactly what the approve/reject route needs to
        resolve a review synchronously (`apps/api/src/thymira/api/routes/runs.py:377-381`, Task 7).
        The execution graph's own Gate must never be built that way: it is built with neither
        (`runtime/core/src/thymira/core/graph/adapters.py:917` calls `gate_factory(run.id)` with
        no keyword arguments at all, and the production worker's own `gate_factory` in
        `runtime/core/src/thymira/core/worker.py:154-155` accepts only `run_id` in the first
        place) -- a Gate with both would auto-answer every tool-call review a Run's own plan
        raises, in process, defeating the human-review boundary the Policy Engine relies on.
        """
        return Gate(
            policy_engine,
            event_store.open(run_id),
            approver=approver,
            human=human,
        )

    def artifact_store_factory(run_id: Id) -> ArtifactStore:
        """Build the local artifact store for one Run."""
        return LocalArtifactStore(root / "artifacts" / run_id, run_id)

    def route_policy_for(run: Run) -> ModelRoutePolicy:
        """Resolve a Run-bound policy, failing closed when its Session has disappeared."""
        if session_repository.get(run.session_id) is None:
            return ModelRoutePolicy.unavailable()
        return run.model_route_policy.narrowed_by(model_route_policy)

    def interview_model_context(run_id: Id) -> RiskInterviewModelContext | None:
        """Build the interview model handles from the API's run-scoped governance handles."""
        return build_risk_interview_model_context(
            event_store.open(run_id),
            gate_factory(run_id),
            usage_ledgers.for_run(run_id),
            provider=provider,
            route_policy=route_policy_for(run_store.get(run_id)),
        )

    risk_interview = RiskInterviewService(
        run_store,
        MiraControlPlane(run_store, policy_engine),
        model_context_factory=interview_model_context,
    )

    goal_board = GoalBoardService(
        board_repository,
        artifact_store_factory=artifact_store_factory,
        authority_verifier=authority_verifier,
    )

    if dispatcher is None:
        if graph_factory is None:
            graph_factory = build_runtime_graph_factory(
                run_store,
                gate_factory=gate_factory,
                artifact_store_factory=artifact_store_factory,
                tool_manager=tool_manager,
                tool_registry=tool_registry,
                mira_tool_registry=resolved_mira_tool_registry,
                record_repository=record_repository,
                checkpoint_repository=checkpoint_repository,
                project_dir=(
                    project_resolution.project_dir if project_resolution is not None else None
                ),
                project_config=(
                    project_resolution.config if project_resolution is not None else None
                ),
                risk_interview=risk_interview,
                route_policy=route_policy_for,
                plan_repository=board_repository,
                dag_repository=dag_repository,
                usage_ledgers=usage_ledgers,
            )
        dispatcher = InlineDispatcher(run_store, graph_factory)
    idempotency_journal = IdempotencyJournal()
    resolved_graph_hash: str | Callable[[], str]
    if graph_definition_hash is None:
        resolved_graph_hash = composed_graph_definition_hash()
    else:
        resolved_graph_hash = graph_definition_hash
    audit_freshness = AuditFreshnessService(
        run_store,
        artifact_store_factory,
        policy_sha256=policy_engine.policy_sha256,
        graph_definition_hash=canonical_graph_definition_hash,
    )
    run_service = RunService(
        run_store,
        session_service,
        dispatcher=dispatcher,
        policy_sha256=policy_engine.policy_sha256,
        graph_definition_hash=resolved_graph_hash,
        risk_interview=risk_interview,
        gate_factory=gate_factory,
        audit_freshness=audit_freshness,
        lifecycle_repository=lifecycle_repository,
    )
    return RuntimeDeps(
        run_service=run_service,
        audit_freshness=audit_freshness,
        risk_interview=risk_interview,
        session_service=session_service,
        run_store=run_store,
        lifecycle_repository=lifecycle_repository,
        run_repository=run_repository,
        session_repository=session_repository,
        record_repository=record_repository,
        dag_repository=dag_repository,
        event_store=event_store,
        event_subscription=event_subscription,
        checkpoint_repository=checkpoint_repository,
        artifact_store_factory=artifact_store_factory,
        board_repository=board_repository,
        goal_board=goal_board,
        gate_factory=gate_factory,
        actor_factory=Actor.system,
        project_resolution=project_resolution,
        tool_registry=tool_registry,
        mira_tool_registry=resolved_mira_tool_registry,
        tool_manager=tool_manager,
        dispatcher=dispatcher,
        idempotency_journal=idempotency_journal,
        idempotency_lock=RLock(),
        settings_store=settings_store,
        principal_resolver=principal_resolver,
        workspace_registry=workspace_registry,
        authority_proof_builder=authority_proof_builder,
    )


def get_runtime_deps(request: Request) -> RuntimeDeps:
    """Return the application-scoped runtime dependencies for a future route."""
    try:
        return request.app.state.runtime_deps
    except AttributeError as exc:
        raise RuntimeError("FastAPI app has no RuntimeDeps configured") from exc


__all__ = [
    "ActorFactory",
    "ArtifactStoreFactory",
    "GateFactory",
    "RuntimeDeps",
    "build_default_deps",
    "get_runtime_deps",
]
