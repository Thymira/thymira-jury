"""Persistence layer for Thymira runtime state.

This member exposes local repositories for runs, sessions and child records, the hash-chained
event store, checkpoints, the transactional write seam, the :class:`ArtifactStore` protocol with
its filesystem implementation, and :class:`LocalRunStore`. ADR-0010 defines the local JSON/JSONL
Run state used by the control plane: ``events.jsonl`` is authoritative and ``run.json`` is a
reconstructible projection. The PostgreSQL package also provides a bounded lifecycle repository;
callers must select one lifecycle backend for a queued Run so ownership and canonical state never
split across local and PostgreSQL stores.

Contracts live in ``thymira.schemas``; this package never redefines them.
"""

from thymira.events import StoragePermissionError, StoragePermissionEvidence
from thymira.state._atomic import replace_with_retry
from thymira.state._lifecycle_authority import HmacAuthorityVerifier, sign_authority
from thymira.state.artifact_store import ArtifactStore, ArtifactWrite
from thymira.state.authority_credentials import (
    AUTHORITY_KEY_BYTES,
    AUTHORITY_KEY_FILE_NAME,
    AUTHORITY_SECRET_ENV_VAR,
    AuthorityCredentialError,
    resolve_authority_secret,
)
from thymira.state.board import (
    BoardConflictError,
    BoardPersistenceError,
    BoardRepository,
    LocalBoardRepository,
)
from thymira.state.checkpoints import CheckpointRepository, LocalCheckpointRepository
from thymira.state.dag import DagRepository, LocalDagRepository, LocalOrchestratorBoardRepository
from thymira.state.events import EventStore, LocalEventStore
from thymira.state.lifecycle import (
    IdempotencyConflictError,
    LifecycleBackendUnavailable,
    LifecycleBackendUnavailableError,
    LifecycleError,
    LifecycleRepository,
    LocalLifecycleRepository,
    OwnerBusyError,
    PublicationError,
    RunHandle,
    StaleOwnerError,
)
from thymira.state.local_run_store import LocalRunStore
from thymira.state.local_store import LocalArtifactStore, canonical_artifact_name
from thymira.state.owned_writes import OwnedArtifactStore, OwnedCheckpointRepository
from thymira.state.records import LocalRecordRepository, Record, RecordRepository
from thymira.state.repositories import (
    LocalRunRepository,
    LocalSessionRepository,
    Page,
    RunRepository,
    SessionRepository,
)
from thymira.state.runs import InMemoryRunRepository
from thymira.state.sessions import InMemorySessionRepository
from thymira.state.settings import (
    LocalSettingsStore,
    SettingsConflictError,
    SettingsCorruptError,
    SettingsError,
)
from thymira.state.unit_of_work import LocalUnitOfWork, UnitOfWork
from thymira.state.workspace_registry import (
    LocalWorkspaceRegistry,
    canonical_workspace_path,
    workspace_id,
    workspace_identity,
)

__all__ = [
    "AUTHORITY_KEY_BYTES",
    "AUTHORITY_KEY_FILE_NAME",
    "AUTHORITY_SECRET_ENV_VAR",
    "ArtifactStore",
    "ArtifactWrite",
    "AuthorityCredentialError",
    "BoardConflictError",
    "BoardPersistenceError",
    "BoardRepository",
    "CheckpointRepository",
    "DagRepository",
    "EventStore",
    "HmacAuthorityVerifier",
    "IdempotencyConflictError",
    "InMemoryRunRepository",
    "InMemorySessionRepository",
    "LifecycleBackendUnavailable",
    "LifecycleBackendUnavailableError",
    "LifecycleError",
    "LifecycleRepository",
    "LocalArtifactStore",
    "LocalBoardRepository",
    "LocalCheckpointRepository",
    "LocalDagRepository",
    "LocalEventStore",
    "LocalLifecycleRepository",
    "LocalOrchestratorBoardRepository",
    "LocalRecordRepository",
    "LocalRunRepository",
    "LocalRunStore",
    "LocalSessionRepository",
    "LocalSettingsStore",
    "LocalUnitOfWork",
    "LocalWorkspaceRegistry",
    "OwnedArtifactStore",
    "OwnedCheckpointRepository",
    "OwnerBusyError",
    "Page",
    "PublicationError",
    "Record",
    "RecordRepository",
    "RunHandle",
    "RunRepository",
    "SessionRepository",
    "SettingsConflictError",
    "SettingsCorruptError",
    "SettingsError",
    "StaleOwnerError",
    "StoragePermissionError",
    "StoragePermissionEvidence",
    "UnitOfWork",
    "canonical_artifact_name",
    "canonical_workspace_path",
    "replace_with_retry",
    "resolve_authority_secret",
    "sign_authority",
    "workspace_id",
    "workspace_identity",
]
