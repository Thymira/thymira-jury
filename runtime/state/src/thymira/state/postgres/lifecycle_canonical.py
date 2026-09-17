"""Owner-fenced PostgreSQL canonical event and causal outcome writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from thymira.events import canonical_json
from thymira.schemas import (
    Actor,
    AuditOutcome,
    Event,
    EventSurface,
    EventType,
    ExecutionOutcome,
    RunOwnerHandle,
    TerminalBinding,
)
from thymira.state.lifecycle_errors import IdempotencyConflictError
from thymira.state.postgres import tables
from thymira.state.postgres.events import PgEventStore, append_event_on
from thymira.state.postgres.lifecycle_util import validate_turn_ended

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from thymira.state.postgres.lifecycle_owner import PgOwnerRegistry


class PgCanonicalStore:
    """Append canonical events and persist immutable lifecycle outcomes under owner fencing."""

    def __init__(self, engine: Engine, owners: PgOwnerRegistry) -> None:
        self._engine = engine
        self._owners = owners

    def events(self, run_id: str) -> list[Event]:
        """Return the verified event chain for one Run."""
        return PgEventStore(self._engine).read(run_id)

    def version(self, run_id: str) -> int:
        """Return the event-derived version for one Run."""
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.select(sa.func.count())
                .select_from(tables.events)
                .where(tables.events.c.run_id == run_id)
            ).scalar_one()
        return int(value)

    def append_owned(
        self,
        owner: RunOwnerHandle,
        type: EventType,  # noqa: A002  # event contract uses `type`
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        subject_id: str | None = None,
        producer: str = "thymira.state.lifecycle",
        producer_version: str = "0.1",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
        allow_transition: bool = False,
    ) -> Event:
        """Append one canonical event while holding and validating the Run owner fence."""
        if type is EventType.RUN_TRANSITIONED and not allow_transition:
            raise PermissionError("Run transitions must be appended by RunController")
        guard = self._owners.guard_for(owner.run_id)
        with guard:
            self._owners.assert_live(owner)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                if type is EventType.TURN_ENDED:
                    validate_turn_ended(conn, owner.run_id, payload)
                current = conn.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.events)
                    .where(tables.events.c.run_id == owner.run_id)
                ).scalar_one()
                current_version = int(current)
                if expected_version is not None and expected_version != current_version:
                    raise ValueError(
                        f"run {owner.run_id}: expected version {expected_version}, "
                        f"current version is {current_version}"
                    )
                return append_event_on(
                    conn,
                    owner.run_id,
                    type,
                    actor,
                    payload,
                    subject_id=subject_id,
                    producer=producer,
                    producer_version=producer_version,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    authorization_context_sha256=authorization_context_sha256,
                    surface=surface,
                    lock_run=False,
                )

    def save_execution_outcome(
        self,
        outcome: ExecutionOutcome,
        owner: RunOwnerHandle,
    ) -> ExecutionOutcome:
        """Persist one immutable execution outcome behind the owner fence."""
        self._save_outcome(outcome.outcome_id, outcome.run_id, "execution", None, outcome, owner)
        return outcome

    def save_audit_outcome(self, outcome: AuditOutcome, owner: RunOwnerHandle) -> AuditOutcome:
        """Persist one immutable audit outcome linked to its execution outcome."""
        self._save_outcome(
            outcome.outcome_id,
            outcome.run_id,
            "audit",
            outcome.source_execution_outcome_id,
            outcome,
            owner,
        )
        return outcome

    def save_terminal_binding(
        self,
        binding: TerminalBinding,
        owner: RunOwnerHandle,
    ) -> TerminalBinding:
        """Persist the one causal terminal binding for a Run behind the owner fence."""
        guard = self._owners.guard_for(owner.run_id)
        with guard:
            self._owners.assert_live(owner, run_id=binding.run_id)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                existing = conn.execute(
                    sa.select(tables.lifecycle_terminal_bindings.c.document)
                    .where(tables.lifecycle_terminal_bindings.c.run_id == binding.run_id)
                    .with_for_update()
                ).scalar_one_or_none()
                encoded = canonical_json(binding.to_json_dict())
                if existing is not None and existing != encoded:
                    raise IdempotencyConflictError(
                        f"Run {binding.run_id} already has another terminal binding"
                    )
                if existing is None:
                    conn.execute(
                        tables.lifecycle_terminal_bindings.insert().values(
                            run_id=binding.run_id,
                            document=encoded,
                        )
                    )
        return binding

    def _save_outcome(
        self,
        outcome_id: str,
        run_id: str,
        outcome_type: str,
        source_outcome_id: str | None,
        outcome: ExecutionOutcome | AuditOutcome,
        owner: RunOwnerHandle,
    ) -> None:
        """Persist one immutable outcome document with owner fencing."""
        guard = self._owners.guard_for(run_id)
        with guard:
            self._owners.assert_live(owner, run_id=run_id)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                encoded = canonical_json(outcome.to_json_dict())
                existing = conn.execute(
                    sa.select(tables.lifecycle_outcomes.c.document)
                    .where(tables.lifecycle_outcomes.c.id == outcome_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != encoded:
                        raise IdempotencyConflictError(
                            f"outcome {outcome_id} already has another document"
                        )
                    return
                conn.execute(
                    tables.lifecycle_outcomes.insert().values(
                        id=outcome_id,
                        run_id=run_id,
                        outcome_type=outcome_type,
                        source_outcome_id=source_outcome_id,
                        document=encoded,
                    )
                )


__all__ = ["PgCanonicalStore"]
