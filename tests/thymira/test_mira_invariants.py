"""Focused tests for MIRA's relationship registry and its Core append seam."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.core import RunEventLog
from thymira.events import InMemoryEventLog, JsonlEventLog
from thymira.mira.checks import (
    AuditContext,
    ControlStatus,
    InvariantClaim,
    InvariantInput,
    InvariantOutcome,
    InvariantPhase,
    InvariantRegistry,
    build_invariant_registry,
)
from thymira.mira.checks.controls import _a30_independent_relationship
from thymira.mira.checks.termination_evidence import check_termination_evidence
from thymira.schemas import Actor, EventType, Run, new_id
from thymira.state import LocalRunStore

if TYPE_CHECKING:
    from pathlib import Path


def _claim(*, invariant_id: str = "A99") -> InvariantClaim:
    """Return valid metadata for a test-owned relationship."""
    return InvariantClaim(
        invariant_id=invariant_id,
        package_distribution="thymira-tools",
        package_import="thymira.tools",
        package_owner="P3",
        producer="thymira.tools.test_producer",
        checker_distribution="thymira-mira",
        checker_import="thymira.mira.checks.controls",
        checker="test_replay_checker",
        checker_owner="P4",
        independent_checker="test_independent_checker",
        registry="thymira.mira.checks.CONTROLS",
        relation=("producer.fact", "observer.fact"),
        failure_code=f"MIRA_{invariant_id}_RELATION_MISMATCH",
        trigger_events=frozenset({"tool.completed"}),
        control_id=invariant_id,
    )


def _register(
    registry: InvariantRegistry,
    claim: InvariantClaim | None = None,
    *,
    replay_passes: bool = True,
    independent_passes: bool = True,
) -> None:
    """Register distinct test observers with controlled outcomes."""
    registry.register(
        claim or _claim(),
        append_checker=lambda _facts: InvariantOutcome(True, "append observed"),
        replay_checker=lambda _facts: InvariantOutcome(replay_passes, "replay observed"),
        independent_checker=lambda _facts: InvariantOutcome(
            independent_passes, "independent observed"
        ),
    )


def test_registry_rejects_wrong_owner_duplicate_and_non_independent_registration() -> None:
    """A relationship cannot be registered under an arbitrary owner or one checker twice."""
    wrong_owner = replace(_claim(), package_owner="P4")
    with pytest.raises(ValueError, match="wrong owner"):
        _register(InvariantRegistry(), wrong_owner)

    registry = InvariantRegistry()
    _register(registry)
    with pytest.raises(ValueError, match="already registered"):
        _register(registry)

    def checker(_facts):
        return InvariantOutcome(True, "same")

    with pytest.raises(ValueError, match="different callables"):
        registry.register(
            _claim(invariant_id="A98"),
            append_checker=checker,
            replay_checker=checker,
            independent_checker=checker,
        )


def test_registry_rejects_invalid_append_and_replay_inputs() -> None:
    """Append and replay observations must be scoped to one persisted chain head and Run."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    first = log.append(EventType.TOOL_COMPLETED, Actor.system(), {})
    second = log.append(EventType.AGENT_MESSAGE, Actor.system(), {})
    registry = InvariantRegistry()
    _register(registry)

    with pytest.raises(ValueError, match="newly appended chain head"):
        registry.observe_append(run_id=run_id, event=first, events=log.events())
    with pytest.raises(ValueError, match="at least one durable event"):
        registry.observe_replay(run_id=run_id, events=[])

    foreign_log = InMemoryEventLog(new_id("run"))
    foreign = foreign_log.append(EventType.TOOL_COMPLETED, Actor.system(), {})
    with pytest.raises(ValueError, match="requested run"):
        registry.observe_append(run_id=run_id, event=foreign, events=[foreign])
    with pytest.raises(ValueError, match="requested run"):
        registry.observe_replay(run_id=run_id, events=[second, foreign])


def test_registry_scopes_append_and_replays_durable_facts_independently(tmp_path: Path) -> None:
    """Only triggered appends run immediately; replay compares two observer outcomes."""
    path = tmp_path / "events.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    log.append(EventType.AGENT_MESSAGE, Actor.system(), {"ignored": True})
    assert InvariantRegistry().claims == ()

    registry = InvariantRegistry()
    calls: list[InvariantPhase] = []
    registry.register(
        _claim(),
        append_checker=lambda facts: calls.append(facts.phase) or InvariantOutcome(True, "append"),
        replay_checker=lambda facts: calls.append(facts.phase) or InvariantOutcome(True, "replay"),
        independent_checker=lambda facts: (
            calls.append(facts.phase) or InvariantOutcome(False, "second observer found drift")
        ),
    )
    event = log.append(EventType.TOOL_COMPLETED, Actor.system(), {"fact": "durable"})
    append_observations = registry.observe_append(run_id=run_id, event=event, events=log.events())
    assert [observation.phase for observation in append_observations] == [InvariantPhase.APPEND]
    assert calls == [InvariantPhase.APPEND]

    replay_events = JsonlEventLog(path, run_id).events()
    replay_observations = registry.observe_replay(run_id=run_id, events=replay_events)
    assert [observation.phase for observation in replay_observations] == [
        InvariantPhase.REPLAY,
        InvariantPhase.INDEPENDENT,
    ]
    assert replay_observations[1].passed is False
    assert replay_observations[1].failure_code == "MIRA_A99_RELATION_MISMATCH"
    assert calls == [InvariantPhase.APPEND, InvariantPhase.REPLAY, InvariantPhase.INDEPENDENT]


def test_run_event_log_persists_before_append_observation(tmp_path: Path) -> None:
    """The Core lifecycle owner leaves failed or successful evidence durable before checking it."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="test invariant append",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    registry = InvariantRegistry()
    observed_history: list[tuple[object, ...]] = []
    registry.register(
        _claim(),
        append_checker=lambda facts: (
            observed_history.append(tuple(facts.events))
            or InvariantOutcome(False, "relationship is invalid")
        ),
        replay_checker=lambda _facts: InvariantOutcome(False, "relationship is invalid"),
        independent_checker=lambda _facts: InvariantOutcome(False, "relationship is invalid"),
    )
    event_log = RunEventLog(store, run.id, invariant_registry=registry)
    event = event_log.append(EventType.TOOL_COMPLETED, Actor.system(), {"fact": "truth"})

    assert store.events(run.id)[-1] == event
    assert observed_history == [tuple(store.events(run.id))]
    assert event_log.invariant_observations[0].failure_code == "MIRA_A99_RELATION_MISMATCH"


def test_malformed_checker_return_is_attributed_after_durable_append(tmp_path: Path) -> None:
    """A checker bug becomes a stable finding after the event has been durably written."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="malformed checker return",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    registry = InvariantRegistry()

    def malformed_checker(_facts: InvariantInput) -> InvariantOutcome:
        """Simulate an untyped third-party callback returning the wrong runtime shape."""
        return cast("InvariantOutcome", object())

    registry.register(
        _claim(),
        append_checker=malformed_checker,
        replay_checker=lambda _facts: InvariantOutcome(True, "replay"),
        independent_checker=lambda _facts: InvariantOutcome(True, "independent"),
    )
    event_log = RunEventLog(store, run.id, invariant_registry=registry)

    event = event_log.append(EventType.TOOL_COMPLETED, Actor.system(), {"fact": "durable"})

    assert store.events(run.id)[-1] == event
    observation = event_log.invariant_observations[0]
    assert observation.passed is False
    assert observation.failure_code == "MIRA_A99_RELATION_MISMATCH"
    assert observation.detail == "checker returned invalid outcome"


def _a30_resource_payload(*, memory_bytes: int = 1024**3) -> dict[str, Any]:
    """Build a complete container termination payload for the independent A30 observer."""
    return {
        "tool": "run_python",
        "status": "COMPLETED",
        "exit_code": 0,
        "sandbox_enforcement": "partial",
        "sandbox_mode": "workspace_write",
        "requested_sandbox_mode": "workspace_write",
        "sandbox_cleanup_confirmed": True,
        "sandbox_spec": {
            "backend": "container",
            "image": "thymira:dev",
            "workspace_mount": "/host:/workspace:rw",
            "network": "none",
            "memory": "1g",
            "cpus": "1.0",
            "pids_limit": 128,
            "environment_names": [],
            "excluded_environment_names": [],
            "unenforced": ["rlimits", "workspace_quota"],
        },
        "sandbox_termination": {
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
            "probe": {
                "network": "none",
                "memory_bytes": memory_bytes,
                "cpus_nano": 1_000_000_000,
                "pids_limit": 128,
                "read_only_rootfs": True,
                "mounts": [{"destination": "/workspace", "read_write": True}],
                "oom_killed": False,
            },
        },
    }


def test_a30_independent_observer_checks_the_recorded_memory_ceiling() -> None:
    """A memory drift is caught by the independent resource projection itself."""
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        _a30_resource_payload(memory_bytes=8 * 1024 * 1024),
    )

    observations = build_invariant_registry().observe_replay(run_id=log.run_id, events=log.events())

    independent = next(
        observation
        for observation in observations
        if observation.phase is InvariantPhase.INDEPENDENT
    )
    assert independent.passed is False
    assert independent.failure_code == "MIRA_A30_TERMINATION_MISMATCH"
    assert "memory" in independent.detail


def test_a30_independent_observer_rejects_a_malformed_local_probe() -> None:
    """A present but malformed probe fails independently even when local probes may be null."""
    payload = _a30_resource_payload()
    payload["sandbox_spec"] = {
        **payload["sandbox_spec"],
        "backend": "local_subprocess",
        "image": None,
        "memory": None,
        "cpus": None,
        "pids_limit": None,
        "workspace_mount": "/workspace",
        "unenforced": [
            "network",
            "memory",
            "cpus",
            "pids_limit",
            "filesystem",
            "rlimits",
            "workspace_quota",
        ],
    }
    payload["sandbox_termination"]["probe"] = "malformed"
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.TOOL_COMPLETED, Actor.system(), payload)

    observations = build_invariant_registry().observe_replay(run_id=log.run_id, events=log.events())

    independent = next(
        observation
        for observation in observations
        if observation.phase is InvariantPhase.INDEPENDENT
    )
    assert independent.passed is False
    assert independent.failure_code == "MIRA_A30_TERMINATION_MISMATCH"
    assert "not a record" in independent.detail


def _apply_a30_resource_case(
    payload: dict[str, Any],
    side: str,
    field: str | None,
    value: Any,
    delete: bool,
) -> None:
    """Apply one table row to a requested spec, live probe, or termination record."""
    if side == "spec":
        target = payload["sandbox_spec"]
    elif side == "probe":
        target = payload["sandbox_termination"]["probe"]
    else:
        target = payload["sandbox_termination"]
    if field is None:
        return
    if delete:
        target.pop(field, None)
    else:
        target[field] = value


@pytest.mark.parametrize(
    ("case", "side", "field", "value", "delete", "expected"),
    [
        ("memory observed missing", "probe", "memory_bytes", None, True, False),
        ("memory observed null", "probe", "memory_bytes", None, False, False),
        ("memory observed wrong type", "probe", "memory_bytes", "1g", False, False),
        ("memory requested missing is unclaimed", "spec", "memory", None, True, True),
        ("memory unlimited request", "spec", "memory", None, False, True),
        ("memory mismatch", "probe", "memory_bytes", 8 * 1024 * 1024, False, False),
        ("CPU requested missing is unproved", "spec", "cpus", None, True, True),
        ("CPU requested null is unproved", "spec", "cpus", None, False, True),
        ("CPU requested wrong type", "spec", "cpus", "unlimited", False, False),
        ("CPU unlimited request is unproved", "spec", "cpus", None, False, True),
        ("CPU observed extra field is outside probe contract", "probe", "cpus", "2.0", False, True),
        ("CPU observed missing", "probe", "cpus_nano", None, True, False),
        ("CPU observed null", "probe", "cpus_nano", None, False, False),
        ("CPU observed wrong type", "probe", "cpus_nano", "1000000000", False, False),
        ("CPU observed unlimited", "probe", "cpus_nano", 0, False, False),
        ("CPU mismatch", "probe", "cpus_nano", 2_000_000_000, False, False),
        ("CPU requested beyond nanocpu precision", "spec", "cpus", "1.0000000001", False, False),
        ("pids observed missing", "probe", "pids_limit", None, True, False),
        ("pids observed null", "probe", "pids_limit", None, False, False),
        ("pids observed wrong type", "probe", "pids_limit", "128", False, False),
        ("pids requested missing is unclaimed", "spec", "pids_limit", None, True, True),
        ("pids unlimited request", "spec", "pids_limit", None, False, True),
        ("pids mismatch", "probe", "pids_limit", 64, False, False),
        ("network observed missing", "probe", "network", None, True, False),
        ("network observed null", "probe", "network", None, False, False),
        ("network observed wrong type", "probe", "network", 1, False, False),
        ("network equal host mode", "probe", "network", "host", False, True),
        ("network mismatch", "probe", "network", "bridge", False, False),
        ("rootfs observed missing", "probe", "read_only_rootfs", None, True, False),
        ("rootfs observed null", "probe", "read_only_rootfs", None, False, False),
        ("rootfs observed wrong type", "probe", "read_only_rootfs", 1, False, False),
        ("rootfs explicitly unenforced", "probe", "read_only_rootfs", False, False, True),
        ("rootfs mismatch", "probe", "read_only_rootfs", False, False, False),
        ("mount observed missing", "probe", "mounts", None, True, False),
        ("mount observed null", "probe", "mounts", None, False, False),
        ("mount observed wrong type", "probe", "mounts", {}, False, False),
        ("mount mode unclaimed", "spec", "workspace_mount", "/workspace", False, True),
        (
            "mount mismatch",
            "probe",
            "mounts",
            [{"destination": "/workspace", "read_write": False}],
            False,
            False,
        ),
    ],
)
def test_a30_observers_agree_on_resource_shape_and_relation_matrix(
    case: str,
    side: str,
    field: str | None,
    value: Any,
    delete: bool,
    expected: bool,
) -> None:
    """Primary and independent A30 observers agree across resource edge cases."""
    payload = _a30_resource_payload()
    if case == "network equal host mode":
        payload["sandbox_spec"]["network"] = "host"
    if case == "rootfs explicitly unenforced":
        payload["sandbox_spec"]["unenforced"].append("filesystem")
    _apply_a30_resource_case(payload, side, field, value, delete)
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.TOOL_COMPLETED, Actor.system(), payload)
    events = log.events()

    primary = check_termination_evidence(AuditContext(log.run_id, events))[0]
    independent = _a30_independent_relationship(
        InvariantInput(log.run_id, events, None, InvariantPhase.INDEPENDENT)
    )
    assert (primary is ControlStatus.PASSED) is expected, case
    assert independent.passed is expected, case


def test_default_registry_attributes_a30_replay_failure() -> None:
    """A malformed sandbox completion produces an attributed replay finding."""
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {
            "sandbox_enforcement": "partial",
            "status": "COMPLETED",
            "exit_code": 0,
            "sandbox_cleanup_confirmed": True,
        },
    )

    observations = build_invariant_registry().observe_replay(run_id=log.run_id, events=log.events())
    a30_observations = [
        observation for observation in observations if observation.control_id == "A30"
    ]
    # Other registered claims (e.g. A31's subagent settlement) also observe this replay; with no
    # delegation events present they pass and stay out of scope for this A30-focused assertion.
    assert [observation.phase for observation in a30_observations] == [
        InvariantPhase.REPLAY,
        InvariantPhase.INDEPENDENT,
    ]
    assert all(observation.passed is False for observation in a30_observations)
    assert all(
        observation.failure_code == "MIRA_A30_TERMINATION_MISMATCH"
        for observation in a30_observations
    )
