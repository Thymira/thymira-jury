"""Deterministic audit controls over the event log and the artifact store.

Ported from the thesis meta-auditor (controls A1-A7, A9-A10, A16, A17) onto the Event API, and
extended with the MVP event-fold controls A8 (reviews resolved before run close), A15 (risk
classified before tool execution), A19 (confinement enforced) and A24 (routing respects the role
floor), then with the modelling-artifact controls A11 (split integrity), A20 (leakage indicators),
A12 (lineage coherence), A13 (model selection re-derived), A14/A21/A22/A23 (the CV, baseline and
reproducibility invariants), the report control A18 (report facts match the source artifacts), the
requirements-coverage control A26, and the evidence-sufficiency control A28, whose recomputation
lives in the sibling ``modelling``, ``report``, ``selection``, ``repro`` and ``coverage`` modules.
Doctrine kept verbatim: the auditor reconstructs the run from the events and the manifest, never
trusts a component's own success flags, and recomputes facts instead of importing the audited code.

The audit-freshness control A25 (``thymira.mira.checks.freshness.assess_audit_freshness``) is
deliberately passive: it consumes an :class:`AuditReport`, so it is not one of the ``CONTROLS`` that
``audit_run`` folds -- it runs on a finished report to ask whether that snapshot is still fresh.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any, cast

from thymira.events import canonical_json, sha256_text, verify_events
from thymira.mira.checks.coverage import check_requirements_coverage
from thymira.mira.checks.discovery_evidence import check_discovery_evidence
from thymira.mira.checks.invariants import (
    InvariantClaim,
    InvariantInput,
    InvariantOutcome,
    InvariantPhase,
    InvariantRegistry,
)
from thymira.mira.checks.modelling import (
    check_leakage_indicators,
    check_lineage_coherence,
    check_split_integrity,
)
from thymira.mira.checks.models import (
    AuditMode,
    AuditReport,
    AuditStatus,
    ControlResult,
    ControlStatus,
)
from thymira.mira.checks.report import check_report_fidelity
from thymira.mira.checks.repro import (
    check_baseline,
    check_fold_local_cv,
    check_reproducibility_metadata,
    check_test_once,
)
from thymira.mira.checks.selection import check_model_selection
from thymira.mira.checks.subagent_settlement import check_subagent_settlement
from thymira.mira.checks.termination_evidence import check_termination_evidence
from thymira.mira.checks.tool_authorization import DenialLedger, ToolAuthorizationLedger
from thymira.mira.checks.tool_intent_evidence import sandbox_modes_match
from thymira.mira.checks.workspace_quota import check_workspace_quota
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.mira.finding_safety import safe_finding
from thymira.schemas import (
    ActorKind,
    AuditFinding,
    Decision,
    Event,
    EventType,
    Evidence,
    Framework,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    approval_decision_id,
    approval_names_decision,
)

if TYPE_CHECKING:
    from datetime import datetime

    from thymira.mira.kb import RequirementsControls
    from thymira.state import ArtifactStore


@dataclass
class AuditContext:
    """Everything a control may look at."""

    run_id: str
    events: Sequence[Event]
    store: ArtifactStore | None = None
    policy_sha256: str | None = None
    frameworks: tuple[Framework, ...] = ()
    requirements_controls: RequirementsControls | None = None
    audit_mode: AuditMode = AuditMode.FINAL
    audited_at: datetime | None = None
    _by_seq: dict[int, Event] | None = field(default=None, init=False, repr=False)
    _by_type: dict[EventType, list[Event]] | None = field(default=None, init=False, repr=False)
    control_status: dict[str, ControlStatus] = field(default_factory=dict, init=False, repr=False)
    """What each control already concluded in this audit, filled by :func:`audit_run` in order.

    A control that needs another control's outcome reads it here instead of re-invoking its check:
    the requirements-coverage control (A26) is the one that does, and reading the recorded outcome
    is what keeps it consistent with the report it is published in.
    """

    def of(self, event_type: EventType) -> list[Event]:
        """Events of one type, in order."""
        if self._by_type is None:
            self._by_type = {}
            for event in self.by_seq.values():
                self._by_type.setdefault(event.type, []).append(event)
        return self._by_type.get(event_type, [])

    @property
    def by_seq(self) -> dict[int, Event]:
        """Return the event index built once for this audit context."""
        if self._by_seq is None:
            self._by_seq = {event.seq: event for event in self.events}
        return self._by_seq

    @property
    def run_failed(self) -> bool:
        """Whether the run ended in failure (lifecycle pairing is relaxed then)."""
        return bool(self.of(EventType.RUN_FAILED))


def _event_evidence(events: Sequence[Event], seqs: Iterable[int]) -> tuple[Evidence, ...]:
    """Return hash-pinned evidence for the selected event sequences, in selection order."""
    by_seq = {event.seq: event for event in events}
    return tuple(
        Evidence(kind="event", ref=f"seq:{seq}", sha256=by_seq[seq].hash)
        for seq in dict.fromkeys(seqs)
        if seq in by_seq
    )


def _artifact_evidence(
    store: ArtifactStore | None,
    names: Iterable[str],
    *,
    note: str | None = None,
) -> tuple[Evidence, ...]:
    """Return evidence for the selected manifest entries, preserving first-seen order."""
    if store is None:
        return ()
    evidence: list[Evidence] = []
    for name in dict.fromkeys(names):
        artifact = store.get(name)
        evidence.append(
            Evidence(
                kind="artifact",
                ref=name,
                sha256=artifact.sha256 if artifact is not None else None,
                note=note,
            )
        )
    return tuple(evidence)


def _approval_answers_request(event: Event) -> bool:
    """Whether an approval event is a valid answer for A7/A8 resolution folds.

    The system actor represents a Gate's automatic response and retires the request without
    becoming human authority. A human answer requires authenticated provenance; a declared human
    identity must remain unresolved in the independent audit.
    """
    if not isinstance(event.payload.get("approved"), bool):
        return False
    if event.actor.kind is ActorKind.SYSTEM:
        return True
    return (
        event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
    )


def _manifest_evidence(store: ArtifactStore | None) -> Evidence | None:
    """Return the hash of the manifest A5 compared against its files, when a store exists."""
    if store is None:
        return None
    return Evidence(
        kind="artifact",
        ref="manifest.json",
        sha256=artifact_manifest_sha256(store),
        note="artifact manifest",
    )


def _artifact_names_from_problems(problems: Iterable[str]) -> tuple[str, ...]:
    """Extract manifest names from the local store's verification messages."""
    names: list[str] = []
    for problem in problems:
        prefix, separator, name = problem.partition(": ")
        if separator and prefix in {"missing artifact", "modified artifact"}:
            names.append(name)
    return tuple(dict.fromkeys(names))


_SEQ_RE = re.compile(r"\bseq(?:s)?\s*(?:\[([^]]*)\]|(\d+))")
_QUOTED_ARTIFACT_RE = re.compile(r"['\"]([^'\"]+\.[A-Za-z0-9]+)['\"]")


def _seqs_from_detail(detail: str) -> tuple[int, ...]:
    """Extract the event sequences a control printed in its detail."""
    seqs: list[int] = []
    for many, one in _SEQ_RE.findall(detail):
        values = many.split(",") if many else [one]
        seqs.extend(int(value.strip()) for value in values if value.strip().isdigit())
    return tuple(dict.fromkeys(seqs))


def _artifact_names_from_detail(detail: str) -> tuple[str, ...]:
    """Extract quoted file-shaped references a control printed in its detail."""
    return tuple(dict.fromkeys(_QUOTED_ARTIFACT_RE.findall(detail)))


def _active_artifact_names(
    ctx: AuditContext,
    *,
    kinds: Iterable[str] = (),
    payload_keys: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return active artifacts carrying one of the evidence shapes a control reads."""
    if ctx.store is None:
        return ()
    wanted_kinds = frozenset(kinds)
    wanted_keys = frozenset(payload_keys)
    names: list[str] = []
    for artifact in ctx.store.list_active():
        if wanted_kinds and artifact.kind.value not in wanted_kinds:
            continue
        if wanted_keys:
            if not artifact.name.endswith(".json"):
                continue
            try:
                payload = ctx.store.load_json(artifact.name)
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or not wanted_keys.intersection(payload):
                continue
        names.append(artifact.name)
    return tuple(names)


def _with_evidence(
    outcome: tuple[ControlStatus, str],
    severity: Severity,
    evidence: tuple[Evidence, ...],
) -> CheckResult:
    """Attach explicit evidence to a delegated control outcome when it fails."""
    status, detail = outcome
    return (status, detail, severity, evidence) if status is ControlStatus.FAILED else outcome


# ----------------------------------------------------------------------------- controls


def a1_chain_integrity(ctx: AuditContext) -> CheckResult:
    """The hash chain verifies end to end."""
    result = verify_events(ctx.events)
    if result.valid:
        return ControlStatus.PASSED, f"{result.event_count} events"
    return (
        ControlStatus.FAILED,
        result.error or "invalid",
        Severity.CRITICAL,
        _event_evidence(ctx.events, (result.event_count,)),
    )


def a2_run_closed(ctx: AuditContext) -> CheckResult:
    """A run that started was closed (completed or failed)."""
    started = ctx.of(EventType.RUN_STARTED)
    if not started:
        return ControlStatus.NOT_APPLICABLE, "no run.started event"
    if ctx.audit_mode is AuditMode.IN_FLIGHT:
        return ControlStatus.NOT_APPLICABLE, "run closeout pending while audit is in flight"
    if ctx.of(EventType.RUN_COMPLETED) or ctx.of(EventType.RUN_FAILED):
        return ControlStatus.PASSED, "run closed"
    return (
        ControlStatus.FAILED,
        "run.started without run.completed/run.failed",
        Severity.HIGH,
        _event_evidence(ctx.events, (event.seq for event in started)),
    )


def a3_authorization_before_tool(ctx: AuditContext) -> CheckResult:
    """Every tool execution was preceded by an allowing policy decision.

    The decision is the one the start's own event names, or -- for a start that names none, or
    one the log never recorded -- the latest decision for its own subject, which must be a
    decision about a tool call either way. Naming *another* call's decision is a claim to the
    one-shot ticket a human granted for one exact call, and
    :class:`~thymira.mira.checks.tool_authorization.ToolAuthorizationLedger` recomputes that
    ticket the way ``ToolManager`` enforces it -- same call and same tool, answers pooled per
    ticket, any human rejection final, one execution per answer -- instead of believing the id
    stamped on the event. A review answered on the start's own subject is spent the same way.
    """
    starts = ctx.of(EventType.TOOL_STARTED)
    if not starts:
        return ControlStatus.NOT_APPLICABLE, "no tool executions"
    ledger = ToolAuthorizationLedger()
    completed = {
        event.subject_id: event
        for event in ctx.of(EventType.TOOL_COMPLETED)
        if event.subject_id is not None
    }
    unauthorised: list[int] = []
    for event in ctx.by_seq.values():
        if event.type is EventType.TOOL_STARTED:
            result = completed.get(event.subject_id)
            if not ledger.authorizes(event) or (
                result is not None and not sandbox_modes_match(event, result)
            ):
                unauthorised.append(event.seq)
        else:
            ledger.record(event)
    if unauthorised:
        return (
            ControlStatus.FAILED,
            f"tool.started without an allowing decision at seq {unauthorised}",
            Severity.CRITICAL,
            _event_evidence(ctx.events, unauthorised),
        )
    return ControlStatus.PASSED, f"{len(starts)} executions authorised"


def a4_nothing_after_block(ctx: AuditContext) -> CheckResult:
    """No tool ran after a BLOCK on its subject, nor after an audit block on the run."""
    blocked_subjects: dict[str | None, int] = {}
    audit_block_seq: int | None = None
    violations: list[int] = []
    for event in ctx.events:
        if (
            event.type is EventType.POLICY_DECISION
            and event.payload.get("decision") == Decision.BLOCK.value
        ):
            blocked_subjects.setdefault(event.subject_id, event.seq)
        elif event.type is EventType.AUDIT_BLOCK and audit_block_seq is None:
            audit_block_seq = event.seq
        elif event.type is EventType.TOOL_STARTED and (
            event.subject_id in blocked_subjects or audit_block_seq is not None
        ):
            violations.append(event.seq)
    if violations:
        return (
            ControlStatus.FAILED,
            f"tool.started after a BLOCK at seq {violations}",
            Severity.CRITICAL,
            _event_evidence(ctx.events, violations),
        )
    if not blocked_subjects and audit_block_seq is None:
        return ControlStatus.NOT_APPLICABLE, "no BLOCK decisions"
    return ControlStatus.PASSED, "no execution after a block"


def a5_artifact_integrity(ctx: AuditContext) -> CheckResult:
    """Every manifest entry is present and matches its recorded hash."""
    if ctx.store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    problems = ctx.store.verify()
    if not problems:
        return ControlStatus.PASSED, "manifest verified"
    evidence = list(_artifact_evidence(ctx.store, _artifact_names_from_problems(problems)))
    if manifest := _manifest_evidence(ctx.store):
        evidence.append(manifest)
    return ControlStatus.FAILED, "; ".join(problems), Severity.HIGH, tuple(evidence)


def a6_denials_recorded(ctx: AuditContext) -> CheckResult:
    """Every denied tool call points at a non-allowing decision.

    The refusal is one recorded for its subject; the decision the denial names, verified against
    that review's own ticket; a non-allowing run-level decision such as a budget refusal; or an
    allowing run-level decision whose recorded constraints produce the exact refusal. A duplicated
    decision id explains no denial through that id. A human's rejection of the ticket or the
    exact agent-allowlist refusal also explains the call
    (:class:`~thymira.mira.checks.tool_authorization.DenialLedger`). Only a human, non-automatic
    approval retires a review, so the manager's fail-closed refusal after its *automatic*
    approver answered is explained rather than reported, and a rejected review stays non-allowing
    for the retry it denies.

    Still reported: the denial a Gate in ``SYNCHRONOUS`` mode leaves when a *human* approver says
    yes in process -- the answer is evidence, never authority, so the manager refuses the call it
    was asked about and nothing in the log refuses it (including a synchronous budget review).
    Task 7 removes that shape. Wiring refusals with no recorded execution decision or constraints
    that differ from the recorded decision remain unexplained: no valid policy evidence backs them.
    """
    denials = ctx.of(EventType.TOOL_DENIED)
    if not denials:
        return ControlStatus.NOT_APPLICABLE, "no denials"
    ledger = DenialLedger()
    unexplained: list[int] = []
    for event in ctx.by_seq.values():
        if event.type is EventType.TOOL_DENIED:
            if not ledger.explains(event):
                unexplained.append(event.seq)
        else:
            ledger.record(event)
    if unexplained:
        return (
            ControlStatus.FAILED,
            f"tool.denied without a denying decision at seq {unexplained}",
            Severity.MEDIUM,
            _event_evidence(ctx.events, unexplained),
        )
    return ControlStatus.PASSED, f"{len(denials)} denials explained"


def a7_approvals_resolved(ctx: AuditContext) -> CheckResult:
    """Every approval request got a recorded human answer.

    Both sides are read through :func:`~thymira.schemas.approval_decision_id`, which knows the two
    field names one decision id is recorded under: requests are written with ``decision_id`` by
    both Gate paths, while answers carry ``policy_decision_id`` whenever they are a serialised
    ``Approval``. A payload that names no decision contributes nothing to the answered set and can
    never satisfy a request, so tolerating two names never makes this control vacuous.
    """
    requests = ctx.of(EventType.HUMAN_APPROVAL_REQUESTED)
    if not requests:
        return ControlStatus.NOT_APPLICABLE, "no approval requests"
    answered = {
        decision_id
        for e in ctx.of(EventType.HUMAN_APPROVAL)
        if _approval_answers_request(e)
        if (decision_id := approval_decision_id(e.payload)) is not None
    }
    pending = [r.seq for r in requests if approval_decision_id(r.payload) not in answered]
    if pending:
        return (
            ControlStatus.FAILED,
            f"approval requested without an answer at seq {pending}",
            Severity.HIGH,
            _event_evidence(ctx.events, pending),
        )
    return ControlStatus.PASSED, f"{len(requests)} requests answered"


_LIFECYCLE_PAIRS: tuple[tuple[EventType, tuple[EventType, ...]], ...] = (
    (EventType.AGENT_STARTED, (EventType.AGENT_COMPLETED,)),
    (EventType.TOOL_STARTED, (EventType.TOOL_COMPLETED,)),
)
"""The start/end event kinds A9 pairs, by the ``subject_id`` each of them carries."""


def _identified_subject(event: Event) -> str | None:
    """The subject an event identifies, or ``None`` when it identifies nobody."""
    subject = event.subject_id
    return subject if isinstance(subject, str) and subject else None


@dataclass(frozen=True)
class _PairingGap:
    """What one lifecycle kind proves about pairing, and what it leaves undecidable.

    ``dangling`` holds the starts that no recorded end could belong to -- proof of an unfinished
    lifecycle. ``unidentified`` counts the starts nothing identifies and ``covered`` the identified
    starts whose only candidate end identifies nobody; neither group can be decided either way
    from this evidence.
    """

    dangling: tuple[Event, ...]
    unidentified: int
    covered: int


def _pairing_gap(starts: Sequence[Event], ends: Sequence[Event]) -> _PairingGap:
    """Split one lifecycle kind's starts into the proven-dangling ones and the undecidable ones.

    ``None`` is not an identity. A start nothing identifies can be matched by nothing, and an end
    nothing identifies could belong to any unmatched start -- so it convicts nobody, but it also
    excuses at most one start: every unmatched start beyond the number of unidentified ends is
    dangling however those ends are assigned.
    """
    ended = {subject for e in ends if (subject := _identified_subject(e)) is not None}
    anonymous_ends = sum(1 for e in ends if _identified_subject(e) is None)
    unidentified = sum(1 for e in starts if _identified_subject(e) is None)
    unmatched = [
        e
        for e in starts
        if (subject := _identified_subject(e)) is not None and subject not in ended
    ]
    covered = min(anonymous_ends, len(unmatched))
    return _PairingGap(tuple(unmatched[covered:]), unidentified, covered)


def _unverifiable_pairing(starts: int, unidentified: int, covered: int) -> str:
    """Say which lifecycle starts A9 could not decide, and why."""
    reasons = []
    if unidentified:
        reasons.append(f"{unidentified} of {starts} lifecycle starts identify no subject")
    if covered:
        reasons.append(f"{covered} could only be closed by an end that identifies none")
    return "pairing not verifiable: " + "; ".join(reasons)


_DELEGATION_KEY_FIELD = "delegation_key"
_UNSTARTED_DELEGATION_REASONS = frozenset({"stopped", "abnormal"})


def _delegation_event_groups(
    events: Sequence[Event],
) -> tuple[dict[str, list[Event]], dict[str, list[Event]], dict[str, list[Event]], list[Event]]:
    """Group keyed delegation lifecycle events while retaining malformed markers."""
    starts: dict[str, list[Event]] = {}
    completions: dict[str, list[Event]] = {}
    settlements: dict[str, list[Event]] = {}
    malformed: list[Event] = []
    for event in events:
        if event.type is EventType.SUBAGENT_SETTLED:
            destination = settlements
        elif event.type is EventType.AGENT_STARTED:
            destination = starts
        elif event.type is EventType.AGENT_COMPLETED:
            destination = completions
        else:
            continue
        if _DELEGATION_KEY_FIELD not in event.payload:
            continue
        key = event.payload.get(_DELEGATION_KEY_FIELD)
        if not isinstance(key, str) or not key:
            malformed.append(event)
            continue
        destination.setdefault(key, []).append(event)
    return starts, completions, settlements, malformed


def _delegation_cardinality_problems(
    key: str,
    starts: Sequence[Event],
    completions: Sequence[Event],
    settlements: Sequence[Event],
) -> list[tuple[str, int]]:
    """Reject repeated keyed records for one invocation."""
    problems: list[tuple[str, int]] = []
    if len(starts) > 1:
        problems.append(
            (
                f"delegation key {key}: duplicate agent.started lifecycle records",
                starts[1].seq,
            )
        )
    if len(completions) > 1:
        problems.append(
            (
                f"delegation key {key}: duplicate agent.completed lifecycle records",
                completions[1].seq,
            )
        )
    if len(settlements) > 1:
        problems.append((f"delegation key {key}: duplicate settlement records", settlements[1].seq))
    return problems


def _delegation_order_problems(
    key: str,
    starts: Sequence[Event],
    completions: Sequence[Event],
    settlements: Sequence[Event],
    mode: AuditMode,
) -> list[tuple[str, int]]:
    """Reject impossible orderings and terminal records for one keyed invocation."""
    if not settlements:
        if completions or (starts and mode is not AuditMode.IN_FLIGHT):
            evidence = completions or starts
            return [
                (
                    f"delegation key {key}: lifecycle evidence has no settlement",
                    evidence[0].seq,
                )
            ]
        return []

    settlement = settlements[-1]
    if completions and not starts:
        return [
            (
                f"seq {settlement.seq}: agent.completed has no matching agent.started",
                settlement.seq,
            )
        ]
    if not starts:
        return []

    start = starts[0]
    problems: list[tuple[str, int]] = []
    if start.seq >= settlement.seq:
        problems.append((f"seq {start.seq}: agent.started does not precede settlement", start.seq))
    if completions:
        completion = completions[0]
        if not start.seq < completion.seq < settlement.seq:
            problems.append(
                (
                    (
                        f"seq {completion.seq}: delegation lifecycle order is not "
                        "started < completed < settled"
                    ),
                    completion.seq,
                )
            )
    elif settlement.payload.get("stop_reason") not in _UNSTARTED_DELEGATION_REASONS:
        problems.append(
            (
                f"seq {settlement.seq}: completed delegation has no agent.completed",
                settlement.seq,
            )
        )
    return problems


def _delegation_lifecycle_problems(
    events: Sequence[Event], mode: AuditMode
) -> tuple[tuple[str, int], ...]:
    """Check ordering and cardinality of keyed delegation lifecycle evidence.

    A9's historical subject pairing remains in place for direct and legacy events. Delegated
    events carry a stronger invocation key, however, and a set-membership check would let a
    repeated or reordered record pass. These independent checks use only raw event facts; A31
    still owns the complete settlement identity and result oracle.
    """
    starts, completions, settlements, malformed = _delegation_event_groups(events)

    problems: list[tuple[str, int]] = [
        (f"seq {event.seq}: malformed delegation identity", event.seq) for event in malformed
    ]
    for key in sorted(set(starts) | set(completions) | set(settlements)):
        problems.extend(
            _delegation_cardinality_problems(
                key,
                starts.get(key, []),
                completions.get(key, []),
                settlements.get(key, []),
            )
        )
        problems.extend(
            _delegation_order_problems(
                key,
                starts.get(key, []),
                completions.get(key, []),
                settlements.get(key, []),
                mode,
            )
        )
    return tuple(problems)


def _delegated_unpaired_starts_allowed(events: Sequence[Event], mode: AuditMode) -> frozenset[int]:
    """Return keyed starts that are pending or legitimately lack a completion."""
    starts, completions, settlements, _ = _delegation_event_groups(events)
    allowed: set[int] = set()
    for key, keyed_starts in starts.items():
        if key in completions:
            continue
        keyed_settlements = settlements.get(key, [])
        if keyed_settlements:
            settlement = keyed_settlements[-1]
            if settlement.payload.get("stop_reason") in _UNSTARTED_DELEGATION_REASONS and all(
                start.seq < settlement.seq for start in keyed_starts
            ):
                allowed.update(start.seq for start in keyed_starts)
        elif mode is AuditMode.IN_FLIGHT:
            allowed.update(start.seq for start in keyed_starts)
    return frozenset(allowed)


def a9_lifecycle_pairing(ctx: AuditContext) -> CheckResult:
    """Agents and tools that started also finished (unless the run failed).

    ``subject_id`` is the pairing identity, and it is only as old as the writers that stamp it:
    every lifecycle event recorded before them carries ``None``, and ``events.jsonl`` is
    append-only, so no writer can repair that evidence -- only this reader can. Pairing was set
    membership, which made ``None`` an identity: the started set was ``{None}``, the ended set was
    ``{None}``, and an agent that started and died read as paired, so A9 returned PASSED on
    exactly the evidence it exists to police.

    ``None`` therefore identifies nobody, which leaves three honest outcomes. A start no recorded
    end could belong to is proof of an unfinished lifecycle: FAILED. A log whose starts are all
    identified and all matched is verified: PASSED. A start nothing identifies -- or one whose only
    candidate end identifies nobody, which is what a run spanning that upgrade looks like from
    here -- can be decided neither way, so the control reports the evidence it audits as absent:
    NOT_APPLICABLE, the status every other control uses for missing evidence and the one A26 reads
    as a coverage gap, with the reason in the detail. PASSED there would assert a pairing nothing
    verified; FAILED would raise a MEDIUM finding on every run recorded before the writers stamped
    a subject, for a defect no evidence shows.
    """
    if ctx.run_failed:
        return ControlStatus.NOT_APPLICABLE, "run failed; pairing relaxed"
    delegated_problems = _delegation_lifecycle_problems(ctx.events, ctx.audit_mode)
    if delegated_problems:
        return (
            ControlStatus.FAILED,
            "; ".join(detail for detail, _ in delegated_problems),
            Severity.MEDIUM,
            _event_evidence(ctx.events, (seq for _, seq in delegated_problems)),
        )
    pending_delegated_starts = _delegated_unpaired_starts_allowed(ctx.events, ctx.audit_mode)
    dangling: list[str] = []
    dangling_events: list[Event] = []
    starts = unidentified = covered = 0
    for start_type, end_types in _LIFECYCLE_PAIRS:
        started = ctx.of(start_type)
        gap = _pairing_gap(started, [e for end_type in end_types for e in ctx.of(end_type)])
        if pending_delegated_starts:
            gap = _PairingGap(
                tuple(event for event in gap.dangling if event.seq not in pending_delegated_starts),
                gap.unidentified,
                gap.covered,
            )
        starts += len(started)
        dangling_events.extend(gap.dangling)
        dangling.extend(f"{start_type}:{event.subject_id}" for event in gap.dangling)
        unidentified += gap.unidentified
        covered += gap.covered
    if dangling:
        return (
            ControlStatus.FAILED,
            f"unfinished: {dangling}",
            Severity.MEDIUM,
            _event_evidence(ctx.events, (event.seq for event in dangling_events)),
        )
    if not starts:
        return ControlStatus.NOT_APPLICABLE, "no agent or tool lifecycle events"
    if unidentified or covered:
        return ControlStatus.NOT_APPLICABLE, _unverifiable_pairing(starts, unidentified, covered)
    return ControlStatus.PASSED, "every start has an end"


# The store's own wording for a revision it archived under ``.history`` before overwriting the
# live file (``LocalArtifactStore._archive_existing``); MIRA matches the phrase, not the class.
_SUPERSEDED_PREFIX = "superseded by a new revision of "


def _superseded_revisions(store: ArtifactStore) -> dict[str, set[str]]:
    """Return, per logical name, the digests of the revisions the store archived for that name.

    Only an entry the store itself marked as superseded *by a new revision of the same name*
    counts; a manual ``invalidate`` reason, or the phrase naming another artifact, does not.
    """
    superseded: dict[str, set[str]] = {}
    for entry in store.manifest().values():
        if entry.valid or entry.invalidated_reason != _SUPERSEDED_PREFIX + entry.name:
            continue
        superseded.setdefault(entry.name, set()).add(entry.sha256)
    return superseded


def a10_declared_artifacts(ctx: AuditContext) -> CheckResult:
    """Every announced artifact exists and still carries the digest the log recorded for it.

    The manifest is ordinary JSON that nothing chains, so an attacker who rewrites an artifact can
    rewrite its manifest entry to match and satisfy :func:`a5_artifact_integrity`. The digest
    announced in the ``artifact.created`` event cannot be rewritten that way — it is inside the
    hash chain. Comparing the two closes the loop event -> manifest -> file, and is the difference
    between recomputing from evidence and trusting the store's own bookkeeping.

    A logical name may be announced more than once: a step that parks twice writes its resume
    bundle under the same name each time. Every announcement is evidence, and the store keeps the
    revision each one describes -- live, or archived under ``.history`` when a later write
    superseded it (``preserve_history``) -- so an announced digest is resolved against the
    revision that carries it, never only against the live file. An announcement no held revision
    backs is a failure whichever it was: rewriting an artifact and announcing the new digest does
    not erase the old announcement, and deleting the archived revision does not satisfy it.
    """
    created = ctx.of(EventType.ARTIFACT_CREATED)
    if not created or ctx.store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact events or no store"
    superseded_digests = _superseded_revisions(ctx.store)
    announced: dict[str, set[str]] = {}
    last_announcement: dict[str, Event] = {}
    missing: list[str] = []
    tampered: list[str] = []
    missing_events: list[Event] = []
    tampered_events: list[Event] = []
    archived = 0
    for event in created:
        name = str(event.payload.get("name"))
        if not ctx.store.exists(name):
            missing.append(name)
            missing_events.append(event)
            continue
        declared = event.payload.get("sha256")
        recorded = ctx.store.get(name)
        if not declared or recorded is None:
            continue
        announced.setdefault(name, set()).add(str(declared))
        last_announcement[name] = event
        if recorded.sha256 == declared:
            continue
        if str(declared) in superseded_digests.get(name, ()):
            archived += 1
            continue
        tampered.append(name)
        tampered_events.append(event)
    # The live revision must itself be one the chain announced. Without this half, overwriting an
    # announced artifact through the store's own API -- which archives the announced revision --
    # would satisfy every announcement while the live bytes answer to nothing on the log.
    unannounced = [
        name
        for name, digests in announced.items()
        if (live := ctx.store.get(name)) is not None and live.sha256 not in digests
    ]
    if missing:
        evidence = _event_evidence(ctx.events, (event.seq for event in missing_events))
        evidence += _artifact_evidence(ctx.store, missing)
        return (
            ControlStatus.FAILED,
            f"announced but missing/invalid: {missing}",
            Severity.HIGH,
            evidence,
        )
    if tampered:
        evidence = _event_evidence(ctx.events, (event.seq for event in tampered_events))
        evidence += _artifact_evidence(ctx.store, tampered)
        return (
            ControlStatus.FAILED,
            f"digest differs from the one recorded on the log: {tampered}",
            Severity.HIGH,
            evidence,
        )
    if unannounced:
        evidence = _event_evidence(
            ctx.events, (last_announcement[name].seq for name in unannounced)
        )
        evidence += _artifact_evidence(ctx.store, unannounced)
        return (
            ControlStatus.FAILED,
            f"live revision was never announced on the log: {unannounced}",
            Severity.HIGH,
            evidence,
        )
    detail = f"{len(created)} artifacts present"
    if archived:
        detail += f" ({archived} superseded revisions held in the store's history)"
    return ControlStatus.PASSED, detail


def a16_policy_snapshot(ctx: AuditContext) -> CheckResult:
    """All decisions were taken under one policy snapshot (the expected one, if known)."""
    decisions = ctx.of(EventType.POLICY_DECISION)
    if not decisions:
        return ControlStatus.NOT_APPLICABLE, "no policy decisions"
    missing = [
        event.seq
        for event in decisions
        if not isinstance(event.payload.get("policy_sha256"), str)
        or not event.payload.get("policy_sha256")
    ]
    if missing:
        return (
            ControlStatus.FAILED,
            f"policy snapshot missing at decision seqs {missing}",
            Severity.HIGH,
            _event_evidence(ctx.events, missing),
        )
    hashes = {event.payload["policy_sha256"] for event in decisions}
    if len(hashes) > 1:
        return (
            ControlStatus.FAILED,
            f"decisions under {len(hashes)} different policy snapshots",
            Severity.HIGH,
            _event_evidence(ctx.events, (event.seq for event in decisions)),
        )
    if ctx.policy_sha256 is not None and hashes != {ctx.policy_sha256}:
        return (
            ControlStatus.FAILED,
            "decisions do not match the expected policy snapshot",
            Severity.HIGH,
            _event_evidence(ctx.events, (event.seq for event in decisions)),
        )
    return ControlStatus.PASSED, f"single snapshot {next(iter(hashes))[:12]}"


def a17_provenance(ctx: AuditContext) -> CheckResult:
    """The run recorded its environment (provenance) at start."""
    started = ctx.of(EventType.RUN_STARTED)
    if not started:
        return ControlStatus.NOT_APPLICABLE, "no run.started event"
    if "run_environment" in started[0].payload or (
        ctx.store is not None and ctx.store.exists("run_environment.json")
    ):
        return ControlStatus.PASSED, "provenance recorded"
    return (
        ControlStatus.FAILED,
        "no run_environment payload or artifact",
        Severity.LOW,
        _event_evidence(ctx.events, (started[0].seq,)),
    )


# --------------------------------------------------- MVP event-fold controls (A8, A15, A19, A24)


RISK_CLASSIFIER_AGENT = "risk-classifier"
"""The ``agent`` marker ``thymira.agents.risk`` stamps on its classification ``agent.message``."""


def a8_reviews_resolved(ctx: AuditContext) -> CheckResult:
    """Every review decision got a human answer before the run reached a terminal state.

    Distinct from A7 (approval *requests* answered): A8 keys off the ``policy.decision`` events that
    *require* review and adds the ordering constraint that the answer precede run close. The
    inherited *analytical-decision* meaning of A8 (``docs/legacy/trazabilidad.md:108``) is not
    revived here; the scoped meaning is the unresolved-review channel (roadmap ``CTRL-MVP``).

    The pairing goes through :func:`~thymira.schemas.approval_names_decision` rather than an
    equality test on the two ids: a decision payload with no ``id`` and an approval naming no
    decision are both ``None``, and ``None == None`` marked an unanswerable review resolved.
    """
    reviews = [
        event
        for event in ctx.of(EventType.POLICY_DECISION)
        if event.payload.get("decision") == Decision.REQUIRE_HUMAN_REVIEW.value
    ]
    if not reviews:
        return ControlStatus.NOT_APPLICABLE, "no REQUIRE_HUMAN_REVIEW decisions"
    terminals = [
        event.seq for event in (*ctx.of(EventType.RUN_COMPLETED), *ctx.of(EventType.RUN_FAILED))
    ]
    terminal_seq = min(terminals) if terminals else None
    approvals = [
        approval
        for approval in ctx.of(EventType.HUMAN_APPROVAL)
        if _approval_answers_request(approval)
    ]
    unresolved = [
        review.seq
        for review in reviews
        if not any(
            approval_names_decision(approval.payload, review.payload.get("id"))
            and (terminal_seq is None or approval.seq < terminal_seq)
            for approval in approvals
        )
    ]
    if unresolved:
        return (
            ControlStatus.FAILED,
            f"REQUIRE_HUMAN_REVIEW unresolved before run close at seq {unresolved}",
            Severity.HIGH,
            _event_evidence(ctx.events, unresolved),
        )
    return ControlStatus.PASSED, f"{len(reviews)} review decisions resolved before close"


def a15_risk_before_tools(ctx: AuditContext) -> CheckResult:
    """A risk-classification ``agent.message`` precedes the first ``tool.started``."""
    tools = ctx.of(EventType.TOOL_STARTED)
    if not tools:
        return ControlStatus.NOT_APPLICABLE, "no tool executions"
    first_tool_seq = min(tool.seq for tool in tools)
    classified_before = any(
        message.payload.get("agent") == RISK_CLASSIFIER_AGENT and message.seq < first_tool_seq
        for message in ctx.of(EventType.AGENT_MESSAGE)
    )
    if classified_before:
        return ControlStatus.PASSED, "risk classified before the first tool.started"
    return (
        ControlStatus.FAILED,
        f"tool.started at seq {first_tool_seq} before any risk classification",
        Severity.HIGH,
        _event_evidence(ctx.events, (first_tool_seq,)),
    )


def a19_sandbox_confinement(
    ctx: AuditContext,
) -> CheckResult:
    """Code executed under weaker-than-full confinement is a finding; severity by how weak it was.

    Enforcement is a reported fact (ADR-0005): ``unusable`` (requested, none applied) is CRITICAL;
    ``partial`` under a ``danger_full_access`` request (unconfined on purpose) is HIGH; any other
    ``partial`` — the MVP's expected local-subprocess state (C-3) — is MEDIUM. When no execution
    recorded an enforcement value nothing ran code and the control is NOT_APPLICABLE.
    """
    recorded = False
    weak: list[str] = []
    weak_events: list[Event] = []
    worst_rank = 0
    severity = Severity.MEDIUM
    for event in ctx.of(EventType.TOOL_COMPLETED):
        enforcement = event.payload.get("sandbox_enforcement")
        if enforcement is None:
            continue
        recorded = True
        if enforcement == SandboxEnforcement.FULL.value:
            continue
        mode = event.payload.get("sandbox_mode")
        if enforcement == SandboxEnforcement.UNUSABLE.value:
            rank, level = 3, Severity.CRITICAL
        elif (
            enforcement == SandboxEnforcement.PARTIAL.value
            and mode == SandboxMode.DANGER_FULL_ACCESS.value
        ):
            rank, level = 2, Severity.HIGH
        else:
            rank, level = 1, Severity.MEDIUM
        weak.append(f"{event.payload.get('tool', 'tool')} at seq {event.seq}: {enforcement}")
        weak_events.append(event)
        if rank > worst_rank:
            worst_rank, severity = rank, level
    if not recorded:
        return ControlStatus.NOT_APPLICABLE, "no execution recorded a sandbox enforcement value"
    if not weak:
        return ControlStatus.PASSED, "every execution reported full confinement"
    return (
        ControlStatus.FAILED,
        "weak confinement: " + "; ".join(weak),
        severity,
        _event_evidence(ctx.events, (event.seq for event in weak_events)),
    )


@dataclass
class _Lifecycle:
    """One ``agent.started`` -> ``agent.completed`` window, for the routing-floor control (A24)."""

    subject: str | None
    completed: bool = False
    has_valid_selection: bool = False
    problems: list[str] = field(default_factory=list)
    problem_events: list[Event] = field(default_factory=list)
    completed_event: Event | None = None


def _pop_lifecycle(stack: list[_Lifecycle], subject: str | None) -> _Lifecycle | None:
    """Remove and return the innermost still-open lifecycle whose subject matches."""
    for index in range(len(stack) - 1, -1, -1):
        if stack[index].subject == subject:
            return stack.pop(index)
    return None


def _fold_lifecycle_event(
    event: Event,
    open_stack: list[_Lifecycle],
    closed: list[_Lifecycle],
    unscoped: _Lifecycle,
    is_valid: Callable[[dict[str, Any]], bool],
    reviewed_decisions: set[str],
    exempt_tool_calls: set[str],
) -> None:
    """Fold one event into the open/closed lifecycle stacks for the routing-floor control (A24).

    A ``model.selected`` recorded while no lifecycle is open is folded into ``unscoped`` instead of
    being skipped. The floor is a property of the routed call, not of the window it happens to sit
    in, and the runtime routes outside a lifecycle in two places -- Core's risk classification node
    and MIRA's own finding verifier -- so skipping those left the floor unaudited exactly where no
    agent boundary was there to catch it. The *ordering* rules stay lifecycle-scoped: "before a
    valid selection" is only meaningful relative to a lifecycle that opened one -- except for a
    ``tool.started`` whose ``decision_id`` already named an earlier ``human.approval_requested``
    (``reviewed_decisions``, built by the caller as it walks the log) and its paired
    ``tool.completed`` (correlated by ``tool_call_id``/``subject_id``, since a completion carries no
    ``decision_id`` of its own -- ``exempt_tool_calls``, populated here). That call was proposed,
    and its proposing model.selected checked, in the earlier lifecycle a review-gated call always
    parks in (`AgentRunner`/`Delegator`, THY-17): a human's later answer lets the *same* deferred
    call replay through `thymira.agents.resume` without asking the model again, so the replay's own
    lifecycle legitimately has no selection of its own to show.
    """
    if event.type is EventType.AGENT_STARTED:
        open_stack.append(_Lifecycle(subject=event.subject_id))
        return
    if event.type is EventType.AGENT_COMPLETED:
        life = _pop_lifecycle(open_stack, event.subject_id)
        if life is not None:
            life.completed = True
            life.completed_event = event
            closed.append(life)
        return
    if not open_stack:
        if event.type is EventType.MODEL_SELECTED:
            if is_valid(event.payload):
                unscoped.has_valid_selection = True
            else:
                unscoped.problems.append(
                    f"model.selected at seq {event.seq} outside any agent lifecycle "
                    f"is below the role floor"
                )
                unscoped.problem_events.append(event)
        return
    life = open_stack[-1]
    if event.type is EventType.MODEL_SELECTED:
        if is_valid(event.payload):
            life.has_valid_selection = True
        else:
            life.problems.append(f"model.selected at seq {event.seq} below the role floor")
            life.problem_events.append(event)
        return
    if (
        event.type is EventType.TOOL_STARTED
        and event.subject_id is not None
        and approval_decision_id(event.payload) in reviewed_decisions
    ):
        exempt_tool_calls.add(event.subject_id)
        return
    if (
        event.type in (EventType.TOOL_STARTED, EventType.TOOL_COMPLETED)
        and not life.has_valid_selection
        and event.subject_id not in exempt_tool_calls
    ):
        life.problems.append(f"{event.type.value} at seq {event.seq} before a valid model.selected")
        life.problem_events.append(event)


def a24_routing_floor(ctx: AuditContext) -> CheckResult:
    """Every recorded model selection respects the role floor, and effects follow a valid one.

    Every ``model.selected`` -- inside an ``agent.started`` -> ``agent.completed`` lifecycle or
    outside every one of them -- must run at or above the role floor
    (``thymira.agents.llm.routing.floor_for``). Inside a lifecycle two ordering rules also apply:
    every ``tool.started`` / ``tool.completed`` must be preceded by a valid selection, and the
    closing ``agent.completed`` must have at least one valid selection before it. ``agent.started``
    opens a lifecycle and needs no preceding selection.

    The control is ``NOT_APPLICABLE`` only when the run recorded neither an agent lifecycle nor a
    single routed selection -- there is then no routing to audit. A run that routed a model without
    opening a lifecycle is audited on that selection alone.
    """
    from thymira.agents.llm.routing import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
        RANK,
        ModelTier,
        Role,
        floor_for,
    )

    def valid_selection(payload: dict[str, Any]) -> bool:
        try:
            role = Role(payload.get("role"))
            applied = ModelTier(payload.get("tier_applied"))
        except ValueError:
            return False
        return RANK[applied] >= RANK[floor_for(role)]

    open_stack: list[_Lifecycle] = []
    closed: list[_Lifecycle] = []
    unscoped = _Lifecycle(subject=None)
    reviewed_decisions: set[str] = set()
    exempt_tool_calls: set[str] = set()
    for event in ctx.events:
        _fold_lifecycle_event(
            event,
            open_stack,
            closed,
            unscoped,
            valid_selection,
            reviewed_decisions,
            exempt_tool_calls,
        )
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED:
            decision_id = approval_decision_id(event.payload)
            if decision_id is not None:
                reviewed_decisions.add(decision_id)
    lifecycles = [*closed, *open_stack]
    routed_unscoped = bool(unscoped.problems or unscoped.has_valid_selection)
    if not lifecycles and not routed_unscoped:
        return ControlStatus.NOT_APPLICABLE, "no agent lifecycle and no model selection"
    violations: list[str] = list(unscoped.problems)
    for life in lifecycles:
        violations.extend(life.problems)
        if life.completed and not life.has_valid_selection:
            violations.append(f"agent.completed {life.subject} with no valid model.selected")
    if violations:
        evidence_events = list(unscoped.problem_events)
        for life in lifecycles:
            evidence_events.extend(life.problem_events)
            if life.completed and not life.has_valid_selection and life.completed_event is not None:
                evidence_events.append(life.completed_event)
        return (
            ControlStatus.FAILED,
            "; ".join(violations),
            Severity.HIGH,
            _event_evidence(ctx.events, (event.seq for event in evidence_events)),
        )
    return ControlStatus.PASSED, "routing floor respected by every recorded model selection"


# ------------------------------------- modelling-artifact controls (A11, A20, A12) and report (A18)


def a11_split_integrity(ctx: AuditContext) -> CheckResult:
    """Train/test split integrity recomputed from the split and dataset evidence."""
    outcome = check_split_integrity(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, payload_keys=("train_indices", "test_indices")
    )
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a20_leakage_indicators(ctx: AuditContext) -> CheckResult:
    """Leakage indicators recomputed from the dataset evidence."""
    outcome = check_leakage_indicators(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, kinds=("DATASET",)
    )
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a12_lineage_coherence(ctx: AuditContext) -> CheckResult:
    """Lineage coherence across split -> candidates -> selection -> model -> evaluation."""
    outcome = check_lineage_coherence(ctx.store)
    names = _artifact_names_from_detail(outcome[1])
    if not names:
        names = _active_artifact_names(ctx, kinds=("MODEL", "METRICS", "REPORT"))
        names += _active_artifact_names(ctx, payload_keys=("train_indices", "test_indices"))
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a18_report_fidelity(ctx: AuditContext) -> CheckResult:
    """Every quantitative claim in the report resolves to a value the run recorded."""
    outcome = check_report_fidelity(ctx.store, ctx.events)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, kinds=("REPORT", "METRICS")
    )
    evidence = _event_evidence(
        ctx.events,
        (event.seq for event in ctx.of(EventType.EXPERIMENT_COMPLETED)),
    ) + _artifact_evidence(ctx.store, names)
    return *outcome, Severity.CRITICAL, evidence


def a13_model_selection(ctx: AuditContext) -> CheckResult:
    """Model selection re-derived from the recorded out-of-fold metrics and declared strategy."""
    outcome = check_model_selection(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, payload_keys=("candidates",)
    )
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a14_test_once(ctx: AuditContext) -> CheckResult:
    """The test partition is read once, at final evaluation, with the OOF-selected threshold."""
    outcome = check_test_once(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, payload_keys=("test_protocol",)
    )
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a21_fold_local_cv(ctx: AuditContext) -> CheckResult:
    """Cross-validation preprocessing is fold-local and the folds are shared by every candidate."""
    outcome = check_fold_local_cv(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, payload_keys=("cross_validation",)
    )
    return _with_evidence(outcome, Severity.CRITICAL, _artifact_evidence(ctx.store, names))


def a22_baseline(ctx: AuditContext) -> CheckResult:
    """A trivial baseline was scored on the identical folds the candidates use."""
    outcome = check_baseline(ctx.store)
    names = _artifact_names_from_detail(outcome[1]) or _active_artifact_names(
        ctx, payload_keys=("cross_validation",)
    )
    return _with_evidence(outcome, Severity.LOW, _artifact_evidence(ctx.store, names))


def a23_reproducibility(ctx: AuditContext) -> CheckResult:
    """Every ``model.trained`` event carries its reproducibility metadata."""
    outcome = check_reproducibility_metadata(ctx.events)
    return _with_evidence(
        outcome,
        Severity.MEDIUM,
        _event_evidence(ctx.events, _seqs_from_detail(outcome[1])),
    )


def a26_requirements_coverage(ctx: AuditContext) -> CheckResult:
    """Every declared framework's mapped requirement has evidence from a deterministic control.

    A26 is registered last so every other control's outcome is already recorded on the context by
    the time it runs; it asserts over those recorded statuses and never re-evaluates a check.
    """
    recorded = {
        control_id: status
        for control_id, status in ctx.control_status.items()
        if control_id != "A26"
    }
    outcome = check_requirements_coverage(ctx, recorded, mapping=ctx.requirements_controls)
    return _with_evidence(outcome, Severity.MEDIUM, ())


def a27_compaction_integrity(ctx: AuditContext) -> CheckResult:
    """Every ``context.compacted`` event is structurally sound (ADR-0006, CMP-01)."""
    from thymira.mira.checks.compaction import (  # noqa: PLC0415  # lazy: sibling check module
        check_compaction_integrity,
    )

    outcome = check_compaction_integrity(ctx.events)
    return _with_evidence(
        outcome,
        Severity.HIGH,
        _event_evidence(ctx.events, _seqs_from_detail(outcome[1])),
    )


def a28_evidence_sufficiency(ctx: AuditContext) -> CheckResult:
    """Require at least one recorded signal that the Run produced or reached something."""
    evidence: list[str] = []
    if ctx.of(EventType.AGENT_COMPLETED):
        evidence.append("agent.completed")
    if ctx.of(EventType.ARTIFACT_CREATED) or (ctx.store is not None and ctx.store.list_active()):
        evidence.append("artifact")
    if ctx.of(EventType.RUN_COMPLETED) or ctx.of(EventType.RUN_FAILED):
        evidence.append("terminal Run event")
    if not evidence:
        return (
            ControlStatus.FAILED,
            "no agent.completed event, artifact, or terminal Run event",
            Severity.HIGH,
            (),
        )
    return ControlStatus.PASSED, "evidence present: " + ", ".join(evidence)


_A29_CREDENTIAL_NAME_FRAGMENTS: tuple[str, ...] = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)
"""MIRA's own copy of the credential-fragment list (never imported from the producer, F11.3):
two readers of one constant are not independent evidence, so a producer-side weakening of the
scrub in ``thymira.tools.sandbox.environment`` is visible here as a finding rather than mirrored.
"""

_A29_REQUIRED_SPEC_KEYS = frozenset(
    {
        "backend",
        "image",
        "workspace_mount",
        "network",
        "memory",
        "cpus",
        "pids_limit",
        "environment_names",
        "excluded_environment_names",
        "unenforced",
    }
)


def _a29_credential_shaped(name: str) -> bool:
    """Return whether ``name`` looks like it carries a secret, using MIRA's own fragment list."""
    folded = name.upper()
    return any(fragment in folded for fragment in _A29_CREDENTIAL_NAME_FRAGMENTS)


_A29_OPTIONAL_STRING_KEYS = ("image", "memory", "cpus")
_A29_REQUIRED_STRING_KEYS = ("backend", "workspace_mount", "network")
_A29_NAME_LIST_KEYS = ("environment_names", "excluded_environment_names", "unenforced")
_A29_OPTIONAL_POSITIVE_INT_KEYS = ("output_limit_bytes", "workspace_quota_bytes")


def _a29_field_shape_issues(spec: dict[str, Any]) -> list[tuple[str, Severity]]:
    """Type-check every key of an already key-complete spec, trusting no field's shape.

    A spec that carries all ten required keys but the wrong type for one of them (every value
    ``None``, say) is exactly as malformed as one missing a key outright -- key *presence* alone
    proved nothing about whether the recorded confinement facts could ever have been real.
    """
    issues: list[tuple[str, Severity]] = [
        (f"{key} is not a string: {spec[key]!r}", Severity.CRITICAL)
        for key in _A29_REQUIRED_STRING_KEYS
        if not isinstance(spec[key], str)
    ]
    issues.extend(
        (f"{key} is not a string or None: {spec[key]!r}", Severity.CRITICAL)
        for key in _A29_OPTIONAL_STRING_KEYS
        if spec[key] is not None and not isinstance(spec[key], str)
    )
    pids_limit = spec["pids_limit"]
    if pids_limit is not None and (isinstance(pids_limit, bool) or not isinstance(pids_limit, int)):
        issues.append((f"pids_limit is not an int or None: {pids_limit!r}", Severity.CRITICAL))
    issues.extend(
        (f"{key} is not a list of names: {spec[key]!r}", Severity.CRITICAL)
        for key in _A29_NAME_LIST_KEYS
        if not (isinstance(spec[key], list) and all(isinstance(item, str) for item in spec[key]))
    )
    issues.extend(
        (f"{key} is not a positive int or None: {spec[key]!r}", Severity.CRITICAL)
        for key in _A29_OPTIONAL_POSITIVE_INT_KEYS
        if key in spec
        and spec[key] is not None
        and (isinstance(spec[key], bool) or not isinstance(spec[key], int) or spec[key] <= 0)
    )
    return issues


def _a29_spec_issues(
    spec: Any, mode: str | None, requested_mode: str | None
) -> list[tuple[str, Severity]]:
    """Recompute whether one recorded ``ResolvedExecutionSpec`` payload is well-formed.

    Never trusts the producer: a spec missing an expected key, a key of the wrong shape (every
    field ``None`` is malformed even though every key is present), a confined mode whose recorded
    network is not ``none``, a ``read_only`` mode whose mount does not end ``:ro``, any forwarded
    environment name that looks credential-shaped, or a recorded ``sandbox_mode`` that disagrees
    with ``requested_sandbox_mode`` is a CRITICAL issue. The network/mount checks each skip a
    backend that honestly names the corresponding control in its own ``unenforced`` list (the
    local backend always does, for every control, whenever it runs at all -- it can enforce
    nothing) -- that field exists precisely so A29 can tell "not enforced, honestly declared"
    apart from "silently missing" (see ``ResolvedExecutionSpec``'s docstring), and this control
    would otherwise never be able to grade a container-only guarantee against a backend that
    never promised it. The mode cross-check closes a narrower bypass: every current producer sets
    ``sandbox_mode``/``requested_sandbox_mode`` equal by construction (a forced
    ``THYMIRA_SANDBOX_MODE`` override rewrites the tool's requested mode before dispatch, so it is
    never observed dispatching one mode while reporting another), so nothing but a bug or
    tampering could produce a mismatch -- self-declaring ``danger_full_access`` for
    ``sandbox_mode`` no longer switches off the network/mount checks below when the mode the tool
    actually requested does not agree.

    Field-shape issues are returned on their own before any of the mode-dependent checks run, so
    a value that failed shape validation is never also read for meaning.
    """
    if not isinstance(spec, dict) or not _A29_REQUIRED_SPEC_KEYS.issubset(spec):
        return [("resolved specification is malformed", Severity.CRITICAL)]
    shape_issues = _a29_field_shape_issues(spec)
    if shape_issues:
        return shape_issues
    issues: list[tuple[str, Severity]] = []
    if mode is not None and requested_mode is not None and mode != requested_mode:
        issues.append(
            (
                f"sandbox_mode {mode!r} disagrees with requested_sandbox_mode {requested_mode!r}",
                Severity.CRITICAL,
            )
        )
    unenforced = spec["unenforced"]
    network = spec["network"]
    if (
        mode != SandboxMode.DANGER_FULL_ACCESS.value
        and "network" not in unenforced
        and network != "none"
    ):
        issues.append((f"network {network!r} is not none under a confined mode", Severity.CRITICAL))
    mount = spec["workspace_mount"]
    if (
        mode == SandboxMode.READ_ONLY.value
        and "filesystem" not in unenforced
        and not mount.endswith(":ro")
    ):
        issues.append(
            (f"workspace_mount {mount!r} is not read-only under read_only mode", Severity.CRITICAL)
        )
    issues.extend(
        (f"credential-shaped environment name forwarded: {name}", Severity.CRITICAL)
        for name in spec["environment_names"]
        if _a29_credential_shaped(name)
    )
    return issues


_A29_SEVERITY_RANK = {Severity.CRITICAL: 2, Severity.MEDIUM: 1}


def a29_resolved_execution_spec(ctx: AuditContext) -> CheckResult:
    """The resolved execution specification is present and self-consistent (F6.2/F6.6).

    Recomputed from the replayed ``tool.completed`` events alone: a ``PARTIAL``/``FULL``
    execution must carry a ``sandbox_spec``. Whenever a spec *is* recorded -- including an
    ``UNUSABLE`` refusal, which still attaches an honest all-``unenforced`` spec -- every one of
    its ten fields is type-checked, a confined mode's recorded ``network`` and ``workspace_mount``
    must match what the mode promises, the recorded ``sandbox_mode`` must agree with
    ``requested_sandbox_mode``, and no recorded ``environment_names`` entry may be
    credential-shaped; a spec is graded on the same rules regardless of what its execution's
    enforcement value was, so there is no enforcement value under which a malformed or
    inconsistent spec passes unexamined. A ``sandbox_cleanup_confirmed`` of ``False`` is a
    separate MEDIUM finding folded into the same control -- it never changes the exit outcome A19
    already grades. An execution with no recorded enforcement (a pre-dispatch refusal such as
    ``read_only_staging_refusal`` or the confined-Git preflight, which never reach a backend) is
    not penalised for carrying no spec.
    """
    recorded = False
    worst_rank = 0
    severity = Severity.MEDIUM
    problems: list[str] = []
    problem_events: list[Event] = []
    for event in ctx.of(EventType.TOOL_COMPLETED):
        enforcement = event.payload.get("sandbox_enforcement")
        if enforcement is None:
            continue
        recorded = True
        spec = event.payload.get("sandbox_spec")
        mode = event.payload.get("sandbox_mode")
        requested_mode = event.payload.get("requested_sandbox_mode")
        event_problems: list[tuple[str, Severity]] = []
        if spec is not None:
            event_problems.extend(_a29_spec_issues(spec, mode, requested_mode))
        elif enforcement in (SandboxEnforcement.PARTIAL.value, SandboxEnforcement.FULL.value):
            event_problems.append(("no resolved specification recorded", Severity.CRITICAL))
        if event.payload.get("sandbox_cleanup_confirmed") is False:
            event_problems.append(("container cleanup was not confirmed", Severity.MEDIUM))
        for detail, level in event_problems:
            problems.append(f"{event.payload.get('tool', 'tool')} at seq {event.seq}: {detail}")
            problem_events.append(event)
            rank = _A29_SEVERITY_RANK.get(level, 1)
            if rank > worst_rank:
                worst_rank, severity = rank, level
    if not recorded:
        return ControlStatus.NOT_APPLICABLE, "no execution recorded a sandbox enforcement value"
    if not problems:
        return ControlStatus.PASSED, "every execution recorded a consistent specification"
    return (
        ControlStatus.FAILED,
        "; ".join(problems),
        severity,
        _event_evidence(ctx.events, (event.seq for event in problem_events)),
    )


def a30_termination_evidence(ctx: AuditContext) -> CheckResult:
    """How every sandboxed execution ended, recomputed from the chain (F6.4/F6.8).

    Recomputation lives in the sibling ``termination_evidence`` module, which imports nothing
    from the producer it audits.
    """
    return check_termination_evidence(ctx)


def a32_workspace_quota(ctx: AuditContext) -> CheckResult:
    """Recompute the bounded tmpfs workspace quota from replayed event evidence."""
    status, detail, evidence = check_workspace_quota(ctx.events)
    return status, detail, Severity.CRITICAL, evidence


def a33_discovery_evidence(ctx: AuditContext) -> CheckResult:
    """Discovery evidence substantiates every rendered glob/grep/list_files result (F3.2/F3.3).

    Delegates the actual recomputation to
    :func:`thymira.mira.checks.discovery_evidence.check_discovery_evidence`, which imports
    nothing from ``thymira.tools``: it re-declares the discovery artifact's schema itself and
    folds three independently written facts -- the artifact's own bytes, the sha256 the store
    recorded on ``artifact.created``, and the ``result_sha256``/``artifact_ids`` the manager
    copied onto ``tool.completed`` -- checking each rendered result is a true, fact-consistent
    prefix of the complete persisted list rather than trusting the tool's own claim. The same
    fold also recomputes an ``FS_READ_REQUIRED``/``FS_STALE_VERSION`` freshness refusal from the
    path its own ``tool.started`` request recorded (F3.3): a refusal sentence that does not
    recompute is not evidence, only a claim. ``NOT_EVALUATED`` when no artifact store is
    available at all -- never a silent pass.
    """
    outcome = check_discovery_evidence(ctx.events, ctx.store)
    return _with_evidence(
        outcome, Severity.HIGH, _event_evidence(ctx.events, _seqs_from_detail(outcome[1]))
    )


CheckResult = (
    tuple[ControlStatus, str]
    | tuple[ControlStatus, str, Severity]
    | tuple[ControlStatus, str, Severity, tuple[Evidence, ...]]
)
Check = Callable[[AuditContext], CheckResult]

_RESULT_BASE_LENGTH = 2
_RESULT_SEVERITY_LENGTH = 3


def a31_subagent_settlement(ctx: AuditContext) -> CheckResult:
    """Every delegation settled exactly once and the parent acted on the canonical settlement.

    Recomputed by :mod:`thymira.mira.checks.subagent_settlement` from the replayed chain alone;
    it imports neither the producer that settles nor the one that runs the child. Not excluded
    in flight -- only its exactly-one-settlement conjunct stands down there -- and not relaxed on
    a failed Run: a child that dies must still settle, and a hard process kill legitimately
    reports FAILED.
    """
    status, detail, seqs = check_subagent_settlement(ctx.events, ctx.audit_mode)
    if status is not ControlStatus.FAILED:
        return status, detail
    return status, detail, Severity.HIGH, _event_evidence(ctx.events, seqs)


def _unpack(
    outcome: CheckResult, default: Severity
) -> tuple[ControlStatus, str, Severity, tuple[Evidence, ...] | None]:
    """Split a result into status, detail, severity and optional explicitly supplied evidence."""
    status, detail = outcome[:2]
    if len(outcome) == _RESULT_BASE_LENGTH:
        return status, detail, default, None
    severity = outcome[2]
    if len(outcome) == _RESULT_SEVERITY_LENGTH:
        return status, detail, severity, None
    return status, detail, severity, outcome[3]


def _legacy_evidence(events: Sequence[Event]) -> tuple[Evidence, ...]:
    """Keep the old broad evidence only for checks that omit the new fourth result element."""
    return _event_evidence(events, (event.seq for event in events[:20]))


@dataclass(frozen=True)
class Control:
    """A registered control: id, title, severity when it fails, and the check function."""

    control_id: str
    title: str
    severity: Severity
    check: Check


CONTROLS: tuple[Control, ...] = (
    Control("A1", "Event chain integrity", Severity.CRITICAL, a1_chain_integrity),
    Control("A2", "Run closed", Severity.HIGH, a2_run_closed),
    Control(
        "A3", "Authorization before tool execution", Severity.CRITICAL, a3_authorization_before_tool
    ),
    Control("A4", "No execution after a block", Severity.CRITICAL, a4_nothing_after_block),
    Control("A5", "Artifact integrity", Severity.HIGH, a5_artifact_integrity),
    Control("A6", "Denials explained by decisions", Severity.MEDIUM, a6_denials_recorded),
    Control("A7", "Approval requests answered", Severity.HIGH, a7_approvals_resolved),
    Control("A9", "Lifecycle pairing", Severity.MEDIUM, a9_lifecycle_pairing),
    Control("A10", "Declared artifacts present", Severity.HIGH, a10_declared_artifacts),
    Control("A16", "Single policy snapshot", Severity.HIGH, a16_policy_snapshot),
    Control("A17", "Provenance recorded", Severity.LOW, a17_provenance),
    Control("A8", "Reviews resolved before run close", Severity.HIGH, a8_reviews_resolved),
    Control("A15", "Risk classified before tool execution", Severity.HIGH, a15_risk_before_tools),
    Control("A19", "Confinement enforced", Severity.MEDIUM, a19_sandbox_confinement),
    Control("A24", "Model routing respects the role floor", Severity.HIGH, a24_routing_floor),
    Control("A11", "Split integrity", Severity.CRITICAL, a11_split_integrity),
    Control("A20", "Leakage indicators", Severity.CRITICAL, a20_leakage_indicators),
    Control("A12", "Lineage coherence", Severity.CRITICAL, a12_lineage_coherence),
    Control("A18", "Report facts match source artifacts", Severity.CRITICAL, a18_report_fidelity),
    Control(
        "A13", "Model selection re-derived independently", Severity.CRITICAL, a13_model_selection
    ),
    Control(
        "A14", "Test partition read once at final evaluation", Severity.CRITICAL, a14_test_once
    ),
    Control("A21", "Fold-local cross-validation", Severity.CRITICAL, a21_fold_local_cv),
    Control("A22", "Trivial baseline scored on identical folds", Severity.LOW, a22_baseline),
    Control("A23", "Reproducibility metadata recorded", Severity.MEDIUM, a23_reproducibility),
    Control("A26", "Requirements coverage", Severity.MEDIUM, a26_requirements_coverage),
    Control("A27", "Compaction integrity", Severity.HIGH, a27_compaction_integrity),
    Control("A28", "Evidence sufficiency", Severity.HIGH, a28_evidence_sufficiency),
    Control("A29", "Resolved execution specification", Severity.HIGH, a29_resolved_execution_spec),
    Control("A30", "Termination and cleanup evidence", Severity.HIGH, a30_termination_evidence),
    Control("A32", "Workspace quota evidence", Severity.CRITICAL, a32_workspace_quota),
    Control("A31", "Subagent settlement", Severity.HIGH, a31_subagent_settlement),
    Control(
        "A33",
        "Discovery evidence substantiates the rendered result",
        Severity.HIGH,
        a33_discovery_evidence,
    ),
)


INVARIANT_CLAIMS: tuple[InvariantClaim, ...] = (
    InvariantClaim(
        invariant_id="A30",
        package_distribution="thymira-tools",
        package_import="thymira.tools",
        package_owner="P3",
        producer="thymira.tools.manager.ToolManager._close_record",
        checker_distribution="thymira-mira",
        checker_import="thymira.mira.checks.termination_evidence",
        checker="check_termination_evidence",
        checker_owner="P4",
        independent_checker="thymira.mira.checks.controls._a30_independent_relationship",
        registry="thymira.mira.checks.CONTROLS",
        relation=(
            "tool.completed.requested_sandbox_spec",
            "tool.completed.observed_sandbox_probe",
        ),
        failure_code="MIRA_A30_TERMINATION_MISMATCH",
        trigger_events=frozenset({"tool.completed"}),
        control_id="A30",
    ),
    InvariantClaim(
        invariant_id="A31",
        package_distribution="thymira-thy",
        package_import="thymira.thy",
        package_owner="P2",
        producer="thymira.thy.delegation.Delegator._settle",
        checker_distribution="thymira-mira",
        checker_import="thymira.mira.checks.subagent_settlement",
        checker="check_subagent_settlement",
        checker_owner="P4",
        independent_checker="thymira.mira.checks.controls._a31_independent_relationship",
        registry="thymira.mira.checks.CONTROLS",
        relation=(
            "subagent.settled.invocation_identity",
            "agent.completed.stop_reason_and_parent_message",
        ),
        failure_code="MIRA_A31_SETTLEMENT_MISMATCH",
        # Completion legitimately precedes settlement; observe once the settlement or parent
        # rendering closes the relationship so IN_FLIGHT does not flag that normal gap.
        trigger_events=frozenset({"subagent.settled", "agent.message"}),
        control_id="A31",
    ),
    InvariantClaim(
        invariant_id="A32",
        package_distribution="thymira-tools",
        package_import="thymira.tools",
        package_owner="P3",
        producer="thymira.tools.builtins.discovery.persist_discovery",
        checker_distribution="thymira-mira",
        checker_import="thymira.mira.checks.discovery_evidence",
        checker="check_discovery_evidence",
        checker_owner="P4",
        independent_checker="thymira.mira.checks.controls._a32_independent_relationship",
        registry="thymira.mira.checks.CONTROLS",
        relation=(
            "tool.completed.rendered_discovery",
            "artifact.created.persisted_discovery_list_and_witness",
        ),
        failure_code="MIRA_A32_DISCOVERY_MISMATCH",
        trigger_events=frozenset({"tool.completed", "artifact.created"}),
        control_id="A32",
    ),
)


def _control_observer(
    control: Control, facts: InvariantInput, audit_mode: AuditMode
) -> InvariantOutcome:
    """Adapt one MIRA control to the registry's append/replay observation contract.

    An append observation always grades a chain that is still growing, so it is in flight by
    construction. A replay observation grades the snapshot the caller is auditing and must use
    that audit's own mode: reading a mid-run snapshot under end-of-run semantics would report
    the pending work an in-flight audit exists to tolerate.
    """
    context = AuditContext(
        run_id=facts.run_id,
        events=facts.events,
        store=cast("ArtifactStore | None", facts.store),
        audit_mode=(AuditMode.IN_FLIGHT if facts.phase is InvariantPhase.APPEND else audit_mode),
    )
    outcome = control.check(context)
    return InvariantOutcome(
        outcome[0] in {ControlStatus.PASSED, ControlStatus.NOT_APPLICABLE},
        str(outcome[1]),
    )


_A30_MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_A30_MEMORY_RE = re.compile(r"^([0-9]+)([bkmg]?)$")
_A30_CPUS_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_A30_MAX_NUMERIC_TEXT = 128
_A30_NANOCPUS_PER_CPU = Decimal(1_000_000_000)


def _a30_memory_bytes(value: Any) -> int | None:
    """Parse the recorded memory ceiling with MIRA's independent implementation."""
    if not isinstance(value, str):
        return None
    match = _A30_MEMORY_RE.fullmatch(value.strip().lower())
    if match is None:
        return None
    try:
        return int(match.group(1)) * _A30_MEMORY_UNITS[match.group(2) or "b"]
    except (OverflowError, ValueError):
        return None


def _a30_network_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested and observed network mode."""
    requested_network = spec.get("network")
    observed_network = probe.get("network")
    if not isinstance(requested_network, str) or not requested_network:
        return [f"seq {sequence}: requested network is unreadable"]
    if not isinstance(observed_network, str) or not observed_network:
        return [f"seq {sequence}: observed network is unreadable"]
    if requested_network != observed_network:
        return [f"seq {sequence}: requested and observed networks disagree"]
    return []


def _a30_memory_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested and observed memory ceiling."""
    requested_memory = spec.get("memory")
    if requested_memory is None:
        return []
    expected_memory = _a30_memory_bytes(requested_memory)
    observed_memory = probe.get("memory_bytes")
    if (
        expected_memory is None
        or not isinstance(observed_memory, int)
        or isinstance(observed_memory, bool)
    ):
        return [f"seq {sequence}: requested and observed memory is unreadable"]
    if expected_memory != observed_memory:
        return [f"seq {sequence}: requested and observed memory ceilings disagree"]
    return []


def _a30_cpu_nanos(requested: str) -> int | None:
    """Scale a validated decimal CPU ceiling to the daemon's integer nanocpu representation.

    MIRA's own conversion: the primary checker pads the fractional digits as text, so this
    observer multiplies the parsed decimal instead and refuses any ceiling that integer nanocpus
    cannot hold exactly.
    """
    with localcontext() as context:
        context.prec = _A30_MAX_NUMERIC_TEXT + len(str(_A30_NANOCPUS_PER_CPU))
        scaled = Decimal(requested) * _A30_NANOCPUS_PER_CPU
        return int(scaled) if scaled == scaled.to_integral_value() else None


def _a30_cpu_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested CPU ceiling with the one the live probe observed."""
    requested_cpus = spec.get("cpus")
    if requested_cpus is None:
        return []
    if (
        not isinstance(requested_cpus, str)
        or len(requested_cpus) > _A30_MAX_NUMERIC_TEXT
        or _A30_CPUS_RE.fullmatch(requested_cpus) is None
    ):
        return [f"seq {sequence}: requested CPU ceiling is unreadable"]
    expected_nanos = _a30_cpu_nanos(requested_cpus)
    if expected_nanos is None or expected_nanos <= 0:
        return [f"seq {sequence}: requested CPU ceiling is not representable in nanocpus"]
    if probe.get("cpus_nano") != expected_nanos:
        return [f"seq {sequence}: requested and observed CPU ceilings disagree"]
    return []


def _a30_pids_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested and observed process limit."""
    requested_pids = spec.get("pids_limit")
    if requested_pids is None:
        return []
    observed_pids = probe.get("pids_limit")
    if (
        not isinstance(requested_pids, int)
        or isinstance(requested_pids, bool)
        or requested_pids <= 0
    ):
        return [f"seq {sequence}: requested process limit is unreadable"]
    if not isinstance(observed_pids, int) or isinstance(observed_pids, bool) or observed_pids < 0:
        return [f"seq {sequence}: observed process limit is unreadable"]
    if requested_pids != observed_pids:
        return [f"seq {sequence}: requested and observed process limits disagree"]
    return []


def _a30_filesystem_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested filesystem enforcement and observed root state."""
    unenforced = spec.get("unenforced")
    if not isinstance(unenforced, list) or any(not isinstance(item, str) for item in unenforced):
        return [f"seq {sequence}: requested filesystem enforcement is unreadable"]
    read_only_rootfs = probe.get("read_only_rootfs")
    if not isinstance(read_only_rootfs, bool):
        return [f"seq {sequence}: observed root filesystem state is unreadable"]
    if "filesystem" not in unenforced and not read_only_rootfs:
        return [f"seq {sequence}: observed root filesystem is writable"]
    return []


def _a30_mount_issues(sequence: int, spec: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    """Compare the requested workspace mount and observed mount profile."""
    mount = spec.get("workspace_mount")
    mounts = probe.get("mounts")
    if not isinstance(mounts, list):
        return [f"seq {sequence}: observed mounts are unreadable"]
    if not isinstance(mount, str) or not mount:
        return [f"seq {sequence}: requested workspace mount is unreadable"]
    if not mount.endswith((":ro", ":rw")):
        return []
    expected_writable = mount.endswith(":rw")
    problems: list[str] = []
    for entry in mounts:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("destination"), str)
            or not isinstance(entry.get("read_write"), bool)
        ):
            problems.append(f"seq {sequence}: observed mount entry is unreadable")
            continue
        if entry["destination"] == "/workspace" and entry["read_write"] != expected_writable:
            problems.append(f"seq {sequence}: observed workspace mount writability disagrees")
    return problems


def _a30_probe_value_issues(sequence: int, probe: dict[str, Any]) -> list[str]:
    """Validate scalar fields in a live backend probe independently of A30's main checker."""
    issues: list[str] = []
    network = probe.get("network")
    if not isinstance(network, str) or not network:
        issues.append(f"seq {sequence}: observed network is unreadable")
    memory = probe.get("memory_bytes")
    if not isinstance(memory, int) or isinstance(memory, bool) or memory < 0:
        issues.append(f"seq {sequence}: observed memory is unreadable")
    cpus_nano = probe.get("cpus_nano")
    if not isinstance(cpus_nano, int) or isinstance(cpus_nano, bool) or cpus_nano <= 0:
        issues.append(f"seq {sequence}: observed CPU ceiling is unreadable")
    pids_limit = probe.get("pids_limit")
    if pids_limit is not None and (
        not isinstance(pids_limit, int) or isinstance(pids_limit, bool) or pids_limit < 0
    ):
        issues.append(f"seq {sequence}: observed process limit is unreadable")
    issues.extend(
        f"seq {sequence}: observed {key} is unreadable"
        for key in ("read_only_rootfs", "oom_killed")
        if not isinstance(probe.get(key), bool)
    )
    return issues


def _a30_probe_mount_issues(sequence: int, probe: dict[str, Any]) -> list[str]:
    """Validate the mount records inside a live backend probe."""
    mounts = probe.get("mounts")
    if not isinstance(mounts, list):
        return [f"seq {sequence}: observed mounts are unreadable"]
    return [
        f"seq {sequence}: observed mount entry is unreadable"
        for entry in mounts
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("destination"), str)
            or not entry.get("destination")
            or not isinstance(entry.get("read_write"), bool)
        )
    ]


def _a30_probe_shape_issues(sequence: int, probe: dict[str, Any]) -> list[str]:
    """Validate every field of a present probe before interpreting backend-specific semantics."""
    required = (
        "network",
        "memory_bytes",
        "cpus_nano",
        "pids_limit",
        "read_only_rootfs",
        "mounts",
        "oom_killed",
    )
    issues = [
        f"seq {sequence}: observed probe is missing {key}" for key in required if key not in probe
    ]
    issues.extend(_a30_probe_value_issues(sequence, probe))
    issues.extend(_a30_probe_mount_issues(sequence, probe))
    return issues


def _a30_resource_disagreements(
    sequence: int, spec: dict[str, Any], probe: dict[str, Any]
) -> list[str]:
    """Compare every resource field represented by the A30 specification and probe."""
    problems: list[str] = []
    for issues in (
        _a30_network_issues(sequence, spec, probe),
        _a30_memory_issues(sequence, spec, probe),
        _a30_cpu_issues(sequence, spec, probe),
        _a30_pids_issues(sequence, spec, probe),
        _a30_filesystem_issues(sequence, spec, probe),
        _a30_mount_issues(sequence, spec, probe),
    ):
        problems.extend(issues)
    return problems


def _a30_terminal_issues(event: Event, termination: dict[str, Any]) -> list[str]:
    """Check terminal fields that claim a completed outer tool call."""
    payload = event.payload
    if payload.get("status") != "COMPLETED" or payload.get("exit_code") != 0:
        return []
    issues: list[str] = []
    if termination.get("outcome") != "completed":
        issues.append(f"seq {event.seq}: outer success has a non-completed termination")
    if termination.get("child_exit_code") != 0:
        issues.append(f"seq {event.seq}: outer success has a non-zero child exit")
    if payload.get("sandbox_cleanup_confirmed") is not True:
        issues.append(f"seq {event.seq}: outer success lacks confirmed cleanup")
    return issues


def _a30_event_issues(event: Event) -> list[str]:
    """Independently validate one completed event's probe shape and resource relationship."""
    payload = event.payload
    termination = payload.get("sandbox_termination")
    if not isinstance(termination, dict):
        return [f"seq {event.seq}: missing sandbox termination record"]

    probe_present = "probe" in termination
    probe = termination.get("probe")
    if not probe_present:
        probe_issues = [f"seq {event.seq}: observed probe is missing"]
    elif probe is None:
        probe_issues = []
    elif not isinstance(probe, dict):
        probe_issues = [f"seq {event.seq}: observed probe is not a record"]
    else:
        probe_issues = _a30_probe_shape_issues(event.seq, probe)

    issues = list(probe_issues)
    spec = payload.get("sandbox_spec")
    if not isinstance(spec, dict):
        issues.append(f"seq {event.seq}: missing sandbox specification")
    elif isinstance(probe, dict) and not probe_issues:
        issues.extend(_a30_resource_disagreements(event.seq, spec, probe))
    elif (
        probe is None
        and spec.get("backend") == "container"
        and termination.get("control_channel_validated") is True
    ):
        issues.append(f"seq {event.seq}: confirmed container has no observed probe")
    issues.extend(_a30_terminal_issues(event, termination))
    return issues


def _a30_independent_relationship(facts: InvariantInput) -> InvariantOutcome:
    """Recompute A30's complete requested-resource to observed-probe relationship.

    This observer has its own parser and field comparisons. It reads no Tool Manager code and
    verifies network, memory, process, root-filesystem and workspace-mount facts before checking
    the terminal outcome. It therefore provides an independent second observation of the whole
    resource projection registered for A30, rather than a second success-flag reader.
    """
    records = [
        event
        for event in facts.events
        if event.type is EventType.TOOL_COMPLETED
        and event.payload.get("sandbox_enforcement") in {"partial", "full"}
    ]
    problems = [issue for event in records for issue in _a30_event_issues(event)]
    if problems:
        return InvariantOutcome(False, "; ".join(problems))
    return InvariantOutcome(True, "outer and nested termination facts agree")


def _a31_independent_relationship(facts: InvariantInput) -> InvariantOutcome:
    """Recompute A31 settlement cardinality from keyed lifecycle events."""
    completions: dict[str, int] = {}
    settlements: dict[str, int] = {}
    stoppable = {"stopped", "abnormal"}
    for event in facts.events:
        key = event.payload.get("delegation_key")
        if not isinstance(key, str) or not key:
            continue
        if event.type is EventType.AGENT_COMPLETED:
            completions[key] = completions.get(key, 0) + 1
        elif event.type.value == "subagent.settled":
            settlements[key] = settlements.get(key, 0) + 1
    problems = [
        f"delegation key {key}: {count} settlements recorded"
        for key, count in settlements.items()
        if count != 1
    ]
    problems.extend(
        f"delegation key {key}: completed lifecycle has no settlement"
        for key in completions.keys() - settlements.keys()
    )
    problems.extend(
        f"delegation key {key}: settlement has no completed lifecycle"
        for key in settlements.keys() - completions.keys()
        if not any(
            event.type.value == "subagent.settled"
            and event.payload.get("delegation_key") == key
            and event.payload.get("stop_reason") in stoppable
            for event in facts.events
        )
    )
    if problems:
        return InvariantOutcome(False, "; ".join(problems))
    return InvariantOutcome(True, "settlement cardinality agrees with completed lifecycles")


def _a32_independent_relationship(facts: InvariantInput) -> InvariantOutcome:
    """Recompute A32's event binding without reading discovery implementation code."""
    discovery_tools = {"glob", "grep", "list_files"}
    completed = [
        event
        for event in facts.events
        if event.type is EventType.TOOL_COMPLETED
        and event.payload.get("tool") in discovery_tools
        and event.payload.get("status") == "COMPLETED"
    ]
    artifact_ids = {
        event.payload.get("artifact_id")
        for event in facts.events
        if event.type is EventType.ARTIFACT_CREATED
        and isinstance(event.payload.get("artifact_id"), str)
    }
    problems: list[str] = []
    for event in completed:
        payload = event.payload
        declared = payload.get("artifact_ids")
        witness = payload.get("discovery_witness_artifact_id")
        if not isinstance(declared, list) or not declared or witness not in declared:
            problems.append(f"seq {event.seq}: discovery completion is not bound to its artifacts")
            continue
        if any(artifact_id not in artifact_ids for artifact_id in declared):
            problems.append(f"seq {event.seq}: discovery completion names an absent artifact")
    if problems:
        return InvariantOutcome(False, "; ".join(problems))
    return InvariantOutcome(True, "discovery completions agree with artifact bindings")


def build_invariant_registry(
    controls: Sequence[Control] = CONTROLS,
    *,
    audit_mode: AuditMode = AuditMode.FINAL,
) -> InvariantRegistry:
    """Build the MIRA registry from relationship controls present in this checkout.

    A claim whose producer control has not landed yet stays a documented scoped absence.  Once
    the corresponding control is registered (for example A31 or A32), the same composition
    automatically binds its append, replay and independent observations.  `audit_mode` is the
    mode the replay observation grades under; append observations are always in flight.
    """
    by_id = {control.control_id: control for control in controls}
    registry = InvariantRegistry()
    independent_observers = {
        "A30": _a30_independent_relationship,
        "A31": _a31_independent_relationship,
        "A32": _a32_independent_relationship,
    }
    for claim in INVARIANT_CLAIMS:
        control = by_id.get(claim.control_id)
        if control is None:
            continue
        independent_observer = independent_observers.get(claim.control_id)
        if independent_observer is None:
            raise ValueError(f"no independent observer registered for {claim.invariant_id}")
        registry.register(
            claim,
            append_checker=lambda facts, control=control: _control_observer(
                control, facts, audit_mode
            ),
            replay_checker=lambda facts, control=control: _control_observer(
                control, facts, audit_mode
            ),
            independent_checker=independent_observer,
        )
    return registry


_IN_FLIGHT_EXCLUDED_CONTROL_IDS = frozenset({"A2", "A7", "A8"})
"""Controls whose evidence is only available after Core's findings Gate and Run closeout."""


def audit_run(
    ctx: AuditContext,
    controls: Sequence[Control] = CONTROLS,
    *,
    invariant_registry: InvariantRegistry | None = None,
) -> AuditReport:
    """Run controls and replay each registered relationship against a fresh evidence snapshot.

    Only the replay observation -- the claim's registered recomputation of this same snapshot --
    may attribute a relationship failure to a control that reported no failure of its own. The
    independent observation is a deliberately different, coarser algorithm; the registry records
    its verdict and its disagreements, but a second observer never rewrites a control's verdict.
    """
    registry = invariant_registry or build_invariant_registry(controls, audit_mode=ctx.audit_mode)
    replay_observations = (
        registry.observe_replay(
            run_id=ctx.run_id,
            events=ctx.events,
            store=ctx.store,
        )
        if ctx.events
        else ()
    )
    registry_failures = {
        observation.control_id: observation
        for observation in replay_observations
        if not observation.passed and observation.phase is InvariantPhase.REPLAY
    }
    selected_controls = (
        tuple(
            control
            for control in controls
            if control.control_id not in _IN_FLIGHT_EXCLUDED_CONTROL_IDS
        )
        if ctx.audit_mode is AuditMode.IN_FLIGHT
        else controls
    )
    results: list[ControlResult] = []
    chain_ok = True
    ctx.control_status.clear()
    for control in selected_controls:
        relationship_failure = registry_failures.get(control.control_id)
        if not chain_ok and control.control_id != "A1":
            status, detail, severity, evidence = (
                ControlStatus.NOT_EVALUATED,
                "chain invalid; not evaluated",
                control.severity,
                (),
            )
        else:
            status, detail, severity, evidence = _unpack(control.check(ctx), control.severity)
            if relationship_failure is not None and status is not ControlStatus.FAILED:
                status = ControlStatus.FAILED
                detail = relationship_failure.detail
            if control.control_id == "A1" and status is ControlStatus.FAILED:
                chain_ok = False
        if status is ControlStatus.FAILED and evidence is None:
            evidence = _legacy_evidence(ctx.events)
        if evidence is None:
            evidence = ()
        ctx.control_status[control.control_id] = status
        claim = registry.claim_for_control(control.control_id)
        results.append(
            ControlResult(
                control_id=control.control_id,
                title=control.title,
                status=status,
                severity=severity,
                detail=detail,
                evidence=evidence,
                failure_code=(
                    claim.failure_code if status is ControlStatus.FAILED and claim else None
                ),
            )
        )
    audited_at = ctx.audited_at or (ctx.events[-1].ts if ctx.events else None)
    findings = tuple(
        _with_audit_timestamp(
            safe_finding(
                AuditFinding(
                    id=_stable_finding_id(ctx.run_id, result.control_id, audited_at),
                    run_id=ctx.run_id,
                    control_id=result.control_id,
                    framework=Framework.INTERNAL,
                    title=result.title,
                    finding=result.detail or "control failed",
                    severity=result.severity,
                    confidence=1.0,
                    evidence=result.evidence,
                    recommendation=(
                        "Inspect the referenced events; the run must not be relied upon until "
                        "resolved."
                    ),
                )
            ),
            audited_at,
        )
        for result in results
        if result.status is ControlStatus.FAILED
    )
    failed = [r for r in results if r.status is ControlStatus.FAILED]
    status: AuditStatus = "passed"
    if any(r.severity in (Severity.HIGH, Severity.CRITICAL) for r in failed):
        status = "failed"
    elif failed:
        status = "passed_with_warnings"
    summary = {s.value: sum(1 for r in results if r.status is s) for s in ControlStatus}
    return AuditReport(
        run_id=ctx.run_id,
        status=status,
        controls=tuple(results),
        findings=findings,
        terminal_hash=ctx.events[-1].hash if ctx.events else None,
        summary=summary,
    )


def _stable_finding_id(run_id: str, control_id: str, audited_at: datetime | None) -> str:
    """Return the deterministic identity of one failed deterministic control."""
    at = audited_at.isoformat() if audited_at is not None else None
    digest = sha256_text(canonical_json({"run": run_id, "control": control_id, "at": at}))[:32]
    return f"finding_{digest}"


def _with_audit_timestamp(finding: AuditFinding, audited_at: datetime | None) -> AuditFinding:
    """Pin a finding timestamp when the deterministic audit has one."""
    if audited_at is None:
        return finding
    return finding.model_copy(update={"created_at": audited_at})
