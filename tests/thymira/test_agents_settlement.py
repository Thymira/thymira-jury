"""The one settlement module: the bound, the delegation key and the single selector (F7.2/F7.5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from thymira.agents.settlement import (
    DIAGNOSTICS_LIMIT,
    bounded_diagnostics,
    delegation_key,
    finalize_parked_invocation,
    has_open_parked_invocation,
    select_canonical,
    settlements_from_events,
)
from thymira.events import InMemoryEventLog
from thymira.schemas import Actor, EventType, StopReason, SubagentResult, new_id

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SELECTION_TOKEN_RE = re.compile(r"select_canonical|SUBAGENT_SETTLED|['\"]subagent\.settled['\"]")

_SELECTION_SOURCES: dict[str, str] = {
    "packages/schemas/src/thymira/schemas/enums.py": "declares the closed event type",
    "runtime/agents/src/thymira/agents/settlement.py": "the one selector and the one writer",
    "runtime/agents/src/thymira/agents/__init__.py": "re-export wiring only",
    "runtime/agents/src/thymira/agents/resume.py": (
        "resume boundary rejects terminal facts after a parked invocation"
    ),
    "runtime/core/src/thymira/core/orchestrator_board.py": (
        "the parent recovery consumer, which delegates canonical selection to the shared helper"
    ),
    "runtime/mira/src/thymira/mira/checks/subagent_settlement.py": (
        "the independent oracle, which re-implements the rule on purpose"
    ),
    "runtime/mira/src/thymira/mira/checks/controls.py": (
        "A9's independent keyed lifecycle ordering check"
    ),
}

_SOURCE_ROOTS = ("packages", "runtime", "apps", "adapters")


def _result(**overrides: object) -> SubagentResult:
    fields: dict[str, object] = {
        "run_id": new_id("run"),
        "task_id": new_id("task"),
        "agent_id": new_id("agent"),
        "agent": "data",
        "parent_agent": "thy",
        "objective": "profile the dataset",
        "delegation_depth": 1,
        "delegation_key": "b" * 64,
        "stop_reason": StopReason.FAILED,
        "diagnostics": "disk full",
        "diagnostics_limit": DIAGNOSTICS_LIMIT,
    }
    fields.update(overrides)
    return SubagentResult.model_validate(fields)


def test_diagnostics_are_redacted_before_they_are_truncated() -> None:
    """Redact-then-truncate: cutting first would halve a secret and write the prefix verbatim."""
    secret = "sk-livesecret1234567890abcdefghijABCDEF"  # noqa: S105  # a fixture, not a key
    text = ("x" * (DIAGNOSTICS_LIMIT - 10)) + " " + secret
    bounded, truncated = bounded_diagnostics(text)

    assert bounded is not None
    assert truncated is True
    for size in range(8, len(secret) + 1):
        assert secret[:size] not in bounded


def test_the_bound_is_recorded_as_a_fact_not_applied_silently() -> None:
    long_text = "y" * (DIAGNOSTICS_LIMIT + 500)
    bounded, truncated = bounded_diagnostics(long_text)
    assert bounded is not None
    assert len(bounded) == DIAGNOSTICS_LIMIT
    assert truncated is True
    settled = _result(diagnostics=bounded, diagnostics_truncated=truncated)
    assert settled.diagnostics_limit == DIAGNOSTICS_LIMIT

    short, short_truncated = bounded_diagnostics("disk full")
    assert short == "disk full"
    assert short_truncated is False
    assert bounded_diagnostics(None) == (None, False)


def test_the_delegation_key_includes_the_invocation_and_parent_identity() -> None:
    """Repeated instructions remain distinct while the same invocation stays stable."""
    run_id = new_id("run")
    task_id = new_id("task")
    agent_id = new_id("agent")

    def _key(
        *,
        run: str = run_id,
        task: str = task_id,
        invocation: str = agent_id,
        parent: str = "thy",
        agent: str = "data",
        objective: str = "profile the dataset",
        depth: int = 1,
    ) -> str:
        return delegation_key(
            run_id=run,
            task_id=task,
            agent_id=invocation,
            parent_agent=parent,
            agent=agent,
            objective=objective,
            delegation_depth=depth,
        )

    key = _key()
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert _key() == key
    assert _key(run=new_id("run")) != key
    assert _key(task=new_id("task")) != key
    assert _key(invocation=new_id("agent")) != key
    assert _key(parent="ml") != key
    assert _key(agent="coding") != key
    assert _key(objective="profile something else") != key
    assert _key(depth=2) != key


def test_the_selector_picks_the_latest_settlement_in_log_order() -> None:
    first = _result(stop_reason=StopReason.FAILED)
    second = _result(stop_reason=StopReason.COMPLETED, result_json="{}", result_schema="a:B")
    assert select_canonical((first, second)) is second
    assert select_canonical((second, first)) is first


def test_the_selector_returns_none_for_an_empty_candidate_set() -> None:
    assert select_canonical(()) is None


def test_the_selector_refuses_a_candidate_set_spanning_two_delegation_keys() -> None:
    """A mixed set is a programming error, never a choice silently resolved."""
    with pytest.raises(ValueError, match="delegation key"):
        select_canonical((_result(), _result(delegation_key="c" * 64)))


def test_settlements_are_read_back_from_the_chain_and_a_malformed_one_is_skipped() -> None:
    log = InMemoryEventLog(new_id("run"))
    settled = _result(run_id=log.run_id)
    log.append(EventType.SUBAGENT_SETTLED, Actor.system(), settled.to_json_dict())
    log.append(EventType.SUBAGENT_SETTLED, Actor.system(), {"stop_reason": "cancelled"})
    log.append(EventType.AGENT_MESSAGE, Actor.system(), {"text": "not a settlement"})

    read_back = settlements_from_events(log.events())

    assert [one.task_id for one in read_back] == [settled.task_id]


def test_finalizing_a_parked_invocation_writes_one_terminal_prefix() -> None:
    """Cancellation repair closes lifecycle, settlement and parent rendering in order."""
    run_id = new_id("run")
    task_id = new_id("task")
    agent_id = new_id("agent")
    key = delegation_key(
        run_id=run_id,
        task_id=task_id,
        agent_id=agent_id,
        parent_agent="thy",
        agent="data",
        objective="profile the dataset",
        delegation_depth=1,
    )
    log = InMemoryEventLog(run_id)
    identity = {
        "agent": "data",
        "task_id": task_id,
        "objective": "profile the dataset",
        "step_key": "a" * 64,
        "parent_agent": "thy",
        "delegation_key": key,
        "delegation_depth": 1,
    }
    log.append(EventType.AGENT_STARTED, Actor.system(), identity, subject_id=agent_id)
    log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            **identity,
            "execution_identity_sha256": "a" * 64,
            "status": "PENDING",
        },
        subject_id=agent_id,
    )

    finalized = finalize_parked_invocation(
        log,
        actor=Actor.system(),
        stop_reason=StopReason.STOPPED,
        diagnostics="caller stopped",
    )

    assert finalized is not None
    events = log.events()
    keyed = [
        event
        for event in events
        if event.payload.get("delegation_key") == key
        and event.type
        in {EventType.AGENT_COMPLETED, EventType.SUBAGENT_SETTLED, EventType.AGENT_MESSAGE}
    ]
    assert [event.type for event in keyed] == [
        EventType.AGENT_COMPLETED,
        EventType.SUBAGENT_SETTLED,
        EventType.AGENT_MESSAGE,
    ]
    assert finalized.result.stop_reason is StopReason.STOPPED
    completion = next(event for event in events if event.type is EventType.AGENT_COMPLETED)
    assert completion.payload["execution_identity_sha256"] == "a" * 64
    assert (
        finalize_parked_invocation(log, actor=Actor.system(), stop_reason=StopReason.STOPPED)
        is None
    )


def test_intermediate_parks_of_one_invocation_still_close_once() -> None:
    """Repeated review rounds remain one open invocation until their latest checkpoint closes."""
    run_id = new_id("run")
    task_id = new_id("task")
    agent_id = new_id("agent")
    key = delegation_key(
        run_id=run_id,
        task_id=task_id,
        agent_id=agent_id,
        parent_agent="thy",
        agent="data",
        objective="profile the dataset",
        delegation_depth=1,
    )
    identity = {
        "agent": "data",
        "task_id": task_id,
        "objective": "profile the dataset",
        "step_key": "a" * 64,
        "parent_agent": "thy",
        "delegation_key": key,
        "delegation_depth": 1,
    }
    log = InMemoryEventLog(run_id)
    log.append(EventType.AGENT_STARTED, Actor.system(), identity, subject_id=agent_id)
    for _ in range(2):
        log.append(
            EventType.AGENT_PARKED,
            Actor.system(),
            {
                **identity,
                "execution_identity_sha256": "a" * 64,
                "status": "PENDING",
            },
            subject_id=agent_id,
        )

    assert has_open_parked_invocation(log.events())
    finalized = finalize_parked_invocation(
        log,
        actor=Actor.system(),
        stop_reason=StopReason.STOPPED,
        diagnostics="caller stopped",
    )

    assert finalized is not None
    keyed = [
        event
        for event in log.events()
        if event.payload.get("delegation_key") == key
        and event.type
        in {EventType.AGENT_COMPLETED, EventType.SUBAGENT_SETTLED, EventType.AGENT_MESSAGE}
    ]
    assert [event.type for event in keyed] == [
        EventType.AGENT_COMPLETED,
        EventType.SUBAGENT_SETTLED,
        EventType.AGENT_MESSAGE,
    ]
    assert not has_open_parked_invocation(log.events())


def test_finalizing_after_a_delegation_already_settled_out_of_band_is_a_no_op() -> None:
    """A delegation closed by its own runner step (not by cancel) does not look unresolved.

    Reproduces a real production shape: a step ending via `_context_budget_failure` (or
    `_resume_history_failure`) writes `agent.completed` carrying the same `delegation_key`,
    `parent_agent` and `delegation_depth` as its `agent.parked` checkpoint (via
    `_delegation_payload`), then the runner's own `subagent.settled`/`agent.message` follow. A
    caller reconciling parked invocations afterwards (`RunService.cancel`/`.fail`) must recognise
    this delegation as already closed rather than raise "a settlement without terminal
    completion" -- the failure this test pins down was a real live Run where two settlement
    helpers omitted `_delegation_payload`, leaving `agent.completed` without a `delegation_key`
    the reconstruction could match back to its park.
    """
    run_id = new_id("run")
    task_id = new_id("task")
    agent_id = new_id("agent")
    key = delegation_key(
        run_id=run_id,
        task_id=task_id,
        agent_id=agent_id,
        parent_agent="thy",
        agent="coding",
        objective="assemble the report",
        delegation_depth=1,
    )
    identity = {
        "agent": "coding",
        "task_id": task_id,
        "objective": "assemble the report",
        "step_key": "a" * 64,
        "parent_agent": "thy",
        "delegation_key": key,
        "delegation_depth": 1,
    }
    log = InMemoryEventLog(run_id)
    log.append(EventType.AGENT_STARTED, Actor.system(), identity, subject_id=agent_id)
    log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {**identity, "execution_identity_sha256": "a" * 64, "status": "PENDING"},
        subject_id=agent_id,
    )
    log.append(
        EventType.AGENT_COMPLETED,
        Actor.system(),
        {
            "agent": "coding",
            "task_id": task_id,
            "status": "FAILED",
            "end_reason": "context_budget",
            "stop_reason": "failed",
            "step_key": identity["step_key"],
            "parent_agent": "thy",
            "delegation_key": key,
            "delegation_depth": 1,
        },
        subject_id=agent_id,
    )
    log.append(
        EventType.SUBAGENT_SETTLED,
        Actor.system(),
        {
            "agent": "coding",
            "agent_id": agent_id,
            "task_id": task_id,
            "delegation_key": key,
            "objective": "assemble the report",
            "parent_agent": "thy",
            "delegation_depth": 1,
            "diagnostics": "context budget remained over limit after durable compaction",
            "diagnostics_limit": DIAGNOSTICS_LIMIT,
            "diagnostics_truncated": False,
            "stop_reason": "failed",
            "result_json": None,
            "result_schema": None,
            "run_id": run_id,
            "settled_at": "2026-09-13T00:00:00Z",
        },
        subject_id=task_id,
    )
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {
            "agent": "coding",
            "task_id": task_id,
            "delegation_key": key,
            "parent_agent": "thy",
            "depth": 1,
            "status": "FAILED",
            "stop_reason": "failed",
            "summary": None,
            "text": "coding failed: agent exceeded max_turns",
        },
        subject_id=task_id,
    )

    assert not has_open_parked_invocation(log.events())
    assert (
        finalize_parked_invocation(log, actor=Actor.system(), stop_reason=StopReason.STOPPED)
        is None
    )


def test_no_production_module_selects_a_settlement_outside_the_shared_selector() -> None:
    """One shared selector serves every consumer (F7.5), mechanically.

    MIRA's control is on the allowed list because its re-implementation *is* the independent
    oracle: a selector it imported from the producer would not be one.
    """
    assert (_REPO_ROOT / "AGENTS.md").is_file(), "repo root did not resolve"
    found: dict[str, str] = {}
    for root in _SOURCE_ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("src/thymira/**/*.py")):
            if _SELECTION_TOKEN_RE.search(path.read_text(encoding="utf-8")):
                found[path.relative_to(_REPO_ROOT).as_posix()] = ""

    assert set(found) == set(_SELECTION_SOURCES)
