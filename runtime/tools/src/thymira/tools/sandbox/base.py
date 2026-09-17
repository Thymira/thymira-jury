"""The replaceable contract for executing code under a confinement policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from thymira.schemas import SandboxEnforcement

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from thymira.schemas import SandboxMode
    from thymira.tools.sandbox.termination import TerminationEvidence


@dataclass(frozen=True, slots=True)
class ResolvedExecutionSpec:
    """Runtime-owned facts about how one execution was actually confined.

    Derived from the backend's own configuration at the moment it built the argv for this call --
    never echoed from the caller's request. ``unenforced`` names the controls this backend cannot
    apply under the requested mode (e.g. ``workspace_quota`` for a bind mount, or the full set for
    the local backend), so a control such as MIRA's A29 can tell "not enforced, honestly declared"
    apart from "silently missing".
    """

    backend: str
    image: str | None
    workspace_mount: str
    network: str
    memory: str | None
    cpus: str | None
    pids_limit: int | None
    environment_names: tuple[str, ...]
    excluded_environment_names: tuple[str, ...]
    unenforced: tuple[str, ...]
    output_limit_bytes: int | None = None
    workspace_quota_bytes: int | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the ``tool.completed`` event payload."""
        return {
            "backend": self.backend,
            "image": self.image,
            "workspace_mount": self.workspace_mount,
            "network": self.network,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "environment_names": list(self.environment_names),
            "excluded_environment_names": list(self.excluded_environment_names),
            "unenforced": list(self.unenforced),
            "output_limit_bytes": self.output_limit_bytes,
            "workspace_quota_bytes": self.workspace_quota_bytes,
        }


@dataclass(frozen=True, slots=True)
class StagedInput:
    """Runtime-owned bytes made available to a child at a workspace-relative path.

    A staged input is deliberately content-addressed by the caller before it reaches a backend:
    the quota backend can charge and copy these bytes in its private volume, while the unquotaed
    backends use the same contract for their ordinary host staging path.
    """

    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        """Reject paths that could be interpreted differently by host and worker filesystems."""
        if not isinstance(self.relative_path, str) or not self.relative_path:
            raise ValueError("staged input path must be a non-empty string")
        if "\\" in self.relative_path:
            raise ValueError("staged input path must use POSIX separators")
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(
                f"staged input path must be relative and contained: {self.relative_path!r}"
            )
        if path.as_posix() != self.relative_path:
            raise ValueError(f"staged input path is not canonical: {self.relative_path!r}")
        if not isinstance(self.content, bytes):
            raise TypeError("staged input content must be bytes")


def validate_staged_inputs(
    inputs: tuple[StagedInput, ...] | list[StagedInput] | None,
) -> tuple[StagedInput, ...]:
    """Validate one immutable staging manifest and reject case-fold collisions."""
    values = tuple(inputs or ())
    folded: set[str] = set()
    for item in values:
        if not isinstance(item, StagedInput):
            raise TypeError("staged inputs must contain StagedInput values")
        folded_path = item.relative_path.casefold()
        if folded_path in folded:
            raise ValueError(f"staged input path collides case-insensitively: {item.relative_path}")
        folded.add(folded_path)
    return values


@dataclass(frozen=True, slots=True)
class WorkspaceQuotaEvidence:
    """Trusted observations for one quota-backed workspace publication."""

    mechanism: str
    requested_bytes: int
    observed_capacity_bytes: int | None
    observed_filesystem: str | None
    inode_limit: int
    observed_inode_capacity: int | None
    source_logical_bytes: int | None
    staged_input_logical_bytes: int | None
    final_logical_bytes: int | None
    exported_logical_bytes: int | None
    source_entries: int | None
    staged_input_entries: int | None
    final_entries: int | None
    exported_entries: int | None
    source_tree_sha256: str | None
    staged_input_tree_sha256: str | None
    final_tree_sha256: str | None
    exported_tree_sha256: str | None
    volume_inspected: bool
    worker_mount_inspected: bool
    publication: str
    cleanup: dict[str, bool]

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for the durable tool event."""
        payload: dict[str, Any] = {
            "mechanism": self.mechanism,
            "requested_bytes": self.requested_bytes,
            "observed_capacity_bytes": self.observed_capacity_bytes,
            "observed_filesystem": self.observed_filesystem,
            "inode_limit": self.inode_limit,
            "observed_inode_capacity": self.observed_inode_capacity,
            "source_logical_bytes": self.source_logical_bytes,
            "final_logical_bytes": self.final_logical_bytes,
            "exported_logical_bytes": self.exported_logical_bytes,
            "source_entries": self.source_entries,
            "final_entries": self.final_entries,
            "exported_entries": self.exported_entries,
            "source_tree_sha256": self.source_tree_sha256,
            "final_tree_sha256": self.final_tree_sha256,
            "exported_tree_sha256": self.exported_tree_sha256,
            "volume_inspected": self.volume_inspected,
            "worker_mount_inspected": self.worker_mount_inspected,
            "publication": self.publication,
            "cleanup": dict(self.cleanup),
        }
        if self.staged_input_logical_bytes is not None:
            payload["staged_input_logical_bytes"] = self.staged_input_logical_bytes
            payload["staged_input_entries"] = self.staged_input_entries
            payload["staged_input_tree_sha256"] = self.staged_input_tree_sha256
        return payload


@dataclass(frozen=True, slots=True)
class SandboxRun:
    """The bounded facts returned by one sandbox execution.

    ``termination`` is the sixth orthogonal fact, alongside requested mode, actual mode,
    enforcement, exit outcome and cleanup result: *how* the execution ended, observed through a
    channel the child cannot write to (F6.8). A backend that reaches a child always attaches it.
    """

    stdout: str
    stderr: str
    exit_code: int
    mode: SandboxMode
    enforcement: SandboxEnforcement
    spec: ResolvedExecutionSpec | None = None
    cleanup_confirmed: bool | None = None
    termination: TerminationEvidence | None = None
    timed_out: bool = False
    """Whether this sandbox's own deadline expired before the child completed."""
    quota_evidence: WorkspaceQuotaEvidence | None = None

    @property
    def tool_succeeded(self) -> bool:
        """Return success only when child exit and required workspace publication agree."""
        if self.exit_code != 0 or self.enforcement is SandboxEnforcement.UNUSABLE:
            return False
        return self.quota_evidence is None or self.quota_evidence.publication in {
            "not_required",
            "committed",
            "recovered",
        }


@runtime_checkable
class Sandbox(Protocol):
    """Execute an argv without exposing a shell to the caller."""

    def run(
        self,
        argv: list[str],
        *,
        workspace: Path,
        mode: SandboxMode,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
    ) -> SandboxRun:
        """Run ``argv`` and return confinement facts and captured output."""
        ...


__all__ = [
    "ResolvedExecutionSpec",
    "Sandbox",
    "SandboxRun",
    "StagedInput",
    "WorkspaceQuotaEvidence",
    "validate_staged_inputs",
]
