"""Unit tests for the pure Contract 0.3 Run state machine."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from thymira.core import (
    InvalidRunStateError,
    InvalidRunTransitionError,
    RunCondition,
    RunOutcome,
    RunProjection,
    RunStage,
    RunState,
    RunTransitionCommand,
    RunTransitionKind,
    StaleRunVersionError,
    WaitReason,
    allowed_transitions,
    apply_transition,
    initial_run_state,
)


def _apply(state: RunProjection, kind: RunTransitionKind) -> RunProjection:
    """Apply a command against the current state version."""
    return apply_transition(state, RunTransitionCommand(kind, state.version)).state


def _executing_state() -> RunProjection:
    """Return an active Run in the executing stage."""
    state = initial_run_state("run-state-test")
    state = _apply(state, RunTransitionKind.START)
    return _apply(state, RunTransitionKind.BEGIN_EXECUTION)


def test_normal_path_returns_new_immutable_states() -> None:
    state = _executing_state()
    auditing = _apply(state, RunTransitionKind.BEGIN_AUDIT)
    reporting = _apply(auditing, RunTransitionKind.BEGIN_REPORTING)
    completed = _apply(reporting, RunTransitionKind.COMPLETE)

    assert state.stage is RunStage.EXECUTING
    assert isinstance(state.state, RunState)
    assert auditing is not state
    assert auditing.stage is RunStage.AUDITING
    assert reporting.stage is RunStage.REPORTING
    assert completed.condition is RunCondition.TERMINAL
    assert completed.outcome is RunOutcome.COMPLETED
    assert completed.version == 5


def test_waiting_for_information_resumes_the_same_stage() -> None:
    executing = _executing_state()
    waiting = _apply(executing, RunTransitionKind.WAIT_FOR_INFORMATION)
    resumed = _apply(waiting, RunTransitionKind.RESUME)

    assert waiting.condition is RunCondition.WAITING
    assert waiting.wait_reason is WaitReason.INFORMATION
    assert resumed.stage is RunStage.EXECUTING
    assert resumed.condition is RunCondition.ACTIVE
    assert resumed.wait_reason is None


def test_waiting_for_approval_resumes_the_same_stage() -> None:
    auditing = _apply(_executing_state(), RunTransitionKind.BEGIN_AUDIT)
    waiting = _apply(auditing, RunTransitionKind.WAIT_FOR_APPROVAL)
    resumed = _apply(waiting, RunTransitionKind.RESUME)

    assert waiting.wait_reason is WaitReason.APPROVAL
    assert resumed.stage is RunStage.AUDITING
    assert resumed.condition is RunCondition.ACTIVE


def test_pause_and_resume_preserve_stage() -> None:
    executing = _executing_state()
    paused = _apply(executing, RunTransitionKind.PAUSE)
    resumed = _apply(paused, RunTransitionKind.RESUME)

    assert paused.condition is RunCondition.PAUSED
    assert paused.wait_reason is None
    assert resumed.stage is RunStage.EXECUTING
    assert resumed.condition is RunCondition.ACTIVE


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (RunTransitionKind.BLOCK, RunOutcome.BLOCKED),
        (RunTransitionKind.FAIL, RunOutcome.FAILED),
        (RunTransitionKind.CANCEL, RunOutcome.CANCELLED),
    ],
)
def test_terminal_commands_produce_their_declared_outcome(
    kind: RunTransitionKind, outcome: RunOutcome
) -> None:
    terminal = _apply(_executing_state(), kind)

    assert terminal.condition is RunCondition.TERMINAL
    assert terminal.outcome is outcome
    assert allowed_transitions(terminal) == frozenset()


def test_terminal_state_rejects_every_transition() -> None:
    auditing = _apply(_executing_state(), RunTransitionKind.BEGIN_AUDIT)
    reporting = _apply(auditing, RunTransitionKind.BEGIN_REPORTING)
    completed = _apply(reporting, RunTransitionKind.COMPLETE)

    with pytest.raises(InvalidRunTransitionError, match="not allowed"):
        _apply(completed, RunTransitionKind.RESUME)


@pytest.mark.parametrize(
    ("condition", "wait_reason", "outcome", "match"),
    [
        (RunCondition.WAITING, None, None, "requires wait_reason"),
        (RunCondition.ACTIVE, WaitReason.INFORMATION, None, "only valid while waiting"),
        (RunCondition.TERMINAL, None, None, "requires outcome"),
        (RunCondition.ACTIVE, None, RunOutcome.FAILED, "only valid for terminal"),
    ],
)
def test_impossible_composite_states_are_rejected(
    condition: RunCondition,
    wait_reason: WaitReason | None,
    outcome: RunOutcome | None,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        RunProjection(
            "run-state-test",
            RunState(
                stage=RunStage.EXECUTING,
                condition=condition,
                wait_reason=wait_reason,
                outcome=outcome,
                version=0,
            ),
        )


def test_completed_outcome_requires_reporting_stage() -> None:
    with pytest.raises(ValidationError, match="requires reporting"):
        RunState(
            stage=RunStage.AUDITING,
            condition=RunCondition.TERMINAL,
            outcome=RunOutcome.COMPLETED,
            version=2,
        )


def test_invalid_component_types_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RunState(
            stage=cast("RunStage", "executing"),
            condition=RunCondition.ACTIVE,
            version=0,
        )

    with pytest.raises(InvalidRunStateError, match=r"state must be a Contract 0\.3 RunState"):
        RunProjection("run-state-test", cast("RunState", object()))

    with pytest.raises(InvalidRunStateError, match="kind must be a RunTransitionKind"):
        RunTransitionCommand(cast("RunTransitionKind", "start"), 0)


def test_stale_command_version_is_rejected() -> None:
    state = _executing_state()

    with pytest.raises(StaleRunVersionError, match="expected version 1, current version is 2"):
        apply_transition(state, RunTransitionCommand(RunTransitionKind.BEGIN_AUDIT, 1))


def test_transition_event_is_deterministic_and_structured() -> None:
    state = _executing_state()
    command = RunTransitionCommand(RunTransitionKind.BEGIN_AUDIT, state.version)

    first = apply_transition(state, command)
    second = apply_transition(state, command)

    assert first == second
    assert first.event.event_type == "run.transitioned"
    assert first.event.payload() == {
        "command": "begin_audit",
        "from_version": 2,
        "to_version": 3,
        "previous_stage": "executing",
        "previous_condition": "active",
        "previous_wait_reason": None,
        "previous_outcome": None,
        "stage": "auditing",
        "condition": "active",
        "wait_reason": None,
        "outcome": None,
    }
