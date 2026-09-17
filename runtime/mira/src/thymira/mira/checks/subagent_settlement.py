"""A31: every delegation settled exactly once, and the parent acted on the canonical settlement.

`Delegator` settles its own children and refuses to let one reject its parent. That refusal is a
property of the producer, and a control that only asked the producer whether it had settled would
be no control at all. This module re-derives every fact from the persisted chain alone.

Nothing here imports :mod:`thymira.agents`, :mod:`thymira.core` or :mod:`thymira.thy`. Three
things the producer computed are **re-implemented on purpose**: the six-value stop-reason
vocabulary as MIRA's own literal frozenset, the delegation key
(:func:`_recompute_delegation_key`), and the canonical selector (:func:`_select_canonical`). That
duplication *is* the oracle, and it is the one documented exception to F7.5's "one shared
selector" rule -- a selector MIRA imported from the producer would not be one. It is the same
deliberate duplication :mod:`thymira.mira.checks.tool_intent_evidence` applies to the tool ticket
and :mod:`thymira.mira.checks.approval_scope_evidence` applies to the approval scope digest.

Each raw ``subagent.settled`` payload first crosses the shared ``SubagentResult`` schema boundary.
The validation result is retained as evidence, and only a validated record is consumed by the
identity, diagnostic and parent-rendering checks. Durable record counting still includes malformed
records, so a bad or repeated settlement cannot disappear from exactly-once accounting.

The facts held against each other come from three different writers, so no single producer can
satisfy them by asserting about itself:

- ``AgentRunner.run`` writes ``agent.started`` and ``agent.completed``, stamped with the
  delegation identity and the stop reason it observed;
- ``Delegator._settle`` writes ``subagent.settled``, the durable settlement;
- the parent writes ``agent.message``, the model-visible rendering it acted on.

Six conjuncts, each independently falsifiable and each listed in :data:`SETTLEMENT_CONJUNCTS`
so a test can remove one and measure what the control would have missed without it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from thymira.events import canonical_json, sha256_text
from thymira.mira.checks.models import AuditMode, ControlStatus
from thymira.schemas import EventType, SubagentResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event

SETTLEMENT_DIAGNOSTICS_LIMIT = 2000
"""MIRA's own bound on a settlement's diagnostics, held against the one the producer declares.

Deliberately a second constant rather than an import: a producer that quietly raised its own
bound would otherwise be self-certifying.
"""

STOP_REASONS = frozenset({"completed", "stopped", "out-of-room", "declined", "failed", "abnormal"})
"""The closed terminal vocabulary (F7.1), re-stated here rather than imported as an enum."""

_UNSTARTED_REASONS = frozenset({"stopped", "abnormal"})
"""Reasons that legitimately have no ``agent.started``: the orchestrator stopped the child before
it ran, or it died before or during the step without the runner recording an outcome."""

DELEGATION_KEY_PAYLOAD_KEY = "delegation_key"
"""The payload key a delegation-marked lifecycle event, a settlement and a message all carry."""


def _text(value: Any) -> str | None:  # any payload value, narrowed to an identity
    """A non-empty string from an untrusted payload value, or ``None`` for anything else."""
    return value if isinstance(value, str) and value else None


def _depth(value: Any) -> int | None:  # any payload value, narrowed to a real depth
    """A recorded delegation depth, or ``None``. ``bool`` is an ``int``, so it is excluded."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _validation_detail(error: ValidationError) -> str:
    """Describe invalid fields without copying untrusted input values into an audit detail."""
    locations = [
        ".".join(str(part) for part in item.get("loc", ())) or "record" for item in error.errors()
    ]
    fields = ", ".join(dict.fromkeys(locations))
    return f"invalid SubagentResult payload ({fields or 'record'})"


def _recompute_delegation_key(settlement: AuditedSettlement) -> str | None:
    """Fold the delegation key from the persisted invocation and parent identity fields."""
    if (
        settlement.run_id is None
        or settlement.task_id is None
        or settlement.agent_id is None
        or settlement.parent_agent is None
        or settlement.agent is None
        or settlement.objective is None
        or settlement.delegation_depth is None
    ):
        return None
    return sha256_text(
        canonical_json(
            {
                "run_id": settlement.run_id,
                "task_id": settlement.task_id,
                "agent_id": settlement.agent_id,
                "parent_agent": settlement.parent_agent,
                "agent": settlement.agent,
                "objective": settlement.objective,
                "delegation_depth": settlement.delegation_depth,
            }
        )
    )


def _select_canonical(candidates: Sequence[AuditedSettlement]) -> AuditedSettlement | None:
    """MIRA's own copy of the selector rule: the latest candidate in log order wins."""
    return candidates[-1] if candidates else None


@dataclass(frozen=True, slots=True)
class AuditedSettlement:
    """One ``subagent.settled`` payload as MIRA reads it -- untrusted until every field checks."""

    seq: int
    event_run_id: str
    run_id: str | None
    task_id: str | None
    agent_id: str | None
    subject_id: str | None
    delegation_key: str | None
    stop_reason: str | None
    result_json: str | None
    diagnostics: str | None
    diagnostics_limit: Any
    parent_agent: str | None
    agent: str | None
    objective: str | None
    delegation_depth: int | None
    validation_error: str | None = None

    @classmethod
    def from_event(cls, event: Event) -> AuditedSettlement:
        """Read and validate one settlement, retaining a safe detail for invalid raw JSON."""
        payload = event.payload
        validation_error: str | None = None
        try:
            validated = SubagentResult.model_validate(payload)
        except ValidationError as error:
            validated = None
            validation_error = _validation_detail(error)
        source = validated.model_dump(mode="json") if validated is not None else payload
        return cls(
            seq=event.seq,
            event_run_id=event.run_id,
            run_id=_text(source.get("run_id")),
            task_id=_text(source.get("task_id")),
            agent_id=_text(source.get("agent_id")),
            subject_id=event.subject_id,
            delegation_key=_text(source.get(DELEGATION_KEY_PAYLOAD_KEY)),
            stop_reason=_text(source.get("stop_reason")),
            result_json=source.get("result_json")
            if isinstance(source.get("result_json"), str)
            else None,
            diagnostics=source.get("diagnostics")
            if isinstance(source.get("diagnostics"), str)
            else None,
            diagnostics_limit=source.get("diagnostics_limit"),
            parent_agent=_text(source.get("parent_agent")),
            agent=_text(source.get("agent")),
            objective=_text(source.get("objective")),
            delegation_depth=_depth(source.get("delegation_depth")),
            validation_error=validation_error,
        )

    @property
    def is_valid(self) -> bool:
        """Whether the raw payload passed the shared settlement contract."""
        return self.validation_error is None


@dataclass(frozen=True, slots=True)
class _LifecycleObservation:
    """One delegation lifecycle event, keyed by its complete persisted invocation identity."""

    seq: int
    event_type: EventType
    event_run_id: str
    delegation_key: str
    task_id: str | None
    agent_id: str | None
    agent: str | None
    parent_agent: str | None
    objective: str | None
    objective_present: bool
    delegation_depth: int | None
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _MessageObservation:
    """One parent message that claims to render a delegation settlement."""

    seq: int
    subject_id: str | None
    delegation_key: str | None
    payload: dict[str, Any]


@dataclass
class SubagentSettlementLedger:
    """The delegation evidence a run's chain carries, folded in log order."""

    starts: dict[str, list[_LifecycleObservation]] = field(default_factory=dict)
    """``delegation_key`` -> delegation-marked ``agent.started`` observations."""
    runner_reasons: dict[str, list[_LifecycleObservation]] = field(default_factory=dict)
    """``delegation_key`` -> delegation-marked ``agent.completed`` observations."""
    settlements: list[AuditedSettlement] = field(default_factory=list)
    messages: dict[str, list[tuple[int, str | None, dict[str, Any]]]] = field(default_factory=dict)
    """``delegation_key`` -> the parent's own renderings, in log order."""
    message_observations: list[_MessageObservation] = field(default_factory=list)
    """Every message that carries the delegation-key field, including malformed or orphaned ones."""
    malformed_lifecycle: list[tuple[int, EventType]] = field(default_factory=list)
    """Delegation lifecycle events whose key field is present but unreadable."""

    def record(self, event: Event) -> None:
        """Fold one event into the delegation evidence."""
        if event.type is EventType.AGENT_STARTED:
            self._record_lifecycle(event, self._record_start)
        elif event.type is EventType.AGENT_COMPLETED:
            self._record_lifecycle(event, self._record_completion)
        elif event.type is EventType.SUBAGENT_SETTLED:
            self.settlements.append(AuditedSettlement.from_event(event))
        elif event.type is EventType.AGENT_MESSAGE:
            payload = dict(event.payload)
            if DELEGATION_KEY_PAYLOAD_KEY not in payload:
                if "parent_agent" in payload or "depth" in payload or "delegation_depth" in payload:
                    self.message_observations.append(
                        _MessageObservation(event.seq, event.subject_id, None, payload)
                    )
                return
            key = _text(payload.get(DELEGATION_KEY_PAYLOAD_KEY))
            self.message_observations.append(
                _MessageObservation(event.seq, event.subject_id, key, payload)
            )
            if key is not None:
                self.messages.setdefault(key, []).append((event.seq, event.subject_id, payload))

    def _record_lifecycle(
        self,
        event: Event,
        sink: Callable[[str, _LifecycleObservation], None],
    ) -> None:
        """Route lifecycle evidence by key, preserving every identity field for later matching."""
        if DELEGATION_KEY_PAYLOAD_KEY not in event.payload:
            if "parent_agent" in event.payload or "delegation_depth" in event.payload:
                self.malformed_lifecycle.append((event.seq, event.type))
            return
        key = _text(event.payload.get(DELEGATION_KEY_PAYLOAD_KEY))
        if key is None:
            self.malformed_lifecycle.append((event.seq, event.type))
            return
        observation = _LifecycleObservation(
            seq=event.seq,
            event_type=event.type,
            event_run_id=event.run_id,
            delegation_key=key,
            task_id=_text(event.payload.get("task_id")),
            agent_id=event.subject_id,
            agent=_text(event.payload.get("agent")),
            parent_agent=_text(event.payload.get("parent_agent")),
            objective=_text(event.payload.get("objective")),
            objective_present="objective" in event.payload,
            delegation_depth=_depth(event.payload.get("delegation_depth")),
            stop_reason=_text(event.payload.get("stop_reason")),
        )
        sink(key, observation)

    def _record_start(self, key: str, observation: _LifecycleObservation) -> None:
        self.starts.setdefault(key, []).append(observation)

    def _record_completion(self, key: str, observation: _LifecycleObservation) -> None:
        self.runner_reasons.setdefault(key, []).append(observation)

    @property
    def is_applicable(self) -> bool:
        """Whether this run recorded any delegation at all."""
        return bool(
            self.starts
            or self.runner_reasons
            or self.settlements
            or self.message_observations
            or self.malformed_lifecycle
        )

    def by_key(self) -> dict[str, list[AuditedSettlement]]:
        """Settlements grouped by their recorded delegation key, in log order."""
        grouped: dict[str, list[AuditedSettlement]] = {}
        for settlement in self.settlements:
            if settlement.delegation_key is not None:
                grouped.setdefault(settlement.delegation_key, []).append(settlement)
        return grouped


Problem = tuple[str, int]
"""One failure and the event sequence that evidences it."""

Conjunct = Callable[[SubagentSettlementLedger, AuditMode], list[Problem]]


def _valid_settlements(ledger: SubagentSettlementLedger) -> list[AuditedSettlement]:
    """Return only settlement payloads that passed the shared schema boundary."""
    return [settlement for settlement in ledger.settlements if settlement.is_valid]


def _valid_by_key(ledger: SubagentSettlementLedger) -> dict[str, list[AuditedSettlement]]:
    """Group only schema-valid settlements for relationship checks that consume their fields."""
    grouped: dict[str, list[AuditedSettlement]] = {}
    for settlement in _valid_settlements(ledger):
        if settlement.delegation_key is not None:
            grouped.setdefault(settlement.delegation_key, []).append(settlement)
    return grouped


def _same_invocation(settlement: AuditedSettlement, lifecycle: _LifecycleObservation) -> bool:
    """Compare lifecycle evidence with every persisted settlement identity field it can name."""
    if lifecycle.event_type is EventType.AGENT_STARTED:
        # AgentRunner always persists all seven invocation/parent identity fields on a start;
        # allowing an absent objective here would let a completion's intentional omission weaken
        # the start-to-settlement relationship.
        if not lifecycle.objective_present or lifecycle.objective is None:
            return False
    elif lifecycle.objective_present and (
        lifecycle.objective is None or lifecycle.objective != settlement.objective
    ):
        # The current completion record deliberately omits objective.  A present but malformed
        # or conflicting value is still evidence failure, and the allowance is completion-only.
        return False
    return (
        lifecycle.event_run_id == settlement.event_run_id
        and lifecycle.delegation_key == settlement.delegation_key
        and lifecycle.task_id == settlement.task_id
        and lifecycle.agent_id == settlement.agent_id
        and lifecycle.agent == settlement.agent
        and lifecycle.parent_agent == settlement.parent_agent
        and lifecycle.delegation_depth == settlement.delegation_depth
        and (
            lifecycle.event_type is EventType.AGENT_COMPLETED
            or lifecycle.objective == settlement.objective
        )
    )


def valid_subagent_result(ledger: SubagentSettlementLedger, mode: AuditMode) -> list[Problem]:
    """(1) Every settlement payload passes ``SubagentResult`` and belongs to its event run."""
    del mode
    problems: list[Problem] = []
    for settlement in ledger.settlements:
        if settlement.validation_error is not None:
            problems.append(
                (f"seq {settlement.seq}: {settlement.validation_error}", settlement.seq)
            )
        elif settlement.run_id != settlement.event_run_id:
            problems.append(
                (
                    f"seq {settlement.seq}: settlement run does not match its event run",
                    settlement.seq,
                )
            )
        elif settlement.stop_reason not in STOP_REASONS:
            problems.append(
                (
                    (
                        f"seq {settlement.seq}: stop reason {settlement.stop_reason!r} is not "
                        "one of the six"
                    ),
                    settlement.seq,
                )
            )
    return problems


def _lifecycle_cardinality_problems(ledger: SubagentSettlementLedger) -> list[Problem]:
    """Reject repeated keyed lifecycle or settlement records."""
    problems: list[Problem] = []
    for key, starts in ledger.starts.items():
        if len(starts) > 1:
            problems.append(
                (
                    f"delegation key {key}: {len(starts)} matching agent.started lifecycle records",
                    starts[1].seq,
                )
            )
    for key, completions in ledger.runner_reasons.items():
        if len(completions) > 1:
            problems.append(
                (
                    (
                        f"delegation key {key}: {len(completions)} matching agent.completed "
                        "lifecycle records"
                    ),
                    completions[1].seq,
                )
            )
    for key, candidates in ledger.by_key().items():
        if len(candidates) > 1:
            seqs = [settlement.seq for settlement in candidates]
            problems.append(
                (
                    f"delegation key {key}: {len(candidates)} settlements recorded, seqs {seqs}",
                    seqs[0],
                )
            )
    return problems


def _unsettled_lifecycle_problems(
    ledger: SubagentSettlementLedger, mode: AuditMode
) -> list[Problem]:
    """Reject terminal or final lifecycle evidence with no matching settlement."""
    problems: list[Problem] = []
    grouped = ledger.by_key()
    for key, completions in ledger.runner_reasons.items():
        if not grouped.get(key):
            problems.extend(
                (
                    f"seq {completion.seq}: orphan agent.completed has no settlement for its key",
                    completion.seq,
                )
                for completion in completions
            )
    for key, starts in ledger.starts.items():
        candidates = grouped.get(key, [])
        matching = [
            candidate
            for candidate in candidates
            if any(_same_invocation(candidate, start) for start in starts)
        ]
        if matching:
            continue
        if mode is not AuditMode.IN_FLIGHT:
            seq = starts[0].seq
            problems.append((f"delegation at seq {seq}: no settlement was recorded", seq))
        elif ledger.runner_reasons.get(key):
            seq = ledger.runner_reasons[key][0].seq
            problems.append(
                (f"seq {seq}: terminal lifecycle evidence has no settlement in flight", seq)
            )
    return problems


def _lifecycle_identity_problems(ledger: SubagentSettlementLedger) -> list[Problem]:
    """Reject keyed lifecycle facts that cannot belong to their valid settlement."""
    problems: list[Problem] = []
    for settlement in _valid_settlements(ledger):
        key = settlement.delegation_key
        if key is None:
            continue
        starts = ledger.starts.get(key, [])
        completions = ledger.runner_reasons.get(key, [])
        matching_starts = [start for start in starts if _same_invocation(settlement, start)]
        matching_completions = [
            completion for completion in completions if _same_invocation(settlement, completion)
        ]
        if len(matching_starts) != len(starts) or len(matching_completions) != len(completions):
            problems.append(
                (
                    (
                        f"seq {settlement.seq}: lifecycle evidence does not match its invocation "
                        "identity"
                    ),
                    settlement.seq,
                )
            )
    return problems


def exactly_one_settlement(ledger: SubagentSettlementLedger, mode: AuditMode) -> list[Problem]:
    """(2) Every invocation has one settlement and coherent lifecycle cardinality."""
    problems = [
        (f"seq {seq}: {event_type.value} has a malformed delegation identity", seq)
        for seq, event_type in ledger.malformed_lifecycle
    ]
    problems.extend(_lifecycle_cardinality_problems(ledger))
    problems.extend(_unsettled_lifecycle_problems(ledger, mode))
    problems.extend(_lifecycle_identity_problems(ledger))
    return problems


def diagnostics_within_the_declared_bound(
    ledger: SubagentSettlementLedger, mode: AuditMode
) -> list[Problem]:
    """(3) Valid settlement diagnostics fit the declared bound, and that bound is MIRA's own."""
    del mode
    problems: list[Problem] = []
    for settlement in _valid_settlements(ledger):
        limit = settlement.diagnostics_limit
        if isinstance(limit, bool) or not isinstance(limit, int):
            problems.append(
                (f"seq {settlement.seq}: no readable diagnostics bound", settlement.seq)
            )
            continue
        if limit != SETTLEMENT_DIAGNOSTICS_LIMIT:
            problems.append(
                (
                    (
                        f"seq {settlement.seq}: declares a bound of {limit}, not the "
                        f"{SETTLEMENT_DIAGNOSTICS_LIMIT} MIRA holds it to"
                    ),
                    settlement.seq,
                )
            )
            continue
        if settlement.diagnostics is not None and len(settlement.diagnostics) > limit:
            problems.append(
                (
                    (
                        f"seq {settlement.seq}: diagnostics of {len(settlement.diagnostics)} "
                        f"characters exceed the declared bound {limit}"
                    ),
                    settlement.seq,
                )
            )
    return problems


def _writer_identity_problems(
    settlement: AuditedSettlement,
    starts: Sequence[_LifecycleObservation],
    completions: Sequence[_LifecycleObservation],
    matching_starts: Sequence[_LifecycleObservation],
    matching_completions: Sequence[_LifecycleObservation],
) -> list[Problem]:
    """Report lifecycle facts that cannot identify the settlement's invocation."""
    problems: list[Problem] = []
    if starts and not matching_starts:
        problems.append(
            (
                (
                    f"seq {settlement.seq}: settles {settlement.stop_reason!r} with no "
                    "matching agent.started for its invocation"
                ),
                settlement.seq,
            )
        )
    if completions and not matching_starts:
        problems.append(
            (
                f"seq {settlement.seq}: agent.completed has no matching agent.started",
                settlement.seq,
            )
        )
    if completions and not matching_completions:
        problems.append(
            (
                f"seq {settlement.seq}: no matching agent.completed precedes the settlement",
                settlement.seq,
            )
        )
    return problems


def _unstarted_writer_problems(
    settlement: AuditedSettlement,
    starts: Sequence[_LifecycleObservation],
    completions: Sequence[_LifecycleObservation],
    matching_starts: Sequence[_LifecycleObservation],
    matching_completions: Sequence[_LifecycleObservation],
) -> list[Problem]:
    """Check lifecycle facts present for a stopped or abnormal invocation."""
    problems = _writer_identity_problems(
        settlement, starts, completions, matching_starts, matching_completions
    )
    if (not starts and not completions) or not matching_starts:
        return problems
    start = matching_starts[0]
    if start.seq >= settlement.seq:
        problems.append(
            (
                f"seq {settlement.seq}: agent.started does not precede the settlement",
                settlement.seq,
            )
        )
    if not completions or not matching_completions:
        return problems
    completion = matching_completions[0]
    if not start.seq < completion.seq < settlement.seq:
        problems.append(
            (
                (f"seq {settlement.seq}: lifecycle order is not started < completed < settled"),
                settlement.seq,
            )
        )
    if completion.stop_reason != settlement.stop_reason:
        problems.append(
            (
                (
                    f"seq {settlement.seq}: settles {settlement.stop_reason!r} while the "
                    f"runner recorded {completion.stop_reason!r} -- the two writers disagree"
                ),
                settlement.seq,
            )
        )
    return problems


def _terminal_writer_problems(
    settlement: AuditedSettlement,
    starts: Sequence[_LifecycleObservation],
    completions: Sequence[_LifecycleObservation],
    matching_starts: Sequence[_LifecycleObservation],
    matching_completions: Sequence[_LifecycleObservation],
) -> list[Problem]:
    """Check the exact ordering and reason agreement for a completed invocation."""
    problems = _writer_identity_problems(
        settlement, starts, completions, matching_starts, matching_completions
    )
    if not matching_starts and not starts:
        problems.append(
            (
                (
                    f"seq {settlement.seq}: settles {settlement.stop_reason!r} with no "
                    "matching agent.started for its invocation"
                ),
                settlement.seq,
            )
        )
    if not matching_completions and not completions:
        problems.append(
            (
                f"seq {settlement.seq}: no matching agent.completed precedes the settlement",
                settlement.seq,
            )
        )
    if not matching_starts or not matching_completions:
        return problems
    if len(matching_starts) != 1 or len(matching_completions) != 1:
        return problems
    start = matching_starts[0]
    completion = matching_completions[0]
    if not start.seq < completion.seq < settlement.seq:
        problems.append(
            (
                f"seq {settlement.seq}: lifecycle order is not started < completed < settled",
                settlement.seq,
            )
        )
    if completion.stop_reason != settlement.stop_reason:
        problems.append(
            (
                (
                    f"seq {settlement.seq}: settles {settlement.stop_reason!r} while the "
                    f"runner recorded {completion.stop_reason!r} -- the two writers disagree"
                ),
                settlement.seq,
            )
        )
    return problems


def the_two_writers_agree(ledger: SubagentSettlementLedger, mode: AuditMode) -> list[Problem]:
    """(4) The runner's identity and stop reason agree with each settlement.

    Normal settlements require one preceding ``agent.started`` and ``agent.completed`` with strict
    order ``started < completed < settled``. Stopped or abnormal settlements may omit completion
    after a crash, but any lifecycle facts they carry obey the same ordering and reason checks.
    Cardinality is independently owned by :func:`exactly_one_settlement`.
    """
    del mode
    problems: list[Problem] = []
    for settlement in _valid_settlements(ledger):
        if settlement.task_id is None or settlement.delegation_key is None:
            continue
        starts = ledger.starts.get(settlement.delegation_key, [])
        completions = ledger.runner_reasons.get(settlement.delegation_key, [])
        matching_starts = [start for start in starts if _same_invocation(settlement, start)]
        matching_completions = [
            completion for completion in completions if _same_invocation(settlement, completion)
        ]
        if settlement.stop_reason in _UNSTARTED_REASONS:
            problems.extend(
                _unstarted_writer_problems(
                    settlement,
                    starts,
                    completions,
                    matching_starts,
                    matching_completions,
                )
            )
        else:
            problems.extend(
                _terminal_writer_problems(
                    settlement,
                    starts,
                    completions,
                    matching_starts,
                    matching_completions,
                )
            )
    return problems


def parent_acted_on_the_canonical_settlement(
    ledger: SubagentSettlementLedger, mode: AuditMode
) -> list[Problem]:
    """(5) Every keyed parent message follows, and agrees with, its canonical settlement.

    MIRA re-derives the selection with its own rule and holds the parent's own rendering against
    it, so a parent that reported a stale or otherwise mismatched candidate is visible from the
    log alone. A missing message remains the documented crash-window exception; a message that is
    present must have a preceding settlement for its key.
    """
    del mode
    problems: list[Problem] = []
    grouped = _valid_by_key(ledger)
    for message in ledger.message_observations:
        if message.delegation_key is None:
            problems.append(
                (
                    f"seq {message.seq}: parent message has a malformed delegation key",
                    message.seq,
                )
            )
            continue
        candidates = [
            settlement
            for settlement in grouped.get(message.delegation_key, [])
            if settlement.seq < message.seq
        ]
        if not candidates:
            problems.append(
                (
                    f"seq {message.seq}: parent message has no preceding settlement for its key",
                    message.seq,
                )
            )
            continue
        canonical = _select_canonical(candidates)
        if canonical is None:  # pragma: no cover - a non-empty group always selects
            continue
        seq, subject_id, payload = message.seq, message.subject_id, message.payload
        if (
            subject_id != canonical.task_id
            or payload.get("task_id") != canonical.task_id
            or payload.get("parent_agent") != canonical.parent_agent
            or payload.get("agent") != canonical.agent
            or payload.get("depth") != canonical.delegation_depth
            or payload.get("stop_reason") != canonical.stop_reason
            or payload.get("summary") != canonical.result_json
        ):
            problems.append(
                (
                    (
                        f"seq {seq}: the parent acted on a settlement that disagrees with the "
                        f"canonical settlement at seq {canonical.seq}"
                    ),
                    seq,
                )
            )
    return problems


def the_delegation_key_recomputes(
    ledger: SubagentSettlementLedger, mode: AuditMode
) -> list[Problem]:
    """(6) Each settlement's recorded delegation key equals MIRA's recomputation of it."""
    del mode
    problems: list[Problem] = []
    for settlement in _valid_settlements(ledger):
        recomputed = _recompute_delegation_key(settlement)
        if recomputed is None:
            problems.append(
                (
                    f"seq {settlement.seq}: records no delegation identity to recompute a key from",
                    settlement.seq,
                )
            )
        elif recomputed != settlement.delegation_key:
            problems.append(
                (
                    f"seq {settlement.seq}: the recorded delegation key does not recompute",
                    settlement.seq,
                )
            )
    return problems


SETTLEMENT_CONJUNCTS: tuple[Conjunct, ...] = (
    valid_subagent_result,
    exactly_one_settlement,
    diagnostics_within_the_declared_bound,
    the_two_writers_agree,
    parent_acted_on_the_canonical_settlement,
    the_delegation_key_recomputes,
)
"""The six independently falsifiable conjuncts, in the order they are reported."""


def fold_settlements(events: Sequence[Event]) -> SubagentSettlementLedger:
    """Fold a run's chain into the delegation evidence A31 grades."""
    ledger = SubagentSettlementLedger()
    for event in events:
        ledger.record(event)
    return ledger


def check_subagent_settlement(
    events: Sequence[Event], mode: AuditMode
) -> tuple[ControlStatus, str, tuple[int, ...]]:
    """Grade every delegation the chain records, returning the offending sequences as evidence."""
    ledger = fold_settlements(events)
    if not ledger.is_applicable:
        return ControlStatus.NOT_APPLICABLE, "no delegation recorded", ()
    problems: list[Problem] = []
    for conjunct in SETTLEMENT_CONJUNCTS:
        problems.extend(conjunct(ledger, mode))
    if problems:
        return (
            ControlStatus.FAILED,
            "; ".join(detail for detail, _ in problems),
            tuple(seq for _, seq in problems),
        )
    return (
        ControlStatus.PASSED,
        f"{len(ledger.settlements)} delegation settlement(s), each recorded exactly once",
        (),
    )


__all__ = [
    "DELEGATION_KEY_PAYLOAD_KEY",
    "SETTLEMENT_CONJUNCTS",
    "SETTLEMENT_DIAGNOSTICS_LIMIT",
    "STOP_REASONS",
    "AuditedSettlement",
    "Conjunct",
    "Problem",
    "SubagentSettlementLedger",
    "check_subagent_settlement",
    "diagnostics_within_the_declared_bound",
    "exactly_one_settlement",
    "fold_settlements",
    "parent_acted_on_the_canonical_settlement",
    "the_delegation_key_recomputes",
    "the_two_writers_agree",
    "valid_subagent_result",
]
