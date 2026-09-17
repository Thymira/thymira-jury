"""The read-before-edit freshness policy shared by read_file, write_file and edit_file (F3.3).

:class:`ReadLedger` is a mutable, in-memory, per-registry record of "what content did this run
last read at this path" -- constructed once in :func:`~thymira.tools.builtins.run_python.
builtins_registry` and handed to the three tools that need it. It deliberately writes nothing
durable: a workspace file would be forgeable by ``run_python``, and an artifact-store record
would make ``read_file`` effectful and its declared ``side_effects=()`` capability a lie. The
ledger's absence of durability is a retained, named limitation (see the Agent Note), not an
oversight -- a process restart demands a fresh read, fail-closed, never fail-open.

:class:`FreshnessPolicy` is the configuration: whether the check applies at all. A tool
constructed directly (as most of this repository's tests still do) defaults it off, so every
pre-existing direct-construction test keeps its old behavior unchanged; the *production*
registry (:func:`~thymira.tools.builtins.run_python.builtins_registry`) turns it on explicitly.
That is what "a configured freshness policy" in the criterion text means: configured on for the
tool set real Runs use, configured off for a tool built ad hoc with no ledger to share.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

StaleReason = Literal["unread", "stale"]


@dataclass(slots=True)
class ReadLedger:
    """The digest last read at each ``(run_id, workspace, relative_path)`` this run has seen."""

    _last_read: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def record(self, *, run_id: str, workspace: str, relative_path: str, sha256: str) -> None:
        """Record that ``relative_path`` was just read (or written) at ``sha256``."""
        self._last_read[(run_id, workspace, relative_path)] = sha256

    def last_read_sha256(self, *, run_id: str, workspace: str, relative_path: str) -> str | None:
        """Return the digest this run last read at ``relative_path``, or ``None``."""
        return self._last_read.get((run_id, workspace, relative_path))


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Whether write_file/edit_file require a prior read_file of the current content."""

    require_read_before_edit: bool = True

    def stale_reason(
        self,
        ledger: ReadLedger,
        *,
        run_id: str,
        workspace: str,
        relative_path: str,
        existed_before: bool,
        current_sha256: str | None,
    ) -> StaleReason | None:
        """Return why a write/edit to ``relative_path`` must be refused, or ``None`` to proceed.

        A brand-new file (``existed_before`` is ``False``) never needs a prior read: there is
        nothing on disk yet to have read stale content of. An existing file this run has never
        read is ``"unread"``; one it read but that has since changed on disk is ``"stale"``.
        """
        if not self.require_read_before_edit or not existed_before:
            return None
        last = ledger.last_read_sha256(
            run_id=run_id, workspace=workspace, relative_path=relative_path
        )
        if last is None:
            return "unread"
        if last != current_sha256:
            return "stale"
        return None


__all__ = ["FreshnessPolicy", "ReadLedger", "StaleReason"]
