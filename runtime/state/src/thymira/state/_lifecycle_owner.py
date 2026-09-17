"""Durable epoch and live OS ownership for one local Run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from thymira.schemas import OwnerReleaseReason, RunOwnerHandle, utc_now
from thymira.state._lifecycle_lock import FileLock
from thymira.state.lifecycle_errors import LifecycleError, OwnerBusyError, StaleOwnerError

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.state._lifecycle_files import LifecycleFiles


class OwnerRegistry:
    """Acquire, fence and release per-Run owners using an OS lock plus durable epoch."""

    def __init__(self, files: LifecycleFiles, run_exists: Callable[[str], bool]) -> None:
        self._files = files
        self._run_exists = run_exists

    def acquire(
        self,
        run_id: str,
        claimant_id: str,
        *,
        allow_unmaterialized: bool,
    ) -> RunOwnerHandle | None:
        """Acquire a Run lock and persist the next monotonic owner epoch."""
        if not claimant_id.strip():
            raise ValueError("claimant_id must not be empty")
        if not allow_unmaterialized and not self._run_exists(run_id):
            return None
        lock = FileLock(self._files.locks / f"run-{run_id}.lock")
        try:
            lock.__enter__()
        except OwnerBusyError:
            return None
        metadata_path = self._files.owners / f"{run_id}.json"
        metadata = self._files.read_json(metadata_path) or {}
        prior_epoch = metadata.get("epoch", 0)
        if not isinstance(prior_epoch, int) or isinstance(prior_epoch, bool) or prior_epoch < 0:
            lock.__exit__(None, None, None)
            raise LifecycleError(f"Run {run_id} has invalid owner epoch metadata")
        owner = RunOwnerHandle(run_id=run_id, owner_id=claimant_id, epoch=prior_epoch + 1)
        try:
            self._files.write_json(
                metadata_path,
                {
                    "run_id": owner.run_id,
                    "owner_id": owner.owner_id,
                    "epoch": owner.epoch,
                    "acquired_at": owner.acquired_at.isoformat(),
                    "active": True,
                },
            )
        except BaseException:
            # A failed durable epoch write must never strand the process-owned OS lock.
            lock.__exit__(None, None, None)
            raise
        object.__setattr__(owner, "_lock_resource", lock)
        object.__setattr__(owner, "_repository_root", str(self._files.root.resolve()))
        return owner

    def release(self, owner: RunOwnerHandle, reason: OwnerReleaseReason) -> None:
        """Persist a release reason and always release the process-owned OS lock."""
        self.assert_live(owner)
        metadata_path = self._files.owners / f"{owner.run_id}.json"
        metadata = self._files.read_json(metadata_path) or {}
        self._assert_metadata(owner, metadata)
        lock = cast("FileLock", owner._lock_resource)  # noqa: SLF001  # live lock resource
        try:
            self._files.write_json(
                metadata_path,
                {
                    **metadata,
                    "active": False,
                    "released_at": utc_now().isoformat(),
                    "release_reason": reason.value,
                },
            )
        finally:
            # A metadata failure may require recovery, but it must not leave the OS lock held.
            lock.__exit__(None, None, None)
            object.__setattr__(owner, "_lock_resource", None)

    def assert_live(self, owner: RunOwnerHandle, *, run_id: str | None = None) -> None:
        """Refuse forged, released or stale public owner identities."""
        if run_id is not None and owner.run_id != run_id:
            raise StaleOwnerError("owner handle targets another Run")
        owner_lock = owner._lock_resource  # noqa: SLF001  # live process-owned resource
        if (
            owner._repository_root != str(self._files.root.resolve())  # noqa: SLF001  # anti-forgery
            or not isinstance(owner_lock, FileLock)
            or owner_lock._file is None  # noqa: SLF001  # anti-forgery
        ):
            raise StaleOwnerError("Run owner handle does not hold a live process lock")
        metadata = self._files.read_json(self._files.owners / f"{owner.run_id}.json") or {}
        self._assert_metadata(owner, metadata)

    @staticmethod
    def _assert_metadata(owner: RunOwnerHandle, metadata: dict[str, Any]) -> None:
        if (
            metadata.get("active") is not True
            or metadata.get("owner_id") != owner.owner_id
            or metadata.get("epoch") != owner.epoch
        ):
            raise StaleOwnerError("Run owner fencing epoch is stale")


__all__ = ["OwnerRegistry"]
