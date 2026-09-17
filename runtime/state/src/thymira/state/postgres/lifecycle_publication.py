"""Transactional PostgreSQL Run publication and Session inverse-linking."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from thymira.events import canonical_json
from thymira.schemas import (
    Actor,
    EventType,
    JsonObject,
    OutboxNotification,
    OwnerReleaseReason,
    PreparedPublication,
    PublicationReceipt,
    Run,
    RunOwnerHandle,
    Session,
    WorkItem,
)
from thymira.state.lifecycle_errors import (
    LifecycleError,
    OwnerBusyError,
    PublicationError,
)
from thymira.state.postgres import tables
from thymira.state.postgres._serde import load_document
from thymira.state.postgres.events import append_event_on
from thymira.state.postgres.lifecycle_owner import _run_exists
from thymira.state.postgres.lifecycle_util import publication_document, row_document, work_values
from thymira.state.postgres.repositories import PgRunRepository, upsert_run, upsert_session

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection, Engine

from thymira.state.postgres.lifecycle_owner import PgOwnerRegistry

_PREPARED = "prepared"
_COMMITTED = "committed"


def _ensure_run(
    conn: Connection,
    prepared: PreparedPublication,
    owner: RunOwnerHandle,
) -> None:
    """Create the Run projection and genesis event when publication first materialises."""
    run = prepared.run
    first_event = conn.execute(
        sa.select(tables.events.c.event_id).where(
            tables.events.c.run_id == run.id, tables.events.c.seq == 0
        )
    ).scalar_one_or_none()
    if first_event is None:
        upsert_run(conn, run)
        append_event_on(
            conn,
            run.id,
            EventType.RUN_STARTED,
            prepared.creation_actor,
            {
                **prepared.creation_payload,
                "publication_id": prepared.publication_id,
                "owner_epoch": owner.epoch,
                "run": run.to_json_dict(),
            },
            producer="thymira.state",
            producer_version="0.1",
            lock_run=False,
        )
    elif not _run_exists(conn, run.id):
        raise PublicationError(f"Run {run.id} has events but no Run projection")


def _link_session(conn: Connection, prepared: PreparedPublication) -> None:
    """Add the inverse Run link to a Session in the same transaction."""
    row = conn.execute(
        sa.select(tables.sessions.c.document)
        .where(tables.sessions.c.id == prepared.run.session_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        session = prepared.session
        if session is None:
            raise PublicationError("a new Run requires a Session draft")
    else:
        session = Session.model_validate(load_document(row))
    if session.project_id != prepared.run.project_id:
        raise PublicationError("Session and Run projects do not match")
    if prepared.run.id not in session.run_ids:
        session = session.model_copy(update={"run_ids": (*session.run_ids, prepared.run.id)})
    upsert_session(conn, session)


def _publish_work(conn: Connection, item: WorkItem) -> None:
    """Insert one initial WorkItem and exactly one durable outbox notification."""
    existing = conn.execute(
        sa.select(tables.lifecycle_work.c.document)
        .where(tables.lifecycle_work.c.id == item.work_id)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if WorkItem.model_validate(load_document(existing)) != item:
            raise PublicationError(f"work item {item.work_id} already has another definition")
        return
    conn.execute(tables.lifecycle_work.insert().values(id=item.work_id, **work_values(item)))
    notification = OutboxNotification(work_id=item.work_id, run_id=item.run_id)
    conn.execute(
        tables.lifecycle_outbox.insert().values(
            id=notification.notification_id,
            run_id=notification.run_id,
            kind="work",
            work_id=notification.work_id,
            published=False,
            created_at=notification.created_at,
            document=canonical_json(notification.to_json_dict()),
        )
    )


class PgPublicationStore:
    """Stage and atomically commit complete Run/Session/work publications."""

    def __init__(self, engine: Engine, owners: PgOwnerRegistry) -> None:
        self._engine = engine
        self._owners = owners

    def prepare(
        self,
        session_draft: Session | None,
        run: Run,
        initial_work: WorkItem | Sequence[WorkItem] = (),
        *,
        creation_payload: JsonObject | None = None,
        creation_actor: Actor | None = None,
    ) -> PreparedPublication:
        """Stage a private publication without exposing its Run or Session relation."""
        if session_draft is not None and (
            session_draft.id != run.session_id or session_draft.project_id != run.project_id
        ):
            raise PublicationError("Session draft does not match the Run relationship")
        work = (initial_work,) if isinstance(initial_work, WorkItem) else tuple(initial_work)
        if any(item.run_id != run.id for item in work):
            raise PublicationError("initial work must belong to the staged Run")
        if len({item.work_id for item in work}) != len(work):
            raise PublicationError("initial work ids must be unique")
        prepared = PreparedPublication(
            run=run,
            session=session_draft,
            initial_work=work,
            creation_payload=creation_payload or {},
            creation_actor=creation_actor or Actor.system(),
        )
        statement = tables.lifecycle_publications.insert().values(
            id=prepared.publication_id,
            run_id=run.id,
            session_id=run.session_id,
            status=_PREPARED,
            session_linked=False,
            staged_at=prepared.staged_at,
            document=publication_document(prepared),
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(statement)
        except sa_exc.IntegrityError as exc:
            raise FileExistsError(f"publication {prepared.publication_id} already exists") from exc
        return prepared

    def commit(self, prepared: PreparedPublication) -> tuple[RunOwnerHandle, PublicationReceipt]:
        """Commit Run, Session, initial work and outbox in one transaction."""
        owner = self._owners.acquire(
            prepared.run.id,
            prepared.publication_id,
            allow_unmaterialized=True,
        )
        if owner is None:
            raise OwnerBusyError(f"Run {prepared.run.id} already has a live owner")
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text("SELECT pg_advisory_xact_lock(hashtext(:session_id), 1)"),
                    {"session_id": prepared.run.session_id},
                )
                row = (
                    conn.execute(
                        sa.select(tables.lifecycle_publications)
                        .where(tables.lifecycle_publications.c.id == prepared.publication_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise PublicationError(  # noqa: TRY301  # transaction must roll back
                        f"publication {prepared.publication_id} is not staged"
                    )
                persisted = PreparedPublication.model_validate(row_document(row).get("prepared"))
                if persisted != prepared:
                    raise PublicationError(  # noqa: TRY301  # transaction must roll back
                        "publication manifest changed after staging"
                    )
                if row["status"] == _COMMITTED and row["session_linked"] is True:
                    receipt = PublicationReceipt.model_validate(row_document(row).get("receipt"))
                else:
                    self._owners.check_epoch(conn, owner)
                    _ensure_run(conn, prepared, owner)
                    _link_session(conn, prepared)
                    for item in prepared.initial_work:
                        _publish_work(conn, item)
                    receipt = PublicationReceipt(
                        publication_id=prepared.publication_id,
                        run_id=prepared.run.id,
                        session_id=prepared.run.session_id,
                        work_ids=tuple(item.work_id for item in prepared.initial_work),
                    )
                    conn.execute(
                        tables.lifecycle_publications.update()
                        .where(tables.lifecycle_publications.c.id == prepared.publication_id)
                        .values(
                            status=_COMMITTED,
                            session_linked=True,
                            committed_at=receipt.committed_at,
                            document=publication_document(prepared, receipt),
                        )
                    )
        except BaseException:
            self._owners.release(owner, OwnerReleaseReason.FAILED)
            raise
        return owner, receipt

    def recover(self) -> tuple[PublicationReceipt, ...]:
        """Finish valid prepared publications left by a process crash."""
        with self._engine.connect() as conn:
            documents = (
                conn.execute(
                    sa.select(tables.lifecycle_publications.c.document)
                    .where(tables.lifecycle_publications.c.status == _PREPARED)
                    .order_by(tables.lifecycle_publications.c.id.asc())
                )
                .scalars()
                .all()
            )
        receipts: list[PublicationReceipt] = []
        for document in documents:
            try:
                prepared = PreparedPublication.model_validate(
                    load_document(document).get("prepared")
                )
                owner, receipt = self.commit(prepared)
            except (LifecycleError, OSError, TypeError, ValueError, sa_exc.SQLAlchemyError):
                continue
            receipts.append(receipt)
            self._owners.release(owner, OwnerReleaseReason.RECOVERED)
        return tuple(receipts)

    def is_visible(self, run_id: str) -> bool:
        """Return whether the committed publication and inverse Session link agree."""
        with self._engine.connect() as conn:
            publication = (
                conn.execute(
                    sa.select(
                        tables.lifecycle_publications.c.status,
                        tables.lifecycle_publications.c.session_linked,
                        tables.lifecycle_publications.c.session_id,
                    ).where(tables.lifecycle_publications.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if publication is None:
                return _run_exists(conn, run_id)
            if publication["status"] != _COMMITTED or publication["session_linked"] is not True:
                return False
            session_document = conn.execute(
                sa.select(tables.sessions.c.document).where(
                    tables.sessions.c.id == publication["session_id"]
                )
            ).scalar_one_or_none()
        if session_document is None:
            return False
        return run_id in Session.model_validate(load_document(session_document)).run_ids

    def get_run(self, run_id: str) -> Run:
        """Read a visible Run from the PostgreSQL projection."""
        if not self.is_visible(run_id):
            raise FileNotFoundError(f"run {run_id} is not yet visible")
        run = PgRunRepository(self._engine).get(run_id)
        if run is None:
            raise FileNotFoundError(f"run {run_id} does not exist")
        return run


__all__ = ["PgPublicationStore"]
