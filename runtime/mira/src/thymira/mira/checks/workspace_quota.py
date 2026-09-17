"""Independent replay check for the Docker tmpfs workspace quota evidence."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from thymira.mira.checks.models import ControlStatus
from thymira.schemas import Event, EventType, Evidence

if TYPE_CHECKING:
    from collections.abc import Sequence

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _evidence(events: Sequence[Event]) -> tuple[Evidence, ...]:
    """Pin every quota event examined by this independent check."""
    return tuple(
        Evidence(kind="event", ref=f"seq:{event.seq}", sha256=event.hash) for event in events
    )


def check_workspace_quota(  # noqa: PLR0912, PLR0915  # each conjunct is independently replayed
    events: Sequence[Event],
) -> tuple[ControlStatus, str, tuple[Evidence, ...]]:
    """Recompute quota evidence without importing the producer or trusting its success flag."""
    executions = []
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        spec = event.payload.get("sandbox_spec")
        if isinstance(spec, dict) and spec.get("workspace_quota_bytes") is not None:
            executions.append(event)
    effective = [
        event
        for event in executions
        if isinstance(event.payload.get("sandbox_spec"), dict)
        and "workspace_quota" not in event.payload["sandbox_spec"].get("unenforced", [])
    ]
    if not effective:
        return ControlStatus.NOT_APPLICABLE, "no execution claimed an enforced workspace quota", ()
    problems: list[str] = []
    for event in effective:
        evidence = event.payload.get("sandbox_quota_evidence")
        spec = event.payload.get("sandbox_spec")
        if not isinstance(evidence, dict) or not isinstance(spec, dict):
            problems.append(f"seq {event.seq}: quota evidence is missing")
            continue
        requested = spec.get("workspace_quota_bytes")
        evidence_requested = evidence.get("requested_bytes")
        observed = evidence.get("observed_capacity_bytes")
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested <= 0
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or observed <= 0
            or observed > requested
        ):
            problems.append(f"seq {event.seq}: observed capacity is not within the request")
        if (
            not isinstance(evidence_requested, int)
            or isinstance(evidence_requested, bool)
            or evidence_requested <= 0
            or evidence_requested != requested
        ):
            problems.append(f"seq {event.seq}: evidence request does not match the sandbox request")
        if evidence.get("mechanism") != "docker_local_tmpfs_volume":
            problems.append(f"seq {event.seq}: unsupported quota mechanism")
        if evidence.get("observed_filesystem") != "tmpfs":
            problems.append(f"seq {event.seq}: observed filesystem is not tmpfs")
        if (
            evidence.get("volume_inspected") is not True
            or evidence.get("worker_mount_inspected") is not True
        ):
            problems.append(f"seq {event.seq}: daemon volume or worker mount was not inspected")
        if evidence.get("publication") not in {"committed", "recovered"}:
            problems.append(f"seq {event.seq}: workspace publication was not committed")
        cleanup = evidence.get("cleanup")
        cleanup_keys = {"helper", "worker", "volume", "staging", "inputs"}
        # Events written before staged-input evidence was introduced are still replayable when
        # they explicitly carried the older four cleanup facts. New quota events must account
        # for the private input source mount as well.
        if "staged_input_logical_bytes" in evidence:
            allowed_cleanup = cleanup_keys
        else:
            allowed_cleanup = cleanup_keys - {"inputs"}
        if (
            not isinstance(cleanup, dict)
            or set(cleanup) != allowed_cleanup
            or not all(value is True for value in cleanup.values())
        ):
            problems.append(f"seq {event.seq}: quota cleanup was incomplete")
        for key in (
            "source_logical_bytes",
            "final_logical_bytes",
            "exported_logical_bytes",
            "source_entries",
            "final_entries",
            "exported_entries",
        ):
            value = evidence.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(f"seq {event.seq}: {key} is missing or invalid")
            elif key.endswith("logical_bytes") and isinstance(observed, int) and value > observed:
                problems.append(f"seq {event.seq}: {key} exceeds observed capacity")
        staged_bytes = evidence.get("staged_input_logical_bytes", 0)
        staged_entries = evidence.get("staged_input_entries", 0)
        if (
            not isinstance(staged_bytes, int)
            or isinstance(staged_bytes, bool)
            or staged_bytes < 0
            or not isinstance(staged_entries, int)
            or isinstance(staged_entries, bool)
            or staged_entries < 0
        ):
            problems.append(f"seq {event.seq}: staged input evidence is missing or invalid")
        elif (
            isinstance(observed, int)
            and evidence.get("source_logical_bytes", 0) + staged_bytes > observed
        ):
            problems.append(f"seq {event.seq}: source plus staged inputs exceed observed capacity")
        if evidence.get("final_logical_bytes") != evidence.get("exported_logical_bytes"):
            problems.append(f"seq {event.seq}: final and exported logical bytes disagree")
        if evidence.get("final_entries") != evidence.get("exported_entries"):
            problems.append(f"seq {event.seq}: final and exported entry counts disagree")
        problems.extend(
            f"seq {event.seq}: {key} is missing or invalid"
            for key in (
                "source_tree_sha256",
                "final_tree_sha256",
                "exported_tree_sha256",
            )
            if not isinstance(evidence.get(key), str) or _SHA256.fullmatch(evidence[key]) is None
        )
        if evidence.get("final_tree_sha256") != evidence.get("exported_tree_sha256"):
            problems.append(f"seq {event.seq}: helper and host export digests disagree")
        staged_digest = evidence.get("staged_input_tree_sha256")
        if staged_digest is not None and (
            not isinstance(staged_digest, str) or _SHA256.fullmatch(staged_digest) is None
        ):
            problems.append(f"seq {event.seq}: staged input tree digest is invalid")
        inode_limit = evidence.get("inode_limit")
        observed_inodes = evidence.get("observed_inode_capacity")
        if (
            not isinstance(inode_limit, int)
            or isinstance(inode_limit, bool)
            or inode_limit <= 0
            or not isinstance(observed_inodes, int)
            or isinstance(observed_inodes, bool)
            or observed_inodes <= 0
            or observed_inodes > inode_limit
        ):
            problems.append(f"seq {event.seq}: observed inode capacity is invalid")
        if "workspace_quota" in spec.get("unenforced", []):
            problems.append(f"seq {event.seq}: effective quota remains listed as unenforced")
    if problems:
        return ControlStatus.FAILED, "; ".join(problems), _evidence(effective)
    return (
        ControlStatus.PASSED,
        "every enforced workspace quota has independent evidence",
        _evidence(effective),
    )


__all__ = ["check_workspace_quota"]
