"""Registry for observable relationships checked by MIRA.

The registry is deliberately a small, explicit catalogue of relationships.  It does not load
plugins or import the package that produced an observation.  Producers write facts to the event
stream; MIRA supplies an independently authored checker and this module makes the append and
replay call sites use that checker with a stable attribution code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event


class InvariantPhase(StrEnum):
    """The observation pass that produced an invariant result."""

    APPEND = "append"
    REPLAY = "replay"
    INDEPENDENT = "independent"


@dataclass(frozen=True, slots=True)
class InvariantClaim:
    """Metadata binding one producer relationship to its independent MIRA checker."""

    invariant_id: str
    package_distribution: str
    package_import: str
    package_owner: str
    producer: str
    checker_distribution: str
    checker_import: str
    checker: str
    checker_owner: str
    independent_checker: str
    registry: str
    relation: tuple[str, str]
    failure_code: str
    trigger_events: frozenset[str]
    control_id: str


@dataclass(frozen=True, slots=True)
class InvariantInput:
    """Facts supplied to a relationship checker."""

    run_id: str
    events: Sequence[Event]
    store: object | None
    phase: InvariantPhase


@dataclass(frozen=True, slots=True)
class InvariantOutcome:
    """A checker outcome before the registry attributes its stable failure code."""

    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InvariantObservation:
    """One attributed relationship observation."""

    invariant_id: str
    control_id: str
    phase: InvariantPhase
    passed: bool
    failure_code: str | None
    detail: str


class InvariantChecker(Protocol):
    """Check one relationship using only the supplied durable facts."""

    def __call__(self, facts: InvariantInput, /) -> InvariantOutcome:
        """Return the relationship verdict."""
        ...


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,127}$")
_DISTRIBUTION = re.compile(r"^thymira-[a-z][a-z0-9-]*$")
_IMPORT = re.compile(r"^thymira\.[a-z][a-z0-9_.]*$")
_OWNER = re.compile(r"^P[1-5]$")
_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_RELATION_ENDPOINT_COUNT = 2

# These are package ownership facts, not a replacement for the repository's package inventory.
# Keeping only the packages that currently provide relationship producers makes a wrong owner
# impossible to hide behind an arbitrary string while leaving README-only packages scoped out.
_PRODUCER_OWNERS = {
    "thymira-agents": "P2",
    "thymira-thy": "P2",
    "thymira-tools": "P3",
}
_CHECKER_OWNERS = {"thymira-mira": "P4"}


def _expected_distribution(import_name: str) -> str:
    """Derive the workspace distribution spelling from an import name."""
    root_module = import_name.removeprefix("thymira.").split(".", 1)[0]
    return "thymira-" + root_module


def _validate_text_fields(claim: InvariantClaim) -> None:
    """Reject missing scalar metadata before validating its meaning."""
    scalar_fields = {
        "invariant_id": claim.invariant_id,
        "package_distribution": claim.package_distribution,
        "package_import": claim.package_import,
        "package_owner": claim.package_owner,
        "producer": claim.producer,
        "checker_distribution": claim.checker_distribution,
        "checker_import": claim.checker_import,
        "checker": claim.checker,
        "checker_owner": claim.checker_owner,
        "independent_checker": claim.independent_checker,
        "registry": claim.registry,
        "control_id": claim.control_id,
    }
    for name, value in scalar_fields.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invariant {name} must be a non-empty string")


def _validate_producer(claim: InvariantClaim) -> None:
    """Reject a producer whose distribution, import or owner is not registered."""
    if not _IDENTIFIER.fullmatch(claim.invariant_id):
        raise ValueError(f"invalid invariant id: {claim.invariant_id!r}")
    if not _IDENTIFIER.fullmatch(claim.control_id):
        raise ValueError(f"invalid invariant control id: {claim.control_id!r}")
    if not _DISTRIBUTION.fullmatch(claim.package_distribution):
        raise ValueError(f"invalid producer distribution: {claim.package_distribution!r}")
    if not _IMPORT.fullmatch(claim.package_import):
        raise ValueError(f"invalid producer import: {claim.package_import!r}")
    if claim.package_distribution != _expected_distribution(claim.package_import):
        raise ValueError("producer distribution and import name do not agree")
    expected_owner = _PRODUCER_OWNERS.get(claim.package_distribution)
    if expected_owner is None:
        raise ValueError(
            f"producer package is not a registered relationship owner: {claim.package_distribution}"
        )
    if claim.package_owner != expected_owner:
        raise ValueError(
            f"wrong owner for {claim.package_distribution}: {claim.package_owner!r}; "
            f"expected {expected_owner!r}"
        )


def _validate_checker(claim: InvariantClaim) -> None:
    """Reject a checker outside MIRA or a claim without a distinct second observer."""
    if not _DISTRIBUTION.fullmatch(claim.checker_distribution):
        raise ValueError(f"invalid checker distribution: {claim.checker_distribution!r}")
    if not _IMPORT.fullmatch(claim.checker_import):
        raise ValueError(f"invalid checker import: {claim.checker_import!r}")
    if claim.checker_distribution != _expected_distribution(claim.checker_import):
        raise ValueError("checker distribution and import name do not agree")
    expected_checker_owner = _CHECKER_OWNERS.get(claim.checker_distribution)
    if expected_checker_owner is None:
        raise ValueError(f"checker package is not MIRA: {claim.checker_distribution}")
    if claim.checker_owner != expected_checker_owner:
        raise ValueError(
            f"wrong checker owner for {claim.checker_distribution}: {claim.checker_owner!r}; "
            f"expected {expected_checker_owner!r}"
        )
    if claim.checker == claim.independent_checker:
        raise ValueError("producer checker and independent checker must be different")


def _validate_relationship(claim: InvariantClaim) -> None:
    """Reject an unattributed, degenerate or untriggered relationship."""
    if not _FAILURE_CODE.fullmatch(claim.failure_code):
        raise ValueError(f"invalid invariant failure code: {claim.failure_code!r}")
    if claim.invariant_id.replace("-", "_").upper() not in claim.failure_code:
        raise ValueError("failure code must attribute the invariant id")
    if (
        not isinstance(claim.relation, tuple)
        or len(claim.relation) != _RELATION_ENDPOINT_COUNT
        or any(not isinstance(endpoint, str) or not endpoint.strip() for endpoint in claim.relation)
    ):
        raise ValueError("invariant relation must name two non-empty endpoints")
    if claim.relation[0] == claim.relation[1]:
        raise ValueError("invariant relation endpoints must be distinct")
    if not claim.trigger_events or any(
        not isinstance(event_type, str) or not event_type.strip()
        for event_type in claim.trigger_events
    ):
        raise ValueError("invariant must declare at least one event trigger")


def _validate_claim(claim: InvariantClaim) -> None:
    """Reject a claim whose metadata cannot identify an owned observable relationship."""
    _validate_text_fields(claim)
    _validate_producer(claim)
    _validate_checker(claim)
    _validate_relationship(claim)


class InvariantRegistry:
    """Register and run the small set of MIRA relationship checkers."""

    def __init__(self) -> None:
        self._claims: dict[str, InvariantClaim] = {}
        self._checkers: dict[str, tuple[InvariantChecker, InvariantChecker, InvariantChecker]] = {}

    def register(
        self,
        claim: InvariantClaim,
        *,
        append_checker: InvariantChecker,
        replay_checker: InvariantChecker,
        independent_checker: InvariantChecker,
    ) -> None:
        """Register one claim and reject duplicate or non-independent checkers."""
        _validate_claim(claim)
        if claim.invariant_id in self._claims:
            raise ValueError(f"invariant already registered: {claim.invariant_id}")
        if not callable(append_checker) or not callable(replay_checker):
            raise TypeError("append_checker and replay_checker must be callable")
        if not callable(independent_checker):
            raise TypeError("independent_checker must be callable")
        if append_checker is independent_checker or replay_checker is independent_checker:
            raise ValueError("append/replay and independent checkers must be different callables")
        self._claims[claim.invariant_id] = claim
        self._checkers[claim.invariant_id] = (
            append_checker,
            replay_checker,
            independent_checker,
        )

    @property
    def claims(self) -> tuple[InvariantClaim, ...]:
        """Return claims in deterministic registration order."""
        return tuple(self._claims.values())

    def claim_for_control(self, control_id: str) -> InvariantClaim | None:
        """Return the claim attached to a control, when one is registered."""
        return next(
            (claim for claim in self._claims.values() if claim.control_id == control_id), None
        )

    def observe_append(
        self,
        *,
        run_id: str,
        event: Event,
        events: Sequence[Event],
        store: object | None = None,
    ) -> tuple[InvariantObservation, ...]:
        """Observe only claims triggered by a just-appended event.

        The event has already been persisted when this method runs.  A failed observation therefore
        produces a finding for later policy review and never rolls back or suppresses the evidence.
        """
        _validate_append_input(run_id, event, events)
        facts = InvariantInput(run_id, events, store, InvariantPhase.APPEND)
        observations: list[InvariantObservation] = []
        for claim in self._claims.values():
            if event.type.value not in claim.trigger_events:
                continue
            outcome = self._run(self._checkers[claim.invariant_id][0], facts)
            observations.append(self._observation(claim, InvariantPhase.APPEND, outcome))
        return tuple(observations)

    def observe_replay(
        self,
        *,
        run_id: str,
        events: Sequence[Event],
        store: object | None = None,
    ) -> tuple[InvariantObservation, ...]:
        """Replay all claims and compare each result with an independent second observation."""
        _validate_replay_input(run_id, events)
        facts = InvariantInput(run_id, events, store, InvariantPhase.REPLAY)
        independent_facts = InvariantInput(run_id, events, store, InvariantPhase.INDEPENDENT)
        observations: list[InvariantObservation] = []
        for claim in self._claims.values():
            replay = self._run(self._checkers[claim.invariant_id][1], facts)
            observations.append(self._observation(claim, InvariantPhase.REPLAY, replay))
            independent = self._run(self._checkers[claim.invariant_id][2], independent_facts)
            if replay.passed != independent.passed:
                independent = InvariantOutcome(
                    False,
                    "independent observer disagrees with replay observer",
                )
            observations.append(self._observation(claim, InvariantPhase.INDEPENDENT, independent))
        return tuple(observations)

    @staticmethod
    def _run(checker: InvariantChecker, facts: InvariantInput) -> InvariantOutcome:
        """Run one checker and validate its typed result."""
        try:
            outcome = checker(facts)
        except Exception as exc:  # noqa: BLE001  # checker failure must not erase durable evidence
            return InvariantOutcome(False, f"checker failed with {type(exc).__name__}")
        if not isinstance(outcome, InvariantOutcome):
            return InvariantOutcome(False, "checker returned invalid outcome")
        return outcome

    @staticmethod
    def _observation(
        claim: InvariantClaim, phase: InvariantPhase, outcome: InvariantOutcome
    ) -> InvariantObservation:
        """Attach the claim's stable failure code to a failed outcome."""
        return InvariantObservation(
            invariant_id=claim.invariant_id,
            control_id=claim.control_id,
            phase=phase,
            passed=outcome.passed,
            failure_code=None if outcome.passed else claim.failure_code,
            detail=outcome.detail,
        )


def _validate_append_input(run_id: str, event: Event, events: Sequence[Event]) -> None:
    """Require an append observation to use the newly persisted chain head."""
    if not events or (events[-1].event_id != event.event_id or events[-1].hash != event.hash):
        raise ValueError("append observation must use the newly appended chain head")
    if event.run_id != run_id or any(item.run_id != run_id for item in events):
        raise ValueError("append observation events must belong to the requested run")


def _validate_replay_input(run_id: str, events: Sequence[Event]) -> None:
    """Require replay facts to be scoped to one run."""
    if not events:
        raise ValueError("replay observation requires at least one durable event")
    if any(event.run_id != run_id for event in events):
        raise ValueError("replay observation events must belong to the requested run")


__all__ = [
    "InvariantChecker",
    "InvariantClaim",
    "InvariantInput",
    "InvariantObservation",
    "InvariantOutcome",
    "InvariantPhase",
    "InvariantRegistry",
]
