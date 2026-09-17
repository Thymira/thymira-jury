"""Pure Contract 0.3 Run state machine.

This module defines the in-memory domain mechanics only. Persistence, authorization checks,
event hashing, and clocks belong to their respective boundaries; a caller supplies a validated
transition command and persists the returned event description with the returned state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from thymira.schemas import (
    RunCondition,
    RunOutcome,
    RunStage,
    RunState,
    WaitReason,
)

PersistedRunState = RunState
"""Internal name distinguishing the shared persisted record from ``RunProjection``."""


class RunStateError(ValueError):
    """Base error for invalid Contract 0.3 Run state operations."""


class InvalidRunStateError(RunStateError):
    """Raised when the components of a Run state form an impossible combination."""


class InvalidRunTransitionError(RunStateError):
    """Raised when a command is not allowed from the current composite state."""


class StaleRunVersionError(RunStateError):
    """Raised when a command was created from an obsolete Run version."""


class RunTransitionKind(StrEnum):
    """Commands accepted by the pure Run state machine."""

    START = "start"
    BEGIN_EXECUTION = "begin_execution"
    BEGIN_EXPERIMENT = "begin_experiment"
    BEGIN_AUDIT = "begin_audit"
    BEGIN_REPORTING = "begin_reporting"
    REOPEN = "reopen"
    WAIT_FOR_INFORMATION = "wait_for_information"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    PAUSE = "pause"
    RESUME = "resume"
    COMPLETE = "complete"
    BLOCK = "block"
    FAIL = "fail"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class RunProjection:
    """In-memory Run identity paired with the canonical persisted state.

    ``state`` is the Contract 0.3 record that persistence boundaries store. ``run_id`` remains
    outside it because it identifies the aggregate that owns that projection and its events.
    """

    run_id: str
    state: PersistedRunState

    def __post_init__(self) -> None:
        """Reject invalid aggregate identities and non-contract state records."""
        _validate_projection(self)

    @property
    def stage(self) -> RunStage:
        """Expose the canonical state stage for transition evaluation."""
        return self.state.stage

    @property
    def condition(self) -> RunCondition:
        """Expose the canonical state condition for transition evaluation."""
        return self.state.condition

    @property
    def wait_reason(self) -> WaitReason | None:
        """Expose the canonical wait reason for transition evaluation."""
        return self.state.wait_reason

    @property
    def outcome(self) -> RunOutcome | None:
        """Expose the canonical terminal outcome for transition evaluation."""
        return self.state.outcome

    @property
    def version(self) -> int:
        """Expose the canonical optimistic-concurrency version."""
        return self.state.version


@dataclass(frozen=True, slots=True)
class RunTransitionCommand:
    """A requested transition, separate from the persisted Run state."""

    kind: RunTransitionKind
    expected_version: int

    def __post_init__(self) -> None:
        """Require a non-negative optimistic-concurrency version."""
        if not isinstance(self.kind, RunTransitionKind):
            raise InvalidRunStateError("kind must be a RunTransitionKind")
        if not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool):
            raise InvalidRunStateError("expected_version must be an integer")
        if self.expected_version < 0:
            raise InvalidRunStateError("expected_version must be non-negative")


@dataclass(frozen=True, slots=True)
class RunTransitionEvent:
    """Deterministic event description for a transition that a boundary must persist."""

    event_type: str
    run_id: str
    command: RunTransitionKind
    from_version: int
    to_version: int
    previous_state: PersistedRunState
    state: PersistedRunState

    def payload(self) -> dict[str, str | int | None]:
        """Return the structured, clock-free payload for a future event envelope."""
        return {
            "command": self.command.value,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "previous_stage": self.previous_state.stage.value,
            "previous_condition": self.previous_state.condition.value,
            "previous_wait_reason": (
                self.previous_state.wait_reason.value
                if self.previous_state.wait_reason is not None
                else None
            ),
            "previous_outcome": (
                self.previous_state.outcome.value
                if self.previous_state.outcome is not None
                else None
            ),
            "stage": self.state.stage.value,
            "condition": self.state.condition.value,
            "wait_reason": (
                self.state.wait_reason.value if self.state.wait_reason is not None else None
            ),
            "outcome": self.state.outcome.value if self.state.outcome is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RunTransitionResult:
    """The immutable next state and event description produced by one command."""

    state: RunProjection
    event: RunTransitionEvent


_NON_TERMINAL_COMMANDS = frozenset(
    {RunTransitionKind.BLOCK, RunTransitionKind.FAIL, RunTransitionKind.CANCEL}
)

_ACTIVE_COMMANDS: dict[RunStage, frozenset[RunTransitionKind]] = {
    RunStage.CREATED: frozenset({RunTransitionKind.START, *_NON_TERMINAL_COMMANDS}),
    RunStage.PLANNING: frozenset(
        {
            RunTransitionKind.BEGIN_EXECUTION,
            RunTransitionKind.WAIT_FOR_INFORMATION,
            RunTransitionKind.WAIT_FOR_APPROVAL,
            RunTransitionKind.PAUSE,
            *_NON_TERMINAL_COMMANDS,
        }
    ),
    RunStage.EXECUTING: frozenset(
        {
            RunTransitionKind.BEGIN_EXPERIMENT,
            RunTransitionKind.BEGIN_AUDIT,
            RunTransitionKind.BEGIN_REPORTING,
            RunTransitionKind.WAIT_FOR_INFORMATION,
            RunTransitionKind.WAIT_FOR_APPROVAL,
            RunTransitionKind.PAUSE,
            *_NON_TERMINAL_COMMANDS,
        }
    ),
    RunStage.EXPERIMENTING: frozenset(
        {
            RunTransitionKind.BEGIN_EXECUTION,
            RunTransitionKind.BEGIN_AUDIT,
            RunTransitionKind.BEGIN_REPORTING,
            RunTransitionKind.WAIT_FOR_INFORMATION,
            RunTransitionKind.WAIT_FOR_APPROVAL,
            RunTransitionKind.PAUSE,
            *_NON_TERMINAL_COMMANDS,
        }
    ),
    RunStage.AUDITING: frozenset(
        {
            RunTransitionKind.REOPEN,
            RunTransitionKind.BEGIN_REPORTING,
            RunTransitionKind.WAIT_FOR_INFORMATION,
            RunTransitionKind.WAIT_FOR_APPROVAL,
            RunTransitionKind.PAUSE,
            *_NON_TERMINAL_COMMANDS,
        }
    ),
    RunStage.REPORTING: frozenset(
        {
            RunTransitionKind.COMPLETE,
            RunTransitionKind.WAIT_FOR_INFORMATION,
            RunTransitionKind.WAIT_FOR_APPROVAL,
            RunTransitionKind.PAUSE,
            *_NON_TERMINAL_COMMANDS,
        }
    ),
}

_ADVANCE_STAGES: dict[RunTransitionKind, RunStage] = {
    RunTransitionKind.START: RunStage.PLANNING,
    RunTransitionKind.BEGIN_EXECUTION: RunStage.EXECUTING,
    RunTransitionKind.BEGIN_EXPERIMENT: RunStage.EXPERIMENTING,
    RunTransitionKind.BEGIN_AUDIT: RunStage.AUDITING,
    RunTransitionKind.BEGIN_REPORTING: RunStage.REPORTING,
    RunTransitionKind.REOPEN: RunStage.EXECUTING,
}

_TERMINAL_OUTCOMES: dict[RunTransitionKind, RunOutcome] = {
    RunTransitionKind.COMPLETE: RunOutcome.COMPLETED,
    RunTransitionKind.BLOCK: RunOutcome.BLOCKED,
    RunTransitionKind.FAIL: RunOutcome.FAILED,
    RunTransitionKind.CANCEL: RunOutcome.CANCELLED,
}

_WAITING_TRANSITIONS = frozenset({RunTransitionKind.RESUME, *_NON_TERMINAL_COMMANDS})
_PAUSED_TRANSITIONS = frozenset({RunTransitionKind.RESUME, *_NON_TERMINAL_COMMANDS})
_TERMINAL_TRANSITIONS = frozenset[RunTransitionKind]()


def initial_run_state(run_id: str) -> RunProjection:
    """Return the initial immutable state for ``run_id``."""
    return RunProjection(
        run_id=run_id,
        state=PersistedRunState(
            stage=RunStage.CREATED,
            condition=RunCondition.ACTIVE,
            version=0,
        ),
    )


def allowed_transitions(state: RunProjection) -> frozenset[RunTransitionKind]:
    """Return the commands explicitly permitted from ``state``."""
    if state.condition is RunCondition.ACTIVE:
        return _ACTIVE_COMMANDS[state.stage]
    if state.condition is RunCondition.WAITING:
        return _WAITING_TRANSITIONS
    if state.condition is RunCondition.PAUSED:
        return _PAUSED_TRANSITIONS
    return _TERMINAL_TRANSITIONS


def apply_transition(state: RunProjection, command: RunTransitionCommand) -> RunTransitionResult:
    """Apply one command and return a new state plus its deterministic event description.

    Raises:
        StaleRunVersionError: The command was built for a different version.
        InvalidRunTransitionError: The command is not permitted from the current state.
    """
    if command.expected_version != state.version:
        raise StaleRunVersionError(
            f"run {state.run_id}: expected version {command.expected_version}, current version "
            f"is {state.version}"
        )
    if command.kind not in allowed_transitions(state):
        raise InvalidRunTransitionError(
            f"run {state.run_id}: command {command.kind} is not allowed from "
            f"{state.stage}/{state.condition}"
        )
    next_state = _next_state(state, command.kind)
    return RunTransitionResult(
        state=next_state,
        event=RunTransitionEvent(
            event_type="run.transitioned",
            run_id=state.run_id,
            command=command.kind,
            from_version=state.version,
            to_version=next_state.version,
            previous_state=state.state,
            state=next_state.state,
        ),
    )


def _next_state(state: RunProjection, kind: RunTransitionKind) -> RunProjection:
    """Build the already-authorized next state for one valid command."""
    stage = _ADVANCE_STAGES.get(kind, state.stage)
    if kind in _ADVANCE_STAGES or kind is RunTransitionKind.RESUME:
        return _copy_state(state, stage=stage, condition=RunCondition.ACTIVE)
    if kind is RunTransitionKind.WAIT_FOR_INFORMATION:
        return _copy_state(
            state,
            condition=RunCondition.WAITING,
            wait_reason=WaitReason.INFORMATION,
        )
    if kind is RunTransitionKind.WAIT_FOR_APPROVAL:
        return _copy_state(
            state,
            condition=RunCondition.WAITING,
            wait_reason=WaitReason.APPROVAL,
        )
    if kind is RunTransitionKind.PAUSE:
        return _copy_state(state, condition=RunCondition.PAUSED)
    return _copy_state(
        state,
        condition=RunCondition.TERMINAL,
        outcome=_TERMINAL_OUTCOMES[kind],
    )


def _copy_state(
    state: RunProjection,
    *,
    stage: RunStage | None = None,
    condition: RunCondition,
    wait_reason: WaitReason | None = None,
    outcome: RunOutcome | None = None,
) -> RunProjection:
    """Return the next immutable state with a monotonically increasing version."""
    return RunProjection(
        run_id=state.run_id,
        state=PersistedRunState(
            stage=state.stage if stage is None else stage,
            condition=condition,
            wait_reason=wait_reason,
            outcome=outcome,
            version=state.version + 1,
        ),
    )


def _validate_projection(state: RunProjection) -> None:
    """Enforce the non-contract invariants of an in-memory Run projection."""
    if not isinstance(state.run_id, str):
        raise InvalidRunStateError("run_id must be a string")
    if not state.run_id.strip():
        raise InvalidRunStateError("run_id must not be empty")
    if not isinstance(state.state, PersistedRunState):
        raise InvalidRunStateError("state must be a Contract 0.3 RunState")
