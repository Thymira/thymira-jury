"""Independent A32 checks for quota evidence grammar and conjuncts."""

from __future__ import annotations

from typing import Any

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import Actor, EventType, new_id

_DIGEST = "a" * 64


def _spec(*, unenforced: list[str] | None = None) -> dict[str, Any]:
    return {
        "backend": "container",
        "image": "thymira:dev",
        "workspace_mount": "volume:aabbccddeeff0011:/workspace:rw",
        "network": "none",
        "memory": "1g",
        "cpus": "1.0",
        "pids_limit": 128,
        "environment_names": [],
        "excluded_environment_names": [],
        "unenforced": ["rlimits"] if unenforced is None else unenforced,
        "workspace_quota_bytes": 4096,
    }


def _evidence(**overrides: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "mechanism": "docker_local_tmpfs_volume",
        "requested_bytes": 4096,
        "observed_capacity_bytes": 4096,
        "observed_filesystem": "tmpfs",
        "inode_limit": 128,
        "observed_inode_capacity": 128,
        "source_logical_bytes": 5,
        "final_logical_bytes": 12,
        "exported_logical_bytes": 12,
        "source_entries": 1,
        "final_entries": 2,
        "exported_entries": 2,
        "source_tree_sha256": _DIGEST,
        "final_tree_sha256": _DIGEST,
        "exported_tree_sha256": _DIGEST,
        "volume_inspected": True,
        "worker_mount_inspected": True,
        "publication": "committed",
        "cleanup": {"helper": True, "worker": True, "volume": True, "staging": True},
    }
    evidence.update(overrides)
    return evidence


def _report(payload: dict[str, Any]) -> Any:
    log = InMemoryEventLog(new_id("run"))
    actor = Actor.system()
    log.append(EventType.RUN_STARTED, actor, {})
    log.append(EventType.TOOL_COMPLETED, actor, payload, subject_id=new_id("tool"))
    return audit_run(AuditContext(log.run_id, log.events()))


def test_a32_passes_only_complete_quota_evidence() -> None:
    report = _report(
        {
            "tool": "run_python",
            "sandbox_spec": _spec(),
            "sandbox_quota_evidence": _evidence(),
        }
    )
    control = next(item for item in report.controls if item.control_id == "A32")
    assert control.status is ControlStatus.PASSED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observed_capacity_bytes", 4097),
        ("observed_filesystem", "ext4"),
        ("final_tree_sha256", "b" * 64),
        ("publication", "failed"),
        ("cleanup", {"helper": True}),
    ],
)
def test_a32_rejects_each_tampered_quota_fact(field: str, value: Any) -> None:
    report = _report(
        {
            "tool": "run_python",
            "sandbox_spec": _spec(),
            "sandbox_quota_evidence": _evidence(**{field: value}),
        }
    )
    control = next(item for item in report.controls if item.control_id == "A32")
    assert control.status is ControlStatus.FAILED


def test_a32_does_not_penalize_an_honest_unsupported_quota() -> None:
    report = _report(
        {
            "tool": "run_python",
            "sandbox_spec": _spec(unenforced=["rlimits", "workspace_quota"]),
            "sandbox_quota_evidence": None,
        }
    )
    control = next(item for item in report.controls if item.control_id == "A32")
    assert control.status is ControlStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_bytes", 1),
        ("final_logical_bytes", 13),
        ("exported_entries", 1),
    ],
)
def test_a32_recomputes_request_and_writeback_cross_facts(field: str, value: Any) -> None:
    report = _report(
        {
            "tool": "run_python",
            "sandbox_spec": _spec(),
            "sandbox_quota_evidence": _evidence(**{field: value}),
        }
    )
    control = next(item for item in report.controls if item.control_id == "A32")
    assert control.status is ControlStatus.FAILED
