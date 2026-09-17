"""Per-run child-record persistence for the local runtime backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.events import canonical_json, create_private_file, secure_directory, secure_file
from thymira.schemas import Agent, Experiment, Task, ToolCall
from thymira.state._atomic import replace_with_retry

if TYPE_CHECKING:
    from thymira.schemas import Id

Record = Agent | Task | ToolCall | Experiment
_KINDS: dict[str, type[Record]] = {
    "agent": Agent,
    "task": Task,
    "tool_call": ToolCall,
    "experiment": Experiment,
}
_ORDER_FILENAME = "order.json"


def _validate_plain_id(value: Id) -> None:
    """Reject path-like values before using an id in local storage."""
    if Path(value).name != value or value == _ORDER_FILENAME.removesuffix(".json"):
        msg = "record identifiers must be plain ids"
        raise ValueError(msg)


@runtime_checkable
class RecordRepository(Protocol):
    """Persist child records keyed by run and record id."""

    def save(self, record: Record) -> Record:
        """Persist a child record and return it."""
        ...

    def get(self, run_id: Id, record_id: Id, kind: str) -> Record | None:
        """Return one child record, if present."""
        ...

    def list_for_run(self, run_id: Id, kind: str) -> tuple[Record, ...]:
        """Return records for a run in insertion order."""
        ...


def _kind(record: Record) -> str:
    """Return the stable storage kind for a record."""
    if isinstance(record, Agent):
        return "agent"
    if isinstance(record, Task):
        return "task"
    if isinstance(record, ToolCall):
        return "tool_call"
    return "experiment"


class LocalRecordRepository:
    """Store Agent, Task, ToolCall and Experiment records as canonical JSON files."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "records"
        secure_directory(self._root)

    @property
    def transaction_root(self) -> Path:
        """Return the directory a local transaction must restore on failure."""
        return self._root

    def _path(self, run_id: Id, record_id: Id, kind: str) -> Path:
        """Return a contained path after validating the record kind."""
        if kind not in _KINDS:
            msg = f"unknown record kind: {kind!r}"
            raise ValueError(msg)
        _validate_plain_id(run_id)
        _validate_plain_id(record_id)
        directory = self._root / run_id / kind
        secure_directory(directory)
        return directory / f"{record_id}.json"

    def path_for(self, record: Record) -> Path:
        """Return the persisted path for ``record`` without writing it."""
        return self._root / record.run_id / _kind(record) / f"{record.id}.json"

    def order_path_for(self, record: Record) -> Path:
        """Return the insertion-order index path for ``record`` without writing it."""
        return self._root / record.run_id / _kind(record) / _ORDER_FILENAME

    def _order_path(self, run_id: Id, kind: str) -> Path:
        """Return an order-index path after validating its parent identifiers."""
        if kind not in _KINDS:
            msg = f"unknown record kind: {kind!r}"
            raise ValueError(msg)
        _validate_plain_id(run_id)
        return self._root / run_id / kind / _ORDER_FILENAME

    def _load_order(self, run_id: Id, kind: str) -> list[str]:
        """Load the persisted order or derive one for a legacy directory."""
        order_path = self._order_path(run_id, kind)
        directory = order_path.parent
        if not order_path.exists():
            if not directory.exists():
                return []
            return sorted(
                path.stem for path in directory.glob("*.json") if path.name != _ORDER_FILENAME
            )
        secure_file(order_path)
        payload = json.loads(order_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
            msg = f"invalid record order index: {order_path}"
            raise ValueError(msg)
        order = list(payload)
        if len(order) != len(set(order)):
            msg = f"duplicate record in order index: {order_path}"
            raise ValueError(msg)
        for record_id in order:
            _validate_plain_id(record_id)
        return order

    @staticmethod
    def _write_atomic(path: Path, payload: str) -> None:
        """Write a UTF-8 payload through the repository's replace-on-success seam."""
        secure_directory(path.parent)
        tmp = path.with_suffix(".json.tmp")
        if tmp.exists():
            secure_file(tmp)
            tmp.write_text(payload, encoding="utf-8", newline="\n")
        else:
            create_private_file(tmp, payload.encode("utf-8"))
        try:
            replace_with_retry(tmp, path)
            secure_file(path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _restore_file(path: Path, existed: bool, contents: bytes | None) -> None:
        """Restore one file after a multi-file local write fails."""
        if existed:
            if contents is None:
                msg = f"missing snapshot contents for {path}"
                raise RuntimeError(msg)
            secure_directory(path.parent)
            if path.exists():
                secure_file(path)
                path.write_bytes(contents)
                secure_file(path)
            else:
                create_private_file(path, contents)
        elif path.exists():
            path.unlink()

    def save(self, record: Record) -> Record:
        """Persist a child record and return it."""
        path = self._path(record.run_id, record.id, _kind(record))
        order_path = self.order_path_for(record)
        order = self._load_order(record.run_id, _kind(record))
        if record.id not in order:
            order.append(record.id)
        file_states_list: list[tuple[Path, bool, bytes | None]] = []
        for target in (path, order_path):
            secure_directory(target.parent)
            existed = target.exists()
            if existed:
                secure_file(target)
            file_states_list.append((target, existed, target.read_bytes() if existed else None))
        file_states = tuple(file_states_list)
        try:
            self._write_atomic(path, canonical_json(record.to_json_dict()) + "\n")
            self._write_atomic(order_path, canonical_json(order) + "\n")
        except BaseException:
            for target, existed, contents in reversed(file_states):
                self._restore_file(target, existed, contents)
            raise
        return record

    def get(self, run_id: Id, record_id: Id, kind: str) -> Record | None:
        """Return one child record, if present."""
        path = self._path(run_id, record_id, kind)
        if not path.exists():
            return None
        secure_file(path)
        return _KINDS[kind].model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_for_run(self, run_id: Id, kind: str) -> tuple[Record, ...]:
        """Return records for a run in insertion order."""
        order = self._load_order(run_id, kind)
        directory = self._order_path(run_id, kind).parent
        if not directory.exists():
            return ()
        model = _KINDS[kind]
        indexed = set(order)
        extras = sorted(
            path.stem
            for path in directory.glob("*.json")
            if path.name != _ORDER_FILENAME and path.stem not in indexed
        )
        records: list[Record] = []
        for record_id in (*order, *extras):
            path = directory / f"{record_id}.json"
            if not path.exists():
                msg = f"record order index references missing record: {path}"
                raise ValueError(msg)
            secure_file(path)
            records.append(model.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        return tuple(records)


__all__ = ["LocalRecordRepository", "Record", "RecordRepository"]
