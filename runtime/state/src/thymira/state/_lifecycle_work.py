"""Durable local work claims and owner-bound settlement."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from thymira.schemas import (
    DispatchState,
    ExecutionOutcomeKind,
    FailureCause,
    RunOwnerHandle,
    WorkClaim,
    WorkItem,
    WorkResult,
    WorkState,
    new_id,
    utc_now,
)
from thymira.state._lifecycle_lock import FileLock
from thymira.state.lifecycle_errors import LifecycleError, OwnerBusyError, StaleOwnerError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from thymira.state._lifecycle_files import LifecycleFiles


class WorkBoard:
    """Persist exclusive work claims and results under the shared Run owner guard."""

    _CLAIM_TTL = timedelta(minutes=5)

    def __init__(
        self,
        files: LifecycleFiles,
        assert_owner: Callable[[RunOwnerHandle], None],
    ) -> None:
        self._files = files
        self._assert_owner = assert_owner

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Atomically claim one pending work item with a bounded claim expiry."""
        if not claimant_id.strip():
            raise ValueError("claimant_id must not be empty")
        if limit != 1:
            raise ValueError("local claim_work accepts exactly one item")
        target = self._files.work_path(work_id)
        try:
            lock = FileLock(self._files.locks / f"work-{work_id}.lock")
            with lock:
                value = self._files.read_json(target)
                if value is None:
                    return None
                item = WorkItem.model_validate(value)
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
                    self._files.write_json(target, item.to_json_dict())
                if item.state is not WorkState.PENDING:
                    return None
                claim = WorkClaim(
                    work_id=item.work_id,
                    run_id=item.run_id,
                    claimant_id=claimant_id,
                    attempt=item.attempt + 1,
                    claim_token=new_id("claim"),
                    expires_at=now + self._CLAIM_TTL,
                )
                self._files.write_json(
                    target,
                    item.model_copy(
                        update={
                            "state": WorkState.CLAIMED,
                            "attempt": claim.attempt,
                            "claim_owner": claimant_id,
                            "claim_token": claim.claim_token,
                            "claim_expires_at": claim.expires_at,
                        }
                    ).to_json_dict(),
                )
                return claim
        except OwnerBusyError:
            return None

    def get(self, work_id: str) -> WorkItem | None:
        """Read a work definition without changing its claim or dispatch state."""
        value = self._files.read_json(self._files.work_path(work_id))
        if value is None:
            return None
        return WorkItem.model_validate(value)

    def result(self, work_id: str) -> WorkResult | None:
        """Read the typed result embedded in the authoritative work record."""
        item = self.get(work_id)
        return item.settled_result if item is not None else None

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Record dispatch after checking both work claim and Run owner fencing."""
        self._assert_owner(owner)
        target = self._files.work_path(claim.work_id)
        with FileLock(self._files.locks / f"work-{claim.work_id}.lock"):
            item = self._claimed_item(target, claim)
            updated = item.model_copy(update={"dispatch_state": DispatchState.DISPATCHED})
            self._files.write_json(target, updated.to_json_dict())
            return updated

    def settle_work(self, claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> WorkItem:
        """Durably settle a claimed work item under the live Run owner."""
        self._assert_owner(owner)
        target = self._files.work_path(claim.work_id)
        with FileLock(self._files.locks / f"work-{claim.work_id}.lock"):
            item = self._claimed_item(target, claim)
            result = WorkResult.model_validate(result)
            _validate_result_source(item, claim, result)
            settled = item.model_copy(
                update={
                    "state": WorkState.SETTLED,
                    "claim_expires_at": None,
                    "dispatch_state": result.dispatch_state,
                    "settled_result": result,
                }
            )
            self._files.write_json(target, settled.to_json_dict())
        return settled

    def recover_expired(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Recover an expired claim, preserving unknown effects instead of retrying silently."""
        self._assert_owner(owner)
        target = self._files.work_path(claim.work_id)
        with FileLock(self._files.locks / f"work-{claim.work_id}.lock"):
            item = self._claimed_item(target, claim, allow_expired=True)
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
                self._files.write_json(target, recovered.to_json_dict())
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
            self._files.write_json(target, settled.to_json_dict())
            return settled

    def _claimed_item(
        self,
        target: Path,
        claim: WorkClaim,
        *,
        allow_expired: bool = False,
    ) -> WorkItem:
        value = self._files.read_json(target)
        if value is None:
            raise LifecycleError(f"work item {claim.work_id} does not exist")
        item = WorkItem.model_validate(value)
        if (
            item.state is not WorkState.CLAIMED
            or item.run_id != claim.run_id
            or item.claim_owner != claim.claimant_id
            or item.attempt != claim.attempt
            or item.claim_token != claim.claim_token
        ):
            raise StaleOwnerError("work claim is no longer current")
        if (
            not allow_expired
            and item.claim_expires_at is not None
            and item.claim_expires_at <= utc_now()
        ):
            raise StaleOwnerError("work claim expired before owner settlement")
        return item


def _validate_result_source(item: WorkItem, claim: WorkClaim, result: WorkResult) -> None:
    """Bind optional delegated-result provenance to the exact claimed WorkItem facts.

    The generic lifecycle schema has no child fields yet; ``getattr`` keeps this helper harmless
    for generic work while enforcing the stronger source envelope when the goal-board schema is
    composed. A caller cannot settle a child result against another work item, attempt or claim.
    """
    child = getattr(result, "subagent_result", None)
    if child is None:
        return
    expected = {
        "source_work_id": item.work_id,
        "source_run_id": item.run_id,
        "source_attempt": claim.attempt,
        "source_claim_token": claim.claim_token,
        "source_ordinal": item.ordinal,
        "source_kind": item.kind,
        "source_delegation_depth": item.delegation_depth,
        "source_agent_id": getattr(item, "agent_id", None),
        "source_agent": getattr(item, "agent", None),
        "source_parent_agent": getattr(item, "parent_agent", None),
        "source_delegation_key": getattr(item, "delegation_key", None),
    }
    for field, value in expected.items():
        if getattr(result, field, None) != value:
            raise StaleOwnerError(f"settled result source does not match claimed WorkItem: {field}")


__all__ = ["WorkBoard"]
