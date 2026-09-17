"""Typed PostgreSQL lifecycle row encoders and validation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa

from thymira.events import canonical_json
from thymira.schemas import (
    ControlInput,
    PreparedPublication,
    PublicationReceipt,
    TurnEnded,
    WorkItem,
    WorkState,
)
from thymira.state._lifecycle_files import sha256_json
from thymira.state.postgres import tables
from thymira.state.postgres._serde import load_document

if TYPE_CHECKING:
    from sqlalchemy import RowMapping
    from sqlalchemy.engine import Connection


def publication_document(
    prepared: PreparedPublication,
    receipt: PublicationReceipt | None = None,
) -> str:
    """Encode one private publication manifest and optional durable receipt."""
    value: dict[str, object] = {"prepared": prepared.to_json_dict()}
    if receipt is not None:
        value["receipt"] = receipt.to_json_dict()
    return canonical_json(value)


def row_document(row: RowMapping) -> dict[str, object]:
    """Parse a lifecycle row's authoritative JSON document."""
    return load_document(cast("str", row["document"]))


def work_values(item: WorkItem, *, result_document: str | None = None) -> dict[str, object]:
    """Return projection columns and the canonical WorkItem document for one row."""
    values: dict[str, object] = {
        "run_id": item.run_id,
        "kind": item.kind,
        "state": item.state.value,
        "attempt": item.attempt,
        "claim_owner": item.claim_owner,
        "claim_token": item.claim_token,
        "claim_expires_at": item.claim_expires_at,
        "dispatch_state": item.dispatch_state.value,
        "idempotency_key": item.idempotency_key,
        "document": canonical_json(item.to_json_dict()),
    }
    if result_document is not None:
        values["result_document"] = result_document
    return values


def control_values(command: ControlInput) -> dict[str, object]:
    """Return projection columns for one authenticated control input."""
    return {
        "id": command.input_id,
        "run_id": command.run_id,
        "session_id": command.session_id,
        "requested_lane": command.requested_lane.value,
        "effective_lane": command.effective_lane.value if command.effective_lane else None,
        "kind": command.kind.value,
        "state": command.state.value,
        "idempotency_key": command.idempotency_key,
        "proof_id": command.authority.proof_id,
        "authority_digest": sha256_json(command.authority.signing_payload()),
        "enqueued_at": command.enqueued_at,
        "document": canonical_json(command.to_json_dict()),
    }


def validate_turn_ended(
    conn: Connection,
    run_id: str,
    payload: dict[str, Any] | None,
) -> None:
    """Require a closed turn payload whose referenced work is settled."""
    ended = TurnEnded.model_validate(payload)
    if ended.run_id != run_id:
        raise ValueError("turn.ended payload targets another Run")
    for work_id in ended.work_ids:
        document = conn.execute(
            sa.select(tables.lifecycle_work.c.document).where(tables.lifecycle_work.c.id == work_id)
        ).scalar_one_or_none()
        if document is None:
            raise ValueError(f"turn.ended references unknown work item {work_id}")
        item = WorkItem.model_validate(load_document(document))
        if item.run_id != run_id or item.state not in {WorkState.SETTLED, WorkState.CANCELLED}:
            raise ValueError("turn.ended requires every referenced work item to be settled")


__all__ = [
    "control_values",
    "publication_document",
    "row_document",
    "validate_turn_ended",
    "work_values",
]
