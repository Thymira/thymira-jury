"""Deterministic execution keys and the MVP idempotency journal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json, sha256_text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class DuplicateExecutionError(RuntimeError):
    """Raised when an execution key is recorded more than once."""


def compute_execution_key(
    *,
    tool: str,
    input_fingerprints: Sequence[str],
    parameters: Mapping[str, Any],
    phase_revision: str,
    decision_fingerprint: str,
) -> str:
    """Return a stable SHA-256 key for one execution request.

    The input order is preserved because it may be meaningful to a tool. Mapping keys are
    canonicalised by :func:`thymira.events.canonical_json`.
    """
    payload = {
        "tool": tool,
        "input_fingerprints": list(input_fingerprints),
        "parameters": parameters,
        "phase_revision": phase_revision,
        "decision_fingerprint": decision_fingerprint,
    }
    return sha256_text(canonical_json(payload))


class IdempotencyJournal:
    """Keep completed execution keys and API Run keys in memory."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, ...]] = {}
        self._run_records: dict[str, str] = {}

    def seen(self, key: str) -> bool:
        """Return whether an execution key has already been recorded."""
        return key in self._records

    def record(self, key: str, artifact_ids: Sequence[str]) -> None:
        """Record a completed execution, refusing a duplicate key."""
        if self.seen(key):
            msg = f"execution key already recorded: {key}"
            raise DuplicateExecutionError(msg)
        self._records[key] = tuple(artifact_ids)

    def run_id_for(self, key: str) -> str | None:
        """Return the Run previously associated with an API idempotency key."""
        return self._run_records.get(key)

    def record_run(self, key: str, run_id: str) -> None:
        """Associate one API idempotency key with a created Run."""
        if key in self._run_records:
            msg = f"idempotency key already recorded: {key}"
            raise DuplicateExecutionError(msg)
        self._run_records[key] = run_id


__all__ = ["DuplicateExecutionError", "IdempotencyJournal", "compute_execution_key"]
