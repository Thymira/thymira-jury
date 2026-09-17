"""PostgreSQL advisory ownership and monotonic epoch fencing."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from thymira.schemas import OwnerReleaseReason, RunOwnerHandle, utc_now
from thymira.state.lifecycle_errors import StaleOwnerError
from thymira.state.postgres import tables

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine


@dataclass(slots=True)
class PgOwnerLease:
    """Private advisory-lock resource retained by one serialisable owner identity."""

    connection: Connection
    repository_token: str
    run_id: str


def _run_exists(conn: Connection, run_id: str) -> bool:
    """Return whether a canonical Run row exists on a connection."""
    return (
        conn.execute(
            sa.select(tables.runs.c.id).where(tables.runs.c.id == run_id)
        ).scalar_one_or_none()
        is not None
    )


def _unlock_and_close(connection: Connection, run_id: str) -> None:
    """Release a session advisory lock before returning its connection to the pool."""
    try:
        connection.execute(
            sa.text("SELECT pg_advisory_unlock(hashtext(:run_id), 0)"),
            {"run_id": run_id},
        )
        connection.commit()
    except sa_exc.SQLAlchemyError:
        connection.invalidate()
    finally:
        connection.close()


class PgOwnerRegistry:
    """Hold one dedicated connection-level advisory lock per owned Run."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._repository_token = secrets.token_hex(32)
        self._owner_map: dict[str, RunOwnerHandle] = {}
        self._owner_guards: dict[str, threading.RLock] = {}
        self._map_guard = threading.Lock()

    def acquire(
        self,
        run_id: str,
        claimant_id: str,
        *,
        allow_unmaterialized: bool,
    ) -> RunOwnerHandle | None:
        """Acquire an advisory connection and increment the durable epoch."""
        if not claimant_id.strip():
            raise ValueError("claimant_id must not be empty")
        guard = self.guard_for(run_id)
        with guard:
            with self._map_guard:
                if run_id in self._owner_map:
                    return None
            connection = self._engine.connect()
            lock_held = False
            try:
                lock_held = bool(
                    connection.execute(
                        sa.text("SELECT pg_try_advisory_lock(hashtext(:run_id), 0)"),
                        {"run_id": run_id},
                    ).scalar_one()
                )
                if not lock_held:
                    connection.close()
                    return None
                if not allow_unmaterialized and not _run_exists(connection, run_id):
                    connection.rollback()
                    _unlock_and_close(connection, run_id)
                    return None
                acquired_at = utc_now()
                connection.execute(
                    sa.text(
                        "INSERT INTO lifecycle_owner_epochs "
                        "(run_id, owner_id, epoch, active, acquired_at) "
                        "VALUES (:run_id, :owner_id, 1, true, :acquired_at) "
                        "ON CONFLICT (run_id) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
                        "epoch = lifecycle_owner_epochs.epoch + 1, active = true, "
                        "acquired_at = EXCLUDED.acquired_at, released_at = NULL, "
                        "release_reason = NULL"
                    ),
                    {"run_id": run_id, "owner_id": claimant_id, "acquired_at": acquired_at},
                )
                epoch = int(
                    connection.execute(
                        sa.text("SELECT epoch FROM lifecycle_owner_epochs WHERE run_id = :run_id"),
                        {"run_id": run_id},
                    ).scalar_one()
                )
                connection.commit()
                owner = RunOwnerHandle(run_id=run_id, owner_id=claimant_id, epoch=epoch)
                lease = PgOwnerLease(connection, self._repository_token, run_id)
                object.__setattr__(owner, "_lock_resource", lease)
                object.__setattr__(owner, "_repository_root", self._repository_token)
                with self._map_guard:
                    self._owner_map[run_id] = owner
            except BaseException:
                if lock_held:
                    connection.rollback()
                    _unlock_and_close(connection, run_id)
                else:
                    connection.close()
                raise
            else:
                return owner

    def release(self, owner: RunOwnerHandle, reason: OwnerReleaseReason) -> None:
        """Persist a release reason and then close the advisory-lock connection."""
        guard = self.guard_for(owner.run_id)
        with guard:
            self.assert_live(owner)
            lease = self.lease(owner)
            try:
                lease.connection.execute(
                    sa.text(
                        "UPDATE lifecycle_owner_epochs SET active = false, "
                        "released_at = :released_at, release_reason = :release_reason "
                        "WHERE run_id = :run_id AND owner_id = :owner_id "
                        "AND epoch = :epoch AND active = true"
                    ),
                    {
                        "released_at": utc_now(),
                        "release_reason": reason.value,
                        "run_id": owner.run_id,
                        "owner_id": owner.owner_id,
                        "epoch": owner.epoch,
                    },
                )
                lease.connection.commit()
            finally:
                with self._map_guard:
                    self._owner_map.pop(owner.run_id, None)
                _unlock_and_close(lease.connection, owner.run_id)
                object.__setattr__(owner, "_lock_resource", None)

    def assert_live(self, owner: RunOwnerHandle, *, run_id: str | None = None) -> None:
        """Reject forged, released, cross-repository or stale owner handles."""
        if run_id is not None and owner.run_id != run_id:
            raise StaleOwnerError("owner handle targets another Run")
        with self._map_guard:
            if self._owner_map.get(owner.run_id) is not owner:
                raise StaleOwnerError("Run owner handle is not the active repository owner")
        lease = self.lease(owner)
        if lease.connection.closed:
            raise StaleOwnerError("Run owner advisory connection is closed")
        try:
            try:
                advisory_lock_held = lease.connection.execute(
                    sa.text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_locks "
                        "WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
                        "AND classid = hashtext(:run_id) AND objid = 0 "
                        "AND objsubid = 2 AND granted"
                        ")"
                    ),
                    {"run_id": owner.run_id},
                ).scalar_one()
                if not advisory_lock_held:
                    raise StaleOwnerError("Run owner advisory lock is no longer held")
                row = (
                    lease.connection.execute(
                        sa.text(
                            "SELECT owner_id, epoch, active FROM lifecycle_owner_epochs "
                            "WHERE run_id = :run_id"
                        ),
                        {"run_id": owner.run_id},
                    )
                    .mappings()
                    .one_or_none()
                )
            except sa_exc.SQLAlchemyError as exc:
                raise StaleOwnerError("Run owner advisory connection is unavailable") from exc
        finally:
            lease.connection.rollback()
        if (
            row is None
            or row["active"] is not True
            or row["owner_id"] != owner.owner_id
            or int(row["epoch"]) != owner.epoch
        ):
            raise StaleOwnerError("Run owner fencing epoch is stale")

    def check_epoch(self, conn: Connection, owner: RunOwnerHandle) -> None:
        """Check the active owner row inside a pooled mutation transaction."""
        row = (
            conn.execute(
                sa.select(
                    tables.lifecycle_owner_epochs.c.owner_id,
                    tables.lifecycle_owner_epochs.c.epoch,
                ).where(
                    tables.lifecycle_owner_epochs.c.run_id == owner.run_id,
                    tables.lifecycle_owner_epochs.c.owner_id == owner.owner_id,
                    tables.lifecycle_owner_epochs.c.epoch == owner.epoch,
                    tables.lifecycle_owner_epochs.c.active.is_(True),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise StaleOwnerError("Run owner fencing epoch is stale")

    def guard_for(self, run_id: str) -> threading.RLock:
        """Return the in-process serialisation guard for one Run."""
        with self._map_guard:
            guard = self._owner_guards.get(run_id)
            if guard is None:
                guard = threading.RLock()
                self._owner_guards[run_id] = guard
            return guard

    def lease(self, owner: RunOwnerHandle) -> PgOwnerLease:
        """Return the private lease after checking its repository binding."""
        lease = owner._lock_resource  # noqa: SLF001  # private anti-forgery resource
        if (
            not isinstance(lease, PgOwnerLease)
            or lease.repository_token != self._repository_token
            or lease.run_id != owner.run_id
            or owner._repository_root != self._repository_token  # noqa: SLF001
        ):
            raise StaleOwnerError("Run owner handle does not hold this repository's advisory lock")
        return lease


__all__ = ["PgOwnerLease", "PgOwnerRegistry"]
