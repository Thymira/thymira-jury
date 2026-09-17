"""A30 recomputes termination and cleanup evidence from the chain the producer wrote.

Every node grades events appended through a real event log, so the chain is genuine, and every
node asserts on `audit_run`'s published verdict rather than on the check function alone. The
blindness parametrisation grades the *same* failing events with one conjunct removed: if the
verdict flips to PASSED, that conjunct is the one doing the work; if it stays FAILED with the
conjunct gone, the control was passing for some other reason and the node would be theatre.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.mira.checks.termination_evidence import (
    TERMINATION_CONJUNCTS,
    check_termination_evidence,
)
from thymira.schemas import (
    Actor,
    EventType,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from thymira.mira.checks.models import AuditReport


def _open_run() -> tuple[InMemoryEventLog, Actor]:
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    return log, system


def _spec(*, backend: str = "container", network: str = "none") -> dict[str, Any]:
    return {
        "backend": backend,
        "image": "thymira:dev" if backend == "container" else None,
        "workspace_mount": "/host:/workspace:rw",
        "network": network,
        "memory": "1g" if backend == "container" else None,
        "cpus": "1.0" if backend == "container" else None,
        "pids_limit": 128 if backend == "container" else None,
        "environment_names": [],
        "excluded_environment_names": [],
        "unenforced": ["rlimits", "workspace_quota"],
    }


def _probe(
    *, network: str = "none", memory_bytes: int = 1073741824, cpus_nano: int = 1_000_000_000
) -> dict[str, Any]:
    return {
        "network": network,
        "cpus_nano": cpus_nano,
        "memory_bytes": memory_bytes,
        "pids_limit": 128,
        "read_only_rootfs": True,
        "mounts": [{"destination": "/workspace", "read_write": True}],
        "oom_killed": False,
    }


def _termination(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome": "completed",
        "exit_source": "container_state",
        "control_channel": "container_state",
        "control_channel_validated": True,
        "deadline_s": 30.0,
        "duration_s": 1.5,
        "deadline_exceeded": False,
        "timed_out": False,
        "child_exit_code": 0,
        "reap": "quiesced",
        "reap_deadline_s": 5.0,
        "reap_duration_s": 1.5,
        "tree_scope": "container",
        "signalled_after_reap": False,
        "probe": _probe(),
    }
    payload.update(overrides)
    if payload["reap"] == "not_required":
        payload["reap_deadline_s"] = overrides.get("reap_deadline_s")
        payload["reap_duration_s"] = overrides.get("reap_duration_s")
    return payload


def _completed(
    log: InMemoryEventLog,
    system: Actor,
    *,
    termination: dict[str, Any] | None,
    exit_code: int | None = 0,
    status: str = "COMPLETED",
    cleanup_confirmed: bool = True,
    spec: dict[str, Any] | None = None,
) -> None:
    """Record one code-executing ``tool.completed`` carrying the full sandbox evidence set."""
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {
            "tool": "run_python",
            "status": status,
            "sandbox_mode": SandboxMode.WORKSPACE_WRITE.value,
            "requested_sandbox_mode": SandboxMode.WORKSPACE_WRITE.value,
            "sandbox_enforcement": SandboxEnforcement.PARTIAL.value,
            "sandbox_spec": _spec() if spec is None else spec,
            "sandbox_cleanup_confirmed": cleanup_confirmed,
            "sandbox_termination": termination,
            "exit_code": exit_code,
        },
        subject_id=new_id("tool"),
    )


def _graded(log: InMemoryEventLog) -> AuditReport:
    return audit_run(AuditContext(log.run_id, log.events()))


def _a30(report: AuditReport) -> Any:
    return next(control for control in report.controls if control.control_id == "A30")


def test_a30_passes_a_run_whose_termination_evidence_is_coherent() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination())
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.PASSED


def test_a30_is_not_applicable_without_a_recorded_enforcement_value() -> None:
    log, system = _open_run()
    log.append(EventType.TOOL_COMPLETED, system, {"tool": "read_file", "exit_code": 0})
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.NOT_APPLICABLE


def test_a30_fails_a_clean_success_recorded_over_a_passed_deadline() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(timed_out=True, deadline_exceeded=True))
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_a_completed_call_without_event_exit_code_over_bad_cleanup() -> None:
    """A persisted uppercase completion status still claims success when the event lacks an exit."""
    log, system = _open_run()
    _completed(
        log,
        system,
        exit_code=None,
        status="COMPLETED",
        cleanup_confirmed=False,
        termination=_termination(
            deadline_exceeded=True,
            timed_out=True,
            reap="unbounded",
        ),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_a_duration_past_the_deadline_that_claims_no_timeout() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(duration_s=45.0))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_an_unbounded_reap_under_a_recorded_success() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(reap="unbounded"))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_signal_sent_after_the_pid_was_reaped() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(signalled_after_reap=True))
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_a_probe_that_disagrees_with_the_resolved_specification() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(probe=_probe(network="bridge")))
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL
    assert "bridge" in control.detail


def test_a30_fails_a_probe_whose_memory_disagrees_with_the_recorded_ceiling() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(probe=_probe(memory_bytes=8 * 1024 * 1024)))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_probe_whose_cpu_ceiling_disagrees_with_the_recorded_ceiling() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(probe=_probe(cpus_nano=2_000_000_000)))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_cpu_probe_disagreement_is_not_blind() -> None:
    """The CPU comparison itself must be load-bearing for the independent oracle."""
    log, system = _open_run()
    _completed(log, system, termination=_termination(probe=_probe(cpus_nano=2_000_000_000)))
    log.append(EventType.RUN_COMPLETED, system, {})
    context = AuditContext(log.run_id, log.events())

    with_all = check_termination_evidence(context)
    without_probe_comparison = check_termination_evidence(
        context,
        conjuncts=tuple(
            conjunct
            for conjunct in TERMINATION_CONJUNCTS
            if conjunct.name != "probe_disagrees_with_the_specification"
        ),
    )

    assert with_all[0] is ControlStatus.FAILED
    assert without_probe_comparison[0] is ControlStatus.PASSED


def test_a30_fails_a_cleanup_that_was_not_confirmed_under_a_clean_success() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(), cleanup_confirmed=False)
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_confirmed_container_child_that_recorded_no_probe() -> None:
    log, system = _open_run()
    _completed(log, system, termination=_termination(probe=None))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_accepts_a_local_execution_that_can_offer_no_live_probe() -> None:
    """The criterion says "when the platform can provide it"; the local backend cannot."""
    log, system = _open_run()
    _completed(
        log,
        system,
        termination=_termination(
            probe=None,
            control_channel="os_wait",
            exit_source="os_wait",
            tree_scope="none",
            reap="not_required",
        ),
        spec=_spec(backend="local_subprocess", network="host"),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.PASSED


def test_a30_fails_a_completed_container_without_a_validated_exit_channel() -> None:
    """A daemon channel that was not validated cannot support a completed container result."""
    log, system = _open_run()
    _completed(
        log,
        system,
        termination=_termination(control_channel_validated=False),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_completed_tool_with_a_runtime_refusal_exit_source() -> None:
    """A completed result cannot claim the runtime-refusal channel."""
    log, system = _open_run()
    _completed(
        log,
        system,
        termination=_termination(
            exit_source="runtime_refusal",
            control_channel="none",
            control_channel_validated=False,
            probe=None,
        ),
        spec=_spec(backend="local_subprocess", network="host"),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_reap_that_exceeded_its_recorded_bound() -> None:
    """A measured cleanup overrun is a finding even when the child exit looks clean."""
    log, system = _open_run()
    _completed(log, system, termination=_termination(reap_deadline_s=1.0, reap_duration_s=1.5))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


def test_a30_fails_a_successful_tool_with_a_noncompleted_outcome() -> None:
    """A successful event cannot carry a timeout or overflow outcome."""
    log, system = _open_run()
    _completed(log, system, termination=_termination(outcome="timed_out"))
    log.append(EventType.RUN_COMPLETED, system, {})

    assert _a30(_graded(log)).status is ControlStatus.FAILED


@pytest.mark.parametrize(
    ("termination", "cleanup_confirmed"),
    [
        (None, True),
        ({"outcome": "completed"}, True),
        (_termination(duration_s="soon"), True),
        (_termination(reap=17), True),
        ("not-a-record", True),
    ],
    ids=["missing", "incomplete", "wrong-number", "wrong-text", "not-a-mapping"],
)
def test_a30_fails_closed_on_missing_or_malformed_evidence(
    termination: Any, cleanup_confirmed: bool
) -> None:
    log, system = _open_run()
    _completed(log, system, termination=termination, cleanup_confirmed=cleanup_confirmed)
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_s", None),
        ("duration_s", 10**1000),
        ("duration_s", -1.0),
        ("outcome", "finished"),
        ("reap", "maybe"),
        ("probe", {"network": "none"}),
    ],
    ids=[
        "missing-duration",
        "huge-duration",
        "negative-duration",
        "unknown-outcome",
        "unknown-reap",
        "partial-probe",
    ],
)
def test_a30_fails_closed_on_unreadable_termination_values(field: str, value: Any) -> None:
    """Malformed values cannot make the independent termination oracle pass."""
    log, system = _open_run()
    termination = _termination(**{field: value})
    _completed(log, system, termination=termination)
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_closed_on_an_unhashable_spec_control_list() -> None:
    """A malformed resolved specification becomes a finding instead of escaping the audit."""
    log, system = _open_run()
    _completed(
        log,
        system,
        termination=_termination(),
        spec={"backend": "container", "unenforced": [{}]},
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_closed_on_an_unbounded_memory_spec_string() -> None:
    """A hostile numeric string cannot escape MIRA's bounded memory parser."""
    log, system = _open_run()
    spec = _spec()
    spec["memory"] = "1" + ("0" * 4300)
    _completed(log, system, termination=_termination(), spec=spec)
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


def test_a30_fails_closed_on_an_unbounded_cpus_spec_string() -> None:
    """A hostile CPU limit string cannot escape MIRA's bounded specification parser."""
    log, system = _open_run()
    spec = _spec()
    spec["cpus"] = "1" * 129
    _completed(log, system, termination=_termination(), spec=spec)
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


@pytest.mark.parametrize(("field", "value"), [("memory", "0"), ("cpus", "0.0")])
def test_a30_fails_closed_on_a_zero_resource_ceiling(field: str, value: str) -> None:
    """An unlimited or zero Docker ceiling cannot masquerade as an enforced limit."""
    log, system = _open_run()
    spec = _spec()
    spec[field] = value
    _completed(log, system, termination=_termination(), spec=spec)
    log.append(EventType.RUN_COMPLETED, system, {})

    control = _a30(_graded(log))
    assert control.status is ControlStatus.FAILED
    assert control.severity is Severity.CRITICAL


@pytest.mark.parametrize("dropped", [conjunct.name for conjunct in TERMINATION_CONJUNCTS])
def test_a30_is_not_blind(dropped: str) -> None:
    """Each conjunct is the only thing standing between one hostile record and a PASS."""
    hostile = {
        "shape_and_types": _termination(duration_s="soon"),
        "clean_success_over_a_passed_deadline": _termination(
            timed_out=True, deadline_exceeded=True
        ),
        "duration_over_the_deadline_without_a_timeout": _termination(duration_s=45.0),
        "unbounded_reap": _termination(reap="unbounded"),
        "signalled_after_reap": _termination(signalled_after_reap=True),
        "cleanup_not_confirmed_on_a_success": _termination(),
        "probe_disagrees_with_the_specification": _termination(probe=_probe(network="bridge")),
        "probe_missing_on_a_confirmed_container_child": _termination(probe=None),
        "reap_within_deadline": _termination(reap_deadline_s=1.0, reap_duration_s=1.5),
        "outcome_channel_relationship": _termination(
            control_channel_validated=False,
        ),
    }[dropped]
    log, system = _open_run()
    _completed(
        log,
        system,
        termination=hostile,
        cleanup_confirmed=dropped != "cleanup_not_confirmed_on_a_success",
    )
    log.append(EventType.RUN_COMPLETED, system, {})
    context = AuditContext(log.run_id, log.events())

    with_all = check_termination_evidence(context)
    without_it = check_termination_evidence(
        context, conjuncts=tuple(c for c in TERMINATION_CONJUNCTS if c.name != dropped)
    )

    assert with_all[0] is ControlStatus.FAILED
    assert without_it[0] is ControlStatus.PASSED, f"{dropped} was not the conjunct doing the work"


def test_a30_never_imports_the_producer_it_audits() -> None:
    """MIRA recomputes; it never shares a constant with ``thymira.tools`` (F11.3)."""
    source = Path("runtime/mira/src/thymira/mira/checks/termination_evidence.py").read_text(
        encoding="utf-8"
    )
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert not [name for name in imported if name.startswith("thymira.tools")], imported


__all__: list[str] = []
