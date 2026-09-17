"""Local, single-writer persistence for one Run's evidence and projection.

``events.jsonl`` is the authoritative, hash-chained history. ``run.json`` is a convenience
projection rebuilt from that history whenever it is absent, malformed, or stale. This module
does not coordinate writers or make a transaction across the Run directory's files.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from thymira.events import (
    GENESIS_HASH,
    JsonlEventLog,
    StoragePermissionError,
    StoragePermissionEvidence,
    canonical_json,
    create_private_file,
    redact_export,
    redact_value,
    secure_directory,
    secure_file,
)
from thymira.schemas import (
    Actor,
    Decision,
    DecisionContext,
    Event,
    EventSurface,
    EventType,
    Run,
    RunCondition,
    RunStage,
    RunState,
    RunStatus,
    id_kind,
)
from thymira.state._atomic import fsync_directory, replace_with_retry
from thymira.state.repositories import Page, _decode_cursor, _encode_cursor, _validate_limit

if TYPE_CHECKING:
    from collections.abc import Callable

_EVENTS_FILE = "events.jsonl"
_PROJECTION_FILE = "run.json"
_MIRA_CONTEXT_DIR = "mira-context"
_EXPORTS_DIR = "exports"


class LocalRunStore:
    """Persist Run evidence under a local ``runs/<run_id>`` directory.

    ``append`` serialises concurrent writers to the same Run with an in-process lock, so two
    threads appending at once cannot corrupt the hash chain; a stale ``expected_version`` still
    loses the race with a clean ``ValueError`` instead of a torn write. A successful event append
    and a projection replacement remain separate file operations, so reopening the Run verifies
    the log and rebuilds the projection if needed.

    Args:
        root: Directory that contains one directory per Run.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._permission_evidence = secure_directory(self._root)
        self._run_locks_guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}

    @property
    def permission_evidence(self) -> StoragePermissionEvidence:
        """Return the verified OS permission evidence for this store's root directory."""
        return self._permission_evidence

    def _lock_for(self, run_id: str) -> threading.Lock:
        """Return this Run's append lock, creating it on first use."""
        with self._run_locks_guard:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._run_locks[run_id] = lock
            return lock

    def create(
        self,
        run: Run,
        *,
        actor: Actor,
        payload: dict[str, Any] | None = None,
    ) -> Run:
        """Create a Run directory and record its complete typed creation fact.

        Raises:
            FileExistsError: The Run directory already exists.
        """
        run_dir = self._run_dir(run.id)
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            msg = f"run {run.id} already exists"
            raise FileExistsError(msg) from exc
        secure_directory(run_dir)
        secure_directory(run_dir / _MIRA_CONTEXT_DIR)
        secure_directory(run_dir / _EXPORTS_DIR)

        log = JsonlEventLog(run_dir / _EVENTS_FILE, run.id)
        # The persisted identity is authoritative; caller metadata cannot replace it.
        creation_payload = {**(payload or {}), "run": run.model_dump(mode="json")}
        log.append(
            EventType.RUN_STARTED,
            actor,
            creation_payload,
            producer="thymira.state",
            producer_version="0.1",
        )
        secure_file(run_dir / _EVENTS_FILE)
        self._rebuild_projection(run.id, log.events())
        return self.get(run.id)

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
        hydrate: Callable[[Run], Run] | None = None,
    ) -> Page[Run]:
        """Return filtered Runs ordered by creation time and id using an opaque cursor.

        Each ``run.json`` records only the Run's *creation* status (always
        :attr:`~thymira.schemas.RunStatus.CREATED`); the store reconstructs bodies from that
        frozen fact and cannot know a Run's *current* status, which is derived from the event
        chain by :class:`~thymira.core.control_plane.RunController` above this layer. A caller
        that wants ``status`` to mean the current status therefore passes ``hydrate``, a callback
        returning a view of a Run with its live status (and timestamps) resolved.

        When given, ``hydrate`` is applied to every Run *before* the ``status`` filter and
        *before* ordering and paging. Doing it first is what keeps a page boundary and the next
        page's filter in agreement: the ``next_cursor`` is derived from the last *selected* item,
        so filtering on the frozen status here (or hydrating only the returned page in the caller)
        would size the page wrong and hand back a cursor that the next page's hydrated filter
        excludes. The callback must preserve a Run's ordering identity (``created_at`` and ``id``)
        and its ``project_id``; it only supplies the current status the store cannot compute. This
        is a deliberate inversion of control -- the store owns ordering and paging, the caller
        owns what a Run's current status is.

        Args:
            project_id: When set, keep only Runs in this project.
            status: When set, keep only Runs whose status matches (hydrated when ``hydrate`` is
                given, otherwise the frozen creation status the store recorded).
            limit: Maximum number of Runs to return in the page.
            cursor: Opaque cursor from a previous page's ``next_cursor``.
            hydrate: Optional callback resolving each Run's current status view before filtering.
        """
        _validate_limit(limit)
        runs = [self.get(path.name) for path in self._root.iterdir() if path.is_dir()]
        if project_id is not None:
            runs = [run for run in runs if run.project_id == project_id]
        if hydrate is not None:
            runs = [hydrate(run) for run in runs]
        if status is not None:
            runs = [run for run in runs if run.status is status]
        ordered = sorted(runs, key=lambda run: (run.created_at, run.id))
        if cursor is not None:
            key = _decode_cursor(cursor)
            ordered = [run for run in ordered if (run.created_at, run.id) > key]
        selected = ordered[:limit]
        next_cursor = _encode_cursor(selected[-1]) if len(ordered) > limit else None
        return Page(items=tuple(selected), next_cursor=next_cursor)

    def get(self, run_id: str) -> Run:
        """Return the Run reconstructed from its verified authoritative history."""
        events = self._verified_events(run_id)
        projection = self._projection_from_events(run_id, events)
        if self._read_projection(run_id) != projection:
            self._write_projection(run_id, projection)
        return Run.model_validate(projection["run"])

    def version(self, run_id: str) -> int:
        """Return the current event-derived version for ``run_id``."""
        events = self._verified_events(run_id)
        projection = self._projection_from_events(run_id, events)
        if self._read_projection(run_id) != projection:
            self._write_projection(run_id, projection)
        return int(projection["version"])

    def append(
        self,
        run_id: str,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        expected_version: int | None,
        subject_id: str | None = None,
        producer: str = "thymira.state",
        producer_version: str = "0.1",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append one typed fact when the caller's event-derived version is current.

        ``expected_version=None`` appends at whatever the current version is, resolved *inside*
        this Run's lock. It is for a caller that only ever wanted "record this fact now" and has
        no read-modify-write of its own to protect -- ``thymira.core.RunEventLog``, which read the
        version back purely to satisfy this signature and so could only ever lose a race it had no
        stake in. An integer keeps the optimistic check unchanged: a caller that decided something
        *from* the history it read is still told when that history moved underneath it.

        Raises:
            ValueError: The requested version is stale or the evidence cannot be opened.
        """
        if expected_version is not None and (
            not isinstance(expected_version, int) or isinstance(expected_version, bool)
        ):
            raise TypeError("expected_version must be an integer")

        with self._lock_for(run_id):
            events = self._verified_events(run_id)
            current_version = len(events)
            if expected_version is not None and expected_version != current_version:
                msg = (
                    f"run {run_id}: expected version {expected_version}, "
                    f"current version is {current_version}"
                )
                raise ValueError(msg)

            log = self._event_log(run_id)
            event = log.append(
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
            )
            secure_file(log.path)
            self._rebuild_projection(run_id, log.events())
        return event

    def events(self, run_id: str) -> list[Event]:
        """Return every verified event for ``run_id`` in sequence order."""
        return self._verified_events(run_id)

    def write_export(self, run_id: str, name: str, payload: object) -> Path:
        """Write a redacted, non-authoritative export under the Run's exports directory.

        The embedded event chain of an assurance bundle is omitted from this presentation copy;
        the canonical ``events.jsonl`` remains the separately verified evidence source. This keeps
        cryptographic event identity intact while ensuring a client-facing export cannot carry a
        second readable copy of raw event payloads.

        Raises:
            ValueError: The name is invalid or the payload cannot be safely redacted.
        """
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("export name must be one JSON file name")
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run {run_id} does not exist")
        secure_directory(run_dir)
        exports_dir = run_dir / _EXPORTS_DIR
        secure_directory(exports_dir)
        target = exports_dir / name
        temporary: Path | None = None
        try:
            safe_payload = redact_export(_export_payload(payload))
            rendered = (canonical_json(safe_payload) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("export payload could not be safely redacted") from exc
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=exports_dir,
                prefix=f".{target.stem}-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                # Secure the empty temporary before any redacted payload bytes are written.
                secure_file(temporary)
                handle.write(rendered)
                handle.flush()
            replace_with_retry(temporary, target)
            secure_file(target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return target

    def create_export(self, run_id: str, name: str, payload: object) -> Path:
        """Create a derived export once, accepting only an exact-byte replay.

        Runtime catalog manifests are immutable evidence snapshots. A repeated publication of the
        same selection is idempotent, while a different payload under the same versioned name is a
        conflict and must never replace the bytes a fresh consumer may already be reading.
        """
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("export name must be one JSON file name")
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run {run_id} does not exist")
        target = run_dir / _EXPORTS_DIR / name
        expected = (canonical_json(payload) + "\n").encode("utf-8")
        with self._lock_for(run_id):
            try:
                with target.open("xb") as stream:
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if target.read_bytes() == expected:
                    return target
                raise FileExistsError(
                    f"export already exists with different content: {name}"
                ) from None
        return target

    def read_export(self, run_id: str, name: str) -> object | None:
        """Return a previously written export, or ``None`` when it was never produced.

        Reading never creates the export: a caller that finds ``None`` reports the absence
        instead of regenerating derived state from a read path.

        Raises:
            ValueError: ``name`` is not one JSON file name.
        """
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("export name must be one JSON file name")
        run_dir = self._run_dir(run_id)
        secure_directory(run_dir)
        secure_directory(run_dir / _EXPORTS_DIR)
        target = run_dir / _EXPORTS_DIR / name
        if not target.is_file():
            return None
        secure_file(target)
        return json.loads(target.read_text(encoding="utf-8"))

    def create_context(
        self,
        context: DecisionContext,
        *,
        actor: Actor,
        expected_version: int,
    ) -> Event:
        """Persist one immutable MIRA context snapshot and its creation event.

        The snapshot file and event append are intentionally separate local file operations.
        A caller must handle a failed append after a successful immutable snapshot write.

        Raises:
            FileExistsError: The context id has already been persisted for this Run.
            ValueError: The expected version is stale or the context id has the wrong prefix.
        """
        if id_kind(context.id) != "context":
            raise ValueError(f"context.id must start with 'context_': {context.id!r}")
        events = self._verified_events(context.run_id)
        current_version = len(events)
        if expected_version != current_version:
            msg = (
                f"run {context.run_id}: expected version {expected_version}, "
                f"current version is {current_version}"
            )
            raise ValueError(msg)

        safe_context = DecisionContext.model_validate(
            redact_value(context.model_dump(mode="python"))
        )
        target = self._run_dir(context.run_id) / _MIRA_CONTEXT_DIR / f"{context.id}.json"
        try:
            create_private_file(
                target,
                (canonical_json(safe_context.to_json_dict()) + "\n").encode("utf-8"),
            )
        except FileExistsError as exc:
            raise FileExistsError(f"MIRA context {context.id} already exists") from exc

        return self.append(
            context.run_id,
            EventType.MIRA_CONTEXT_CREATED,
            actor,
            {"context": safe_context.to_json_dict()},
            expected_version=expected_version,
            subject_id=context.id,
            producer="thymira.mira",
            producer_version="0.1",
        )

    def _run_dir(self, run_id: str) -> Path:
        """Return the validated directory path for a Run id."""
        try:
            kind = id_kind(run_id)
        except ValueError as exc:
            msg = f"invalid run_id {run_id!r}"
            raise ValueError(msg) from exc
        if kind != "run":
            raise ValueError(f"invalid run_id {run_id!r}")
        return self._root / run_id

    def _event_log(self, run_id: str) -> JsonlEventLog:
        """Open ``run_id``'s log after confirming its directory exists."""
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run {run_id} does not exist")
        try:
            secure_directory(run_dir)
            path = run_dir / _EVENTS_FILE
            if path.exists():
                secure_file(path)
            log = JsonlEventLog(path, run_id)
        except ValueError as exc:
            msg = f"cannot open run {run_id}: events.jsonl is invalid: {exc}"
            raise ValueError(msg) from exc
        return log

    def _verified_events(self, run_id: str) -> list[Event]:
        """Open and read an authoritative log, rejecting empty or invalid histories."""
        events = self._event_log(run_id).events()
        if not events:
            raise ValueError(f"cannot open run {run_id}: events.jsonl has no creation event")
        return events

    def _rebuild_projection(self, run_id: str, events: list[Event]) -> None:
        """Derive and atomically replace the convenience projection from verified events."""
        self._write_projection(run_id, self._projection_from_events(run_id, events))

    def _projection_from_events(self, run_id: str, events: list[Event]) -> dict[str, Any]:
        """Build the full projection from the immutable creation fact and event head."""
        first = events[0]
        if first.type is not EventType.RUN_STARTED:
            raise ValueError(f"cannot open run {run_id}: first event must be run.started")
        run_data = first.payload.get("run")
        try:
            run = Run.model_validate(run_data)
        except (TypeError, ValueError) as exc:
            msg = f"cannot open run {run_id}: creation event does not contain a valid Run"
            raise ValueError(msg) from exc
        if run.id != run_id:
            raise ValueError(f"cannot open run {run_id}: creation event contains run {run.id}")
        run = self._run_with_event_metadata(run, events)
        return {
            "run_id": run_id,
            "version": len(events),
            "last_event_hash": events[-1].hash or GENESIS_HASH,
            "run": run.model_dump(mode="json"),
            "run_state": self._run_state_from_events(run_id, events).model_dump(mode="json"),
        }

    @staticmethod
    def _run_with_event_metadata(run: Run, events: list[Event]) -> Run:
        """Fold child record identifiers from the authoritative event history into a Run."""
        agent_ids: list[str] = []
        task_ids: list[str] = []
        tool_call_ids: list[str] = []
        experiment_ids: list[str] = []
        artifact_ids: list[str] = []
        finding_ids: list[str] = []
        policy_decision_id: str | None = None
        final_decision: Decision | None = None

        def add_unique(target: list[str], value: object) -> None:
            if isinstance(value, str) and value not in target:
                target.append(value)

        for event in events:
            payload = event.payload
            if event.type is EventType.AGENT_STARTED:
                add_unique(agent_ids, event.subject_id)
                add_unique(task_ids, payload.get("task_id"))
            elif event.type is EventType.TOOL_STARTED:
                add_unique(tool_call_ids, event.subject_id)
            elif event.type is EventType.TOOL_COMPLETED:
                values = payload.get("artifact_ids")
                if isinstance(values, list | tuple):
                    for value in values:
                        add_unique(artifact_ids, value)
            elif event.type is EventType.ARTIFACT_CREATED:
                add_unique(artifact_ids, event.subject_id)
            elif event.type is EventType.EXPERIMENT_STARTED:
                add_unique(experiment_ids, event.subject_id)
            elif event.type is EventType.AUDIT_FINDING:
                add_unique(finding_ids, event.subject_id)
            elif event.type is EventType.POLICY_DECISION:
                decision = payload.get("decision")
                if isinstance(payload.get("id"), str):
                    policy_decision_id = payload["id"]
                with suppress(TypeError, ValueError):
                    final_decision = Decision(decision)

        return run.model_copy(
            update={
                "agent_ids": tuple(agent_ids),
                "task_ids": tuple(task_ids),
                "tool_call_ids": tuple(tool_call_ids),
                "experiment_ids": tuple(experiment_ids),
                "artifact_ids": tuple(artifact_ids),
                "finding_ids": tuple(finding_ids),
                "policy_decision_id": policy_decision_id,
                "final_decision": final_decision,
            }
        )

    def _run_state_from_events(self, run_id: str, events: list[Event]) -> RunState:
        """Derive the latest validated operational state from transition facts only."""
        state = RunState(stage=RunStage.CREATED, condition=RunCondition.ACTIVE)
        for event in events:
            if event.type is not EventType.RUN_TRANSITIONED:
                continue
            try:
                state = RunState.model_validate_json(canonical_json(event.payload["state"]))
            except (KeyError, TypeError, ValueError) as exc:
                msg = f"cannot open run {run_id}: transition event lacks a valid RunState"
                raise ValueError(msg) from exc
        return state

    def _read_projection(self, run_id: str) -> dict[str, Any] | None:
        """Read a projection without trusting it; malformed content is simply unusable."""
        path = self._run_dir(run_id) / _PROJECTION_FILE
        try:
            if path.exists():
                secure_file(path)
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, StoragePermissionError):
            return None
        return value if isinstance(value, dict) else None

    def _write_projection(self, run_id: str, projection: dict[str, Any]) -> None:
        """Replace ``run.json`` atomically within its Run directory."""
        run_dir = self._run_dir(run_id)
        target = run_dir / _PROJECTION_FILE
        secure_directory(run_dir)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=run_dir,
            prefix=".run-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            secure_file(temporary)
            handle.write(canonical_json(projection) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            replace_with_retry(temporary, target)
            secure_file(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        try:
            readback = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"run {run_id}: projection read-back failed") from exc
        if readback != projection:
            raise ValueError(f"run {run_id}: projection read-back differs from derived state")
        fsync_directory(run_dir)


def _export_payload(payload: object) -> object:
    """Remove embedded canonical events before an export copy is redacted."""
    if not isinstance(payload, dict) or "verified_events" not in payload:
        return payload
    safe_payload = dict(payload)
    safe_payload.pop("verified_events", None)
    # The original digest covered the omitted event chain and is therefore no longer valid.
    safe_payload["bundle_sha256"] = None
    return safe_payload
