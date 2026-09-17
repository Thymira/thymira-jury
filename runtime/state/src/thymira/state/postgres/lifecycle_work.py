"""Transactional PostgreSQL work claims, dispatch and settlement."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa

from thymira.events import canonical_json
from thymira.schemas import (
    DispatchState,
    ExecutionOutcomeKind,
    FailureCause,
    RunOwnerHandle,
    WorkClaim,
    WorkItem,
    WorkResult,
    WorkState,
    utc_now,
)
from thymira.state.lifecycle_errors import LifecycleError, StaleOwnerError
from thymira.state.postgres import tables
from thymira.state.postgres._serde import load_document
from thymira.state.postgres.lifecycle_util import row_document, work_values

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from thymira.state.postgres.lifecycle_owner import PgOwnerRegistry

_CLAIM_TTL = timedelta(minutes=5)


def _locked_claimed_item(conn: Connection, claim: WorkClaim) -> WorkItem:
    """Lock and validate a current WorkClaim inside a transaction."""
    row = (
        conn.execute(
            sa.select(tables.lifecycle_work)
            .where(tables.lifecycle_work.c.id == claim.work_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LifecycleError(f"work item {claim.work_id} does not exist")
    item = WorkItem.model_validate(row_document(row))
    if (
        item.state is not WorkState.CLAIMED
        or item.run_id != claim.run_id
        or item.claim_owner != claim.claimant_id
        or item.attempt != claim.attempt
        or item.claim_token != claim.claim_token
        or item.claim_expires_at != claim.expires_at
    ):
        raise StaleOwnerError("work claim is no longer current")
    return item


def _result_document(claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> str:
    """Encode the same result fact retained in WorkItem.settled_result."""
    return canonical_json(
        {
            "work_id": claim.work_id,
            "run_id": claim.run_id,
            "claim_token": claim.claim_token,
            "result": result.to_json_dict(),
            "owner_epoch": owner.epoch,
        }
    )


def _validate_result_binding(item: WorkItem, claim: WorkClaim, result: WorkResult) -> None:
    """Require a delegated result to match immutable facts in the locked WorkItem row.

    The caller's result is transport input.  The WorkItem loaded by ``FOR UPDATE`` is the
    authority for task/objective, invocation identity, topology and the current claim.  Generic
    lifecycle results carry no child envelope and therefore have no source binding to validate.
    """
    if result.subagent_result is None:
        return
    if (
        item.task_id is None
        or item.objective is None
        or item.agent_id is None
        or item.agent is None
        or item.parent_agent is None
        or item.delegation_key is None
    ):
        raise LifecycleError("work item is missing its immutable child invocation binding")
    expected = (
        item.work_id,
        item.run_id,
        item.task_id,
        claim.attempt,
        claim.claim_token,
        item.ordinal,
        item.kind,
        item.objective,
        item.delegation_depth,
        item.agent_id,
        item.agent,
        item.parent_agent,
        item.delegation_key,
    )
    actual = (
        result.source_work_id,
        result.source_run_id,
        result.source_task_id,
        result.source_attempt,
        result.source_claim_token,
        result.source_ordinal,
        result.source_kind,
        result.source_objective,
        result.source_delegation_depth,
        result.source_agent_id,
        result.source_agent,
        result.source_parent_agent,
        result.source_delegation_key,
    )
    if actual != expected:
        raise LifecycleError("child result is not bound to the authoritative work invocation")


class PgWorkBoard:
    """Persist bounded work claims and owner-fenced typed results."""

    def __init__(self, engine: Engine, owners: PgOwnerRegistry) -> None:
        self._engine = engine
        self._owners = owners

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Atomically claim one pending work item with a bounded expiry."""
        if not claimant_id.strip():
            raise ValueError("claimant_id must not be empty")
        if limit != 1:
            raise ValueError("PgLifecycleRepository.claim_work accepts exactly one item")
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(tables.lifecycle_work)
                    .where(tables.lifecycle_work.c.id == work_id)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            item = WorkItem.model_validate(row_document(row))
            now = utc_now()
            if (
                item.state is WorkState.CLAIMED
                and item.claim_expires_at is not None
                and item.claim_expires_at <= now
            ):
                if item.dispatch_state is not DispatchState.NEVER_DISPATCHED:
                    return None
                item = item.model_copy(
                    update={
                        "state": WorkState.PENDING,
                        "claim_owner": None,
                        "claim_token": None,
                        "claim_expires_at": None,
                    }
                )
            if item.state is not WorkState.PENDING:
                return None
            claim = WorkClaim(
                work_id=item.work_id,
                run_id=item.run_id,
                claimant_id=claimant_id,
                attempt=item.attempt + 1,
                claim_token=secrets.token_urlsafe(32),
                expires_at=now + _CLAIM_TTL,
            )
            claimed = item.model_copy(
                update={
                    "state": WorkState.CLAIMED,
                    "attempt": claim.attempt,
                    "claim_owner": claimant_id,
                    "claim_token": claim.claim_token,
                    "claim_expires_at": claim.expires_at,
                }
            )
            conn.execute(
                tables.lifecycle_work.update()
                .where(tables.lifecycle_work.c.id == item.work_id)
                .values(**work_values(claimed))
            )
            return claim

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read one durable WorkItem without granting write authority."""
        with self._engine.connect() as conn:
            document = conn.execute(
                sa.select(tables.lifecycle_work.c.document).where(
                    tables.lifecycle_work.c.id == work_id
                )
            ).scalar_one_or_none()
        return None if document is None else WorkItem.model_validate(load_document(document))

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read the typed result embedded in the authoritative WorkItem row."""
        item = self.get_work(work_id)
        return item.settled_result if item is not None else None

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Record dispatch only for a current, unexpired claim and live owner."""
        return self._mutate_claim(claim, owner, DispatchState.DISPATCHED, None)

    def settle_work(
        self,
        claim: WorkClaim,
        result: WorkResult,
        owner: RunOwnerHandle,
    ) -> WorkItem:
        """Settle one claim atomically with its typed result and owner epoch."""
        if (
            result.kind is ExecutionOutcomeKind.UNKNOWN
            and result.dispatch_state is not DispatchState.EFFECT_UNKNOWN
        ):
            raise ValueError("unknown work results require EFFECT_UNKNOWN dispatch state")
        return self._mutate_claim(claim, owner, result.dispatch_state, result)

    def recover_expired_work(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Requeue undispatched expiry or durably settle dispatched expiry as UNKNOWN."""
        guard = self._owners.guard_for(owner.run_id)
        with guard:
            self._owners.assert_live(owner, run_id=claim.run_id)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                item = _locked_claimed_item(conn, claim)
                if item.claim_expires_at is None or item.claim_expires_at > utc_now():
                    raise LifecycleError("work claim has not expired")
                if item.dispatch_state is DispatchState.NEVER_DISPATCHED:
                    recovered = item.model_copy(
                        update={
                            "state": WorkState.PENDING,
                            "claim_owner": None,
                            "claim_token": None,
                            "claim_expires_at": None,
                        }
                    )
                    conn.execute(
                        tables.lifecycle_work.update()
                        .where(tables.lifecycle_work.c.id == item.work_id)
                        .values(**work_values(recovered))
                    )
                    return recovered
                result = WorkResult(
                    kind=ExecutionOutcomeKind.UNKNOWN,
                    dispatch_state=DispatchState.EFFECT_UNKNOWN,
                    cause=FailureCause(
                        code="claim_expired_after_dispatch",
                        phase="work_recovery",
                        exception_type="WorkerLost",
                        message="Worker claim expired after dispatch; effect status is unknown",
                        effects_may_have_occurred=True,
                    ),
                )
                settled = item.model_copy(
                    update={
                        "state": WorkState.SETTLED,
                        "claim_expires_at": None,
                        "dispatch_state": DispatchState.EFFECT_UNKNOWN,
                        "settled_result": result,
                    }
                )
                conn.execute(
                    tables.lifecycle_work.update()
                    .where(tables.lifecycle_work.c.id == item.work_id)
                    .values(
                        **work_values(
                            settled,
                            result_document=_result_document(claim, result, owner),
                        )
                    )
                )
                return settled

    def _mutate_claim(
        self,
        claim: WorkClaim,
        owner: RunOwnerHandle,
        dispatch_state: DispatchState,
        result: WorkResult | None,
    ) -> WorkItem:
        """Apply one owner-fenced dispatch or settlement mutation."""
        guard = self._owners.guard_for(owner.run_id)
        with guard:
            self._owners.assert_live(owner, run_id=claim.run_id)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                item = _locked_claimed_item(conn, claim)
                if item.claim_expires_at is None or item.claim_expires_at <= utc_now():
                    raise LifecycleError("work claim has expired")
                if result is not None:
                    _validate_result_binding(item, claim, result)
                if result is None:
                    updated = item.model_copy(update={"dispatch_state": dispatch_state})
                    values = work_values(updated)
                else:
                    updated = item.model_copy(
                        update={
                            "state": WorkState.SETTLED,
                            "claim_expires_at": None,
                            "dispatch_state": dispatch_state,
                            "settled_result": result,
                        }
                    )
                    values = work_values(
                        updated,
                        result_document=_result_document(claim, result, owner),
                    )
                conn.execute(
                    tables.lifecycle_work.update()
                    .where(tables.lifecycle_work.c.id == claim.work_id)
                    .values(**values)
                )
                return updated


__all__ = ["PgWorkBoard"]
