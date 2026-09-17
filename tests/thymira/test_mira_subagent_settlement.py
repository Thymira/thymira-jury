"""A31 independently verifies that every delegation settled exactly once (F7.1/F7.2/F7.5).

`Delegator` settles its own children and `AgentRunner` records its own lifecycle; a control that
asked either of them whether it had done so would be no control at all. Every node here builds a
*real* delegation log with the real producers, replays it through a separately constructed
`InMemoryEventLog`, and asks `audit_run` — which imports neither `thymira.agents` nor
`thymira.core` — what it concludes from the chain alone.

The facts A31 relates come from three writers: `AgentRunner.run` writes `agent.started` /
`agent.completed` (with the delegation identity and the stop reason), `Delegator._settle` writes
`subagent.settled`, and the parent writes `agent.message`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import AgentContext, Delegator, load_agent_specs
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.settlement import delegation_key
from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import AuditContext, AuditMode, ControlStatus, audit_run
from thymira.mira.checks import subagent_settlement as settlement_check
from thymira.schemas import Actor, EventType, ModelRoutePolicy, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from thymira.agents import AgentCatalog
    from thymira.mira.checks import ControlResult
    from thymira.schemas import Event

_OBJECTIVE = "profile the dataset"

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _catalog(tmp_path: Path) -> AgentCatalog:
    (tmp_path / "data.yaml").write_text(
        """\
name: data
role: agent
task_kinds: [analyze]
max_turns: 2
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _delegations(tmp_path: Path, *, rows: tuple[int, ...] = (42,)) -> InMemoryEventLog:
    """Run `len(rows)` real delegations of one objective, each with its own invocation key."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(
        [DataProfileOutput(row_count=row, columns=("age",)) for row in rows]
    )
    ctx = AgentContext(
        catalog=catalog, event_log=log, provider=provider, route_policy=TEST_ROUTE_POLICY
    )
    delegator = Delegator(ctx)
    for _ in rows:
        delegator.delegate("thy", spec, _OBJECTIVE, depth=0)
    return log


def _rechain(records: Sequence[tuple[Event, dict[str, Any]]], run_id: str) -> list[Event]:
    """Rebuild a valid hash chain from (event, payload) pairs, so a forgery is still verifiable."""
    rebuilt = InMemoryEventLog(run_id)
    for event, payload in records:
        rebuilt.append(
            event.type,
            event.actor,
            payload,
            subject_id=event.subject_id,
            producer=event.producer,
            producer_version=event.producer_version,
            surface=event.surface,
        )
    return rebuilt.events()


def _as_records(events: Sequence[Event]) -> list[tuple[Event, dict[str, Any]]]:
    return [(event, dict(event.payload)) for event in events]


def _a31(events: Sequence[Event], *, mode: AuditMode = AuditMode.FINAL) -> ControlResult:
    assert verify_events(events).valid, "the forged chain must still verify"
    report = audit_run(AuditContext(run_id=events[0].run_id, events=events, audit_mode=mode))
    return next(control for control in report.controls if control.control_id == "A31")


def _a9(events: Sequence[Event], *, mode: AuditMode = AuditMode.FINAL) -> ControlResult:
    """Return A9 for a hash-valid event snapshot."""
    report = audit_run(AuditContext(run_id=events[0].run_id, events=events, audit_mode=mode))
    return next(control for control in report.controls if control.control_id == "A9")


def _settlement_index(events: Sequence[Event]) -> list[int]:
    return [i for i, event in enumerate(events) if event.type is EventType.SUBAGENT_SETTLED]


def _message_index(events: Sequence[Event]) -> list[int]:
    return [
        i
        for i, event in enumerate(events)
        if event.type is EventType.AGENT_MESSAGE and "delegation_key" in event.payload
    ]


def test_a31_passes_a_run_whose_every_delegation_settled_exactly_once(tmp_path: Path) -> None:
    log = _delegations(tmp_path, rows=(42, 43))
    result = _a31(log.events())
    assert result.status is ControlStatus.PASSED, result.detail
    assert "2 delegation" in result.detail


def test_a31_is_not_applicable_to_a_log_with_no_delegation(tmp_path: Path) -> None:
    """Every log written before settlements existed keeps its verdict."""
    del tmp_path
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {"prompt": "x"})
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})
    assert _a31(log.events()).status is ControlStatus.NOT_APPLICABLE


def test_a31_refuses_a_delegation_that_started_and_never_settled(tmp_path: Path) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    del records[_settlement_index(events)[0]]
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "no settlement" in result.detail


def test_a31_does_not_report_an_unsettled_delegation_while_the_audit_is_in_flight(
    tmp_path: Path,
) -> None:
    """A started child with no terminal evidence is genuinely pending in an in-flight audit."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    remove = {
        _settlement_index(events)[0],
        _message_index(events)[0],
        next(i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED),
    }
    records = [record for i, record in enumerate(records) if i not in remove]
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt, mode=AuditMode.IN_FLIGHT).status is ControlStatus.PASSED
    assert _a9(rebuilt, mode=AuditMode.IN_FLIGHT).status is ControlStatus.PASSED
    assert _a31(rebuilt).status is ControlStatus.FAILED


def test_a31_refuses_terminal_lifecycle_without_settlement_in_flight(tmp_path: Path) -> None:
    """In-flight mode accepts pending work, but a completed child still needs its settlement."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    records = [record for i, record in enumerate(records) if i != _settlement_index(events)[0]]
    rebuilt = _rechain(records, events[0].run_id)
    result = _a31(rebuilt, mode=AuditMode.IN_FLIGHT)
    assert result.status is ControlStatus.FAILED
    assert "terminal lifecycle" in result.detail or "no settlement" in result.detail


def test_a31_refuses_two_settlements_for_one_started_delegation(tmp_path: Path) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records.insert(at + 1, records[at])
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "2 settlements" in result.detail


def test_a31_refuses_a_settlement_whose_stop_reason_is_outside_the_six(tmp_path: Path) -> None:
    """The payload is raw JSON on the chain, so an unknown value really can appear there."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records[at][1]["stop_reason"] = "cancelled"
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "invalid SubagentResult" in result.detail


def test_a31_refuses_diagnostics_longer_than_the_bound_the_settlement_declares(
    tmp_path: Path,
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records[at][1]["diagnostics"] = "z" * (settlement_check.SETTLEMENT_DIAGNOSTICS_LIMIT + 1)
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "diagnostics" in result.detail


def test_a31_refuses_a_settlement_that_declares_a_bound_of_its_own(tmp_path: Path) -> None:
    """MIRA holds the recorded bound against its own constant, so a raised bound is caught."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records[at][1]["diagnostics_limit"] = settlement_check.SETTLEMENT_DIAGNOSTICS_LIMIT * 100
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "bound" in result.detail


def test_a31_refuses_a_settlement_whose_stop_reason_disagrees_with_the_runners_own(
    tmp_path: Path,
) -> None:
    """Two writers, held against each other: the runner's own record and the settlement."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    completed_at = next(
        i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED
    )
    records[completed_at][1]["stop_reason"] = "failed"
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "the two writers disagree" in result.detail


def test_a31_refuses_a_settlement_that_claims_a_completion_no_step_ever_started(
    tmp_path: Path,
) -> None:
    """A forger can only ever downgrade itself to `stopped`, which grants nothing."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    started_at = next(i for i, event in enumerate(events) if event.type is EventType.AGENT_STARTED)
    completed_at = next(
        i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED
    )
    for index in sorted((started_at, completed_at), reverse=True):
        del records[index]
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "no matching agent.started" in result.detail


def test_a31_refuses_a_settlement_whose_delegation_key_does_not_recompute(
    tmp_path: Path,
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records[at][1]["delegation_key"] = "f" * 64
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "does not recompute" in result.detail


def test_a31_refuses_a_settlement_with_invalid_result_fields(tmp_path: Path) -> None:
    """A failed settlement cannot retain the completion fields of a completed child."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    for event, payload in records:
        if event.type in {
            EventType.AGENT_COMPLETED,
            EventType.SUBAGENT_SETTLED,
            EventType.AGENT_MESSAGE,
        }:
            payload["stop_reason"] = "failed"
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "invalid SubagentResult" in result.detail


def test_a31_requires_the_objective_on_a_started_lifecycle(tmp_path: Path) -> None:
    """A start cannot borrow the completion record's intentional objective omission."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    start_at = next(i for i, event in enumerate(events) if event.type is EventType.AGENT_STARTED)
    records[start_at][1].pop("objective")
    rebuilt = _rechain(records, events[0].run_id)

    assert _a31(rebuilt).status is ControlStatus.FAILED
    # A9 independently checks keyed lifecycle pairing and leaves full identity to A31.
    assert _a9(rebuilt).status is ControlStatus.PASSED


def test_a31_rejects_a_present_malformed_completion_objective(tmp_path: Path) -> None:
    """The completion omission allowance does not accept a present null objective."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    completion_at = next(
        i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED
    )
    records[completion_at][1]["objective"] = None
    rebuilt = _rechain(records, events[0].run_id)

    assert _a31(rebuilt).status is ControlStatus.FAILED
    assert _a9(rebuilt).status is ControlStatus.PASSED


@pytest.mark.parametrize("event_type", [EventType.AGENT_STARTED, EventType.AGENT_COMPLETED])
def test_a31_refuses_lifecycle_evidence_with_an_unrelated_invocation_key(
    tmp_path: Path, event_type: EventType
) -> None:
    """A lifecycle record with another key cannot substantiate a settlement by task id alone."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    for event, payload in records:
        if event.type is event_type:
            payload["delegation_key"] = "f" * 64
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    if event_type is EventType.AGENT_STARTED:
        assert "no settlement" in result.detail
        assert "matching agent.started" in result.detail
    else:
        assert "orphan" in result.detail or "no matching agent.completed" in result.detail


def test_a31_refuses_duplicate_unstarted_stopped_settlements(tmp_path: Path) -> None:
    """An unstarted stopped invocation is still allowed one settlement, never two."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    settlement_at = _settlement_index(events)[0]
    stopped_records: list[tuple[Event, dict[str, Any]]] = []
    for event, payload in records:
        if event.type in {
            EventType.AGENT_STARTED,
            EventType.AGENT_COMPLETED,
            EventType.AGENT_MESSAGE,
        }:
            continue
        if event.type is EventType.SUBAGENT_SETTLED:
            payload["stop_reason"] = "stopped"
            payload["result_json"] = None
            payload["result_schema"] = None
        stopped_records.append((event, payload))
    stopped_records.insert(settlement_at, stopped_records[-1])
    result = _a31(_rechain(stopped_records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "2 settlements recorded" in result.detail


def test_a31_refuses_a_lifecycle_that_follows_its_settlement(tmp_path: Path) -> None:
    """A normal invocation must prove start, completion, then settlement in that order."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    start_at = next(i for i, event in enumerate(events) if event.type is EventType.AGENT_STARTED)
    start = records.pop(start_at)
    settlement_at = next(
        i for i, (event, _payload) in enumerate(records) if event.type is EventType.SUBAGENT_SETTLED
    )
    records.insert(settlement_at + 1, start)
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert _a9(_rechain(records, events[0].run_id)).status is ControlStatus.FAILED
    assert "precedes" in result.detail or "order" in result.detail


def test_a31_refuses_a_completion_that_follows_its_settlement(tmp_path: Path) -> None:
    """A completion after settlement cannot close the already-settled invocation."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    completion_at = next(
        i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED
    )
    completion = records.pop(completion_at)
    settlement_at = next(
        i for i, (event, _payload) in enumerate(records) if event.type is EventType.SUBAGENT_SETTLED
    )
    records.insert(settlement_at + 1, completion)
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED
    assert _a9(rebuilt).status is ControlStatus.FAILED


def test_a31_refuses_a_completion_that_precedes_its_start(tmp_path: Path) -> None:
    """A completion must follow the start even when both precede settlement."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    start_at = next(i for i, event in enumerate(events) if event.type is EventType.AGENT_STARTED)
    start = records.pop(start_at)
    completion_at = next(
        i for i, (event, _payload) in enumerate(records) if event.type is EventType.AGENT_COMPLETED
    )
    records.insert(completion_at + 1, start)
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED
    assert _a9(rebuilt).status is ControlStatus.FAILED


@pytest.mark.parametrize(
    ("stop_reason", "status"), [("stopped", "SKIPPED"), ("abnormal", "FAILED")]
)
def test_a31_allows_a_missing_completion_for_an_unstarted_stop(
    tmp_path: Path, stop_reason: str, status: str
) -> None:
    """Stopped or abnormal children may crash after start without recording completion."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    records = [
        (event, payload)
        for event, payload in records
        if event.type is not EventType.AGENT_COMPLETED
    ]
    for event, payload in records:
        if event.type is EventType.SUBAGENT_SETTLED:
            payload["stop_reason"] = stop_reason
            payload["result_json"] = None
            payload["result_schema"] = None
        elif event.type is EventType.AGENT_MESSAGE:
            payload["stop_reason"] = stop_reason
            payload["status"] = status
            payload["summary"] = None
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.PASSED
    assert _a9(rebuilt).status is ControlStatus.PASSED


@pytest.mark.parametrize("event_type", [EventType.AGENT_STARTED, EventType.AGENT_COMPLETED])
def test_a31_refuses_duplicate_lifecycle_evidence(tmp_path: Path, event_type: EventType) -> None:
    """A repeated lifecycle record cannot masquerade as one execution."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = next(i for i, event in enumerate(events) if event.type is event_type)
    records.insert(at + 1, records[at])
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert _a9(_rechain(records, events[0].run_id)).status is ControlStatus.FAILED
    assert "lifecycle" in result.detail or "more than one" in result.detail


def test_a31_refuses_a_parent_rendering_before_its_settlement(tmp_path: Path) -> None:
    """A later correct rendering cannot repair an earlier model-visible claim."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    message_at = _message_index(events)[0]
    message, payload = records[message_at]
    earlier = dict(payload)
    earlier["summary"] = '{"row_count":999,"columns":["age"]}'
    settlement_at = _settlement_index(events)[0]
    records.insert(settlement_at, (message, earlier))
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "preceding settlement" in result.detail


@pytest.mark.parametrize("event_type", [EventType.AGENT_COMPLETED, EventType.AGENT_MESSAGE])
def test_a31_refuses_an_orphan_delegation_fact(tmp_path: Path, event_type: EventType) -> None:
    """A keyed lifecycle or parent fact for another invocation fails closed."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    source_at = next(i for i, event in enumerate(events) if event.type is event_type)
    source, payload = records[source_at]
    orphan = dict(payload)
    orphan["delegation_key"] = "f" * 64
    records.append((source, orphan))
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "orphan" in result.detail or "preceding settlement" in result.detail


@pytest.mark.parametrize("event_type", [EventType.AGENT_STARTED, EventType.AGENT_COMPLETED])
def test_a31_refuses_a_delegation_fact_with_a_malformed_key(
    tmp_path: Path, event_type: EventType
) -> None:
    """A present but unreadable invocation key cannot be treated as anonymous evidence."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    source_at = next(i for i, event in enumerate(events) if event.type is event_type)
    records[source_at][1]["delegation_key"] = None
    rebuilt = _rechain(records, events[0].run_id)
    result = _a31(rebuilt)
    assert result.status is ControlStatus.FAILED
    assert _a9(rebuilt).status is ControlStatus.FAILED
    assert "malformed" in result.detail or "matching" in result.detail


def test_a31_refuses_a_parent_message_missing_its_delegation_key(tmp_path: Path) -> None:
    """A message carrying parent linkage without a key is malformed delegation evidence."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    message_at = _message_index(events)[0]
    records[message_at][1].pop("delegation_key")
    rebuilt = _rechain(records, events[0].run_id)
    result = _a31(rebuilt)
    assert result.status is ControlStatus.FAILED
    assert "malformed" in result.detail


def test_a31_refuses_a_parent_that_did_not_render_its_settlement(tmp_path: Path) -> None:
    """A parent message must agree with the settlement for that invocation."""
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    message = _message_index(events)[0]
    records[message][1]["summary"] = '{"row_count":999,"columns":["age"]}'
    result = _a31(_rechain(records, events[0].run_id))
    assert result.status is ControlStatus.FAILED
    assert "disagrees with the canonical" in result.detail


def test_a31_is_blind_without_the_exactly_one_settlement_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measure of what the control would miss: with the conjunct gone, both nodes PASS."""
    events = _delegations(tmp_path).events()
    missing = _as_records(events)
    remove = {_settlement_index(events)[0], _message_index(events)[0]}
    missing = [record for i, record in enumerate(missing) if i not in remove]
    duplicated = []
    for event, payload in _as_records(events):
        if event.type is EventType.SUBAGENT_SETTLED:
            payload["stop_reason"] = "stopped"
            payload["result_json"] = None
            payload["result_schema"] = None
            duplicated.append((event, payload))
            duplicated.append((event, payload))

    assert _a31(_rechain(missing, events[0].run_id)).status is ControlStatus.FAILED
    assert _a31(_rechain(duplicated, events[0].run_id)).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.exactly_one_settlement
        ),
    )
    assert _a31(_rechain(missing, events[0].run_id)).status is ControlStatus.PASSED
    assert _a31(_rechain(duplicated, events[0].run_id)).status is ControlStatus.PASSED


def test_a31_is_blind_without_the_canonical_selection_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    message = _message_index(events)[0]
    records[message][1]["summary"] = '{"row_count":999,"columns":["age"]}'

    assert _a31(_rechain(records, events[0].run_id)).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.parent_acted_on_the_canonical_settlement
        ),
    )
    assert _a31(_rechain(records, events[0].run_id)).status is ControlStatus.PASSED


def test_a31_is_blind_without_the_result_validation_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    settlement_at = _settlement_index(events)[0]
    settlement = records[settlement_at][1]
    foreign_run = new_id("run")
    settlement["run_id"] = foreign_run
    new_key = delegation_key(
        run_id=foreign_run,
        task_id=settlement["task_id"],
        agent_id=settlement["agent_id"],
        parent_agent=settlement["parent_agent"],
        agent=settlement["agent"],
        objective=settlement["objective"],
        delegation_depth=settlement["delegation_depth"],
    )
    for _, payload in records:
        if "delegation_key" in payload:
            payload["delegation_key"] = new_key
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.valid_subagent_result
        ),
    )
    assert _a31(rebuilt).status is ControlStatus.PASSED


def test_a31_is_blind_without_the_diagnostics_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    at = _settlement_index(events)[0]
    records[at][1]["diagnostics"] = "z" * (settlement_check.SETTLEMENT_DIAGNOSTICS_LIMIT + 1)
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.diagnostics_within_the_declared_bound
        ),
    )
    assert _a31(rebuilt).status is ControlStatus.PASSED


def test_a31_is_blind_without_the_two_writers_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    completed_at = next(
        i for i, event in enumerate(events) if event.type is EventType.AGENT_COMPLETED
    )
    records[completed_at][1]["stop_reason"] = "failed"
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.the_two_writers_agree
        ),
    )
    assert _a31(rebuilt).status is ControlStatus.PASSED


def test_a31_is_blind_without_the_key_recomputation_conjunct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _delegations(tmp_path).events()
    records = _as_records(events)
    for _, payload in records:
        if "delegation_key" in payload:
            payload["delegation_key"] = "f" * 64
    rebuilt = _rechain(records, events[0].run_id)
    assert _a31(rebuilt).status is ControlStatus.FAILED

    monkeypatch.setattr(
        settlement_check,
        "SETTLEMENT_CONJUNCTS",
        tuple(
            conjunct
            for conjunct in settlement_check.SETTLEMENT_CONJUNCTS
            if conjunct is not settlement_check.the_delegation_key_recomputes
        ),
    )
    assert _a31(rebuilt).status is ControlStatus.PASSED
