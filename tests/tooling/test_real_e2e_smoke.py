"""The real-model smoke accepts only verifiable governance outcomes.

The tests use a real hash-chained JSONL log and small event mappings. They exercise the smoke's
decision boundary without starting an API server or contacting a model: a completed Run needs a
recorded PASS/WARNING (or approved review), a blocked Run may carry a deterministic BLOCK or a
human rejection, and failures, malformed evidence and contradictory facts fail closed.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import cast

import pytest

from tests.tooling.conftest import load_script
from thymira.events import JsonlEventLog
from thymira.schemas import Actor, EventType

_RUN_ID = "run_" + "1" * 32
_DECISION_ID = "decision_" + "2" * 32
_POLICY_HASH = "a" * 64
_CRASH_ERROR = "run run_0fd2e3cbbc5341348e97264cd8df0b36: inline resume failed"


def _event(kind: str, payload: dict[str, object], seq: int) -> dict[str, object]:
    """Build a minimal event mapping for pure verdict tests."""
    return {"type": kind, "payload": payload, "seq": seq}


def _decision(value: str) -> dict[str, object]:
    """Build a valid persisted PolicyDecision payload."""
    return {
        "id": _DECISION_ID,
        "run_id": _RUN_ID,
        "subject_kind": "findings",
        "subject_id": "run",
        "decision": value,
        "rule_id": "smoke_test",
        "reason": "recorded by the deterministic test policy",
        "policy_name": "smoke@1.0",
        "policy_sha256": _POLICY_HASH,
        "finding_ids": [],
        "execution_constraints": {},
    }


def _transition(command: str, outcome: str, seq: int) -> dict[str, object]:
    """Build a terminal transition mapping."""
    return _event(
        "run.transitioned",
        {"command": command, "condition": "terminal", "outcome": outcome},
        seq,
    )


def _completed_events(decision: str = "WARNING") -> list[dict[str, object]]:
    """Build a completed Run with a policy decision and lifecycle closure."""
    events: list[dict[str, object]] = [
        _event("run.started", {}, 0),
        _event("policy.decision", _decision(decision), 1),
        _transition("complete", "completed", 2),
        _event("run.completed", {"final_decision": decision}, 3),
    ]
    return events


def _bound_mapping_events(
    *,
    audit_report: dict[str, object] | None = None,
    binding: dict[str, object] | None = None,
    decision_value: str = "WARNING",
    command: str = "complete",
    outcome: str = "completed",
) -> list[dict[str, object]]:
    """Build a sequenced hostile history for terminal-binding checks."""
    report = audit_report or {
        "run_id": _RUN_ID,
        "status": "passed_with_warnings",
        "controls": [],
        "findings": [],
        "terminal_hash": "a" * 64,
        "policy_sha256": _POLICY_HASH,
    }
    final_binding = binding or {
        "policy_decision_id": _DECISION_ID,
        "policy_sha256": _POLICY_HASH,
        "finding_ids": [],
        "audit_revision": 1,
        "audit_completed_seq": 1,
        "audit_terminal_hash": "a" * 64,
    }
    events: list[dict[str, object]] = [
        {"type": "run.started", "payload": {}, "seq": 0, "hash": "a" * 64},
        {
            "type": "audit.completed",
            "payload": {"audit_revision": 1, "audit_report": report},
            "seq": 1,
            "hash": "c" * 64,
        },
        {"type": "policy.decision", "payload": _decision(decision_value), "seq": 2},
        {
            "type": "run.transitioned",
            "payload": {
                "command": command,
                "condition": "terminal",
                "outcome": outcome,
                **final_binding,
            },
            "seq": 3,
        },
    ]
    if command == "complete":
        events.append({"type": "run.completed", "payload": {}, "seq": 4})
    return events


def test_a_crashed_run_is_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = [
        _event("run.started", {}, 0),
        _transition("fail", "failed", 1),
        _event("run.failed", {"error": _CRASH_ERROR}, 2),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "inline resume failed" in reason


def test_a_completed_run_without_a_policy_decision_is_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = [
        _event("run.started", {}, 0),
        _transition("complete", "completed", 1),
        _event("run.completed", {}, 2),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "no recorded policy.decision" in reason


def test_a_completed_run_without_final_audit_binding_is_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    ok, reason = smoke._terminal_outcome(_completed_events())

    assert ok is False
    assert "final audit/governance binding" in reason


def test_a_task_policy_decision_cannot_close_the_run() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    payload = _decision("WARNING")
    payload["subject_kind"] = "task"
    payload["subject_id"] = "task_" + "3" * 32

    events = [
        _event("run.started", {}, 0),
        _event("policy.decision", payload, 1),
        _transition("complete", "completed", 2),
        _event("run.completed", {}, 3),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "final audit/governance binding" in reason


def test_a_recorded_block_is_a_pass_because_the_policy_engine_decided() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    transition = _transition("block", "blocked", 2)
    transition_payload = cast("dict[str, object]", transition["payload"])
    transition_payload.update(
        {
            "policy_decision_id": _DECISION_ID,
            "policy_sha256": _POLICY_HASH,
            "finding_ids": [],
        }
    )
    events = [
        _event("run.started", {}, 0),
        _event(
            "policy.decision",
            {
                **_decision("BLOCK"),
                "subject_kind": "run",
                "subject_id": _RUN_ID,
            },
            1,
        ),
        transition,
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is True
    assert "BLOCK" in reason


def test_a_block_without_an_exact_policy_binding_is_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = [
        _event("run.started", {}, 0),
        _event(
            "policy.decision",
            {
                **_decision("BLOCK"),
                "subject_kind": "run",
                "subject_id": _RUN_ID,
            },
            1,
        ),
        _transition("block", "blocked", 2),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "binding" in reason


def test_a_rejected_human_review_is_a_successful_block() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = _bound_mapping_events(
        decision_value="REQUIRE_HUMAN_REVIEW", command="block", outcome="blocked"
    )
    transition = events[3]
    transition["seq"] = 5
    cast("dict[str, object]", transition["payload"])["outcome"] = "blocked"
    events[3:3] = [
        _event("human.approval_requested", {"decision_id": _DECISION_ID}, 3),
        {
            "type": "human.approval",
            "payload": {"decision_id": _DECISION_ID, "approved": False},
            "actor": {"kind": "human", "id": "reviewer"},
            "seq": 4,
        },
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is True
    assert "rejected human review" in reason


def test_an_automatic_rejection_does_not_count_as_human_governance() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = [
        _event("run.started", {}, 0),
        _event("policy.decision", _decision("REQUIRE_HUMAN_REVIEW"), 1),
        _event("human.approval_requested", {"decision_id": _DECISION_ID}, 2),
        {
            "type": "human.approval",
            "payload": {"decision_id": _DECISION_ID, "approved": False, "automatic": True},
            "actor": {"kind": "system", "id": "system"},
            "seq": 3,
        },
        _transition("block", "blocked", 4),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "final audit/governance binding" in reason


def test_contradictory_terminal_facts_are_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = _completed_events()
    events.append(_event("run.failed", {"error": _CRASH_ERROR}, 4))

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "evidence after the terminal close" in reason or "failure" in reason


def test_malformed_policy_decision_is_not_a_pass() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    payload = _decision("WARNING")
    del payload["policy_sha256"]
    events = [
        _event("run.started", {}, 0),
        _event("policy.decision", payload, 1),
        _transition("complete", "completed", 2),
        _event("run.completed", {}, 3),
    ]

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "malformed" in reason


def test_a_running_run_has_no_verdict_yet() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    verdict = smoke._terminal_outcome(
        [
            _event("run.started", {}, 0),
            _event("run.transitioned", {"command": "resume", "condition": "active"}, 1),
            _event("tool.completed", {"error": None}, 2),
        ]
    )

    assert verdict is None


def test_read_events_requires_an_intact_canonical_chain(tmp_path: Path) -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    path = tmp_path / ".thymira" / "runtime" / "runs" / _RUN_ID / "events.jsonl"
    log = JsonlEventLog(path, _RUN_ID)
    log.append(EventType.RUN_STARTED, Actor.system())
    raw = path.read_text(encoding="utf-8").replace('"seq":0', '"seq":1')
    path.write_text(raw, encoding="utf-8", newline="\n")

    with pytest.raises(smoke.SmokeError, match="verification failed"):
        smoke._read_events(tmp_path, _RUN_ID)


def test_verified_canonical_events_can_produce_a_pass(tmp_path: Path) -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    path = tmp_path / ".thymira" / "runtime" / "runs" / _RUN_ID / "events.jsonl"
    log = JsonlEventLog(path, _RUN_ID)
    started = log.append(EventType.RUN_STARTED, Actor.system())
    audit = log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "status": "passed_with_warnings",
            "audit_revision": 1,
            "audit_report": {
                "run_id": _RUN_ID,
                "status": "passed_with_warnings",
                "controls": [],
                "findings": [],
                "terminal_hash": started.hash,
                "policy_sha256": _POLICY_HASH,
            },
        },
    )
    log.append(EventType.POLICY_DECISION, Actor.system(), _decision("WARNING"))
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {
            "command": "complete",
            "from_version": 0,
            "to_version": 1,
            "stage": "reporting",
            "condition": "terminal",
            "outcome": "completed",
            "state": {
                "stage": "reporting",
                "condition": "terminal",
                "outcome": "completed",
                "version": 1,
            },
            "policy_decision_id": _DECISION_ID,
            "policy_sha256": _POLICY_HASH,
            "finding_ids": [],
            "audit_revision": 1,
            "audit_completed_seq": audit.seq,
            "audit_terminal_hash": started.hash,
        },
    )
    log.append(EventType.RUN_COMPLETED, Actor.system())

    events = smoke._read_events(tmp_path, _RUN_ID)
    ok, reason = smoke._terminal_outcome(events)

    assert ok is True, reason
    assert "WARNING" in reason


def test_verified_completion_without_audit_binding_is_rejected(tmp_path: Path) -> None:
    """A hash-valid Core-shaped completion cannot stand in for final audit evidence."""
    smoke = load_script("scripts/real_e2e_smoke.py")
    path = tmp_path / ".thymira" / "runtime" / "runs" / _RUN_ID / "events.jsonl"
    log = JsonlEventLog(path, _RUN_ID)
    log.append(EventType.RUN_STARTED, Actor.system())
    log.append(EventType.POLICY_DECISION, Actor.system(), _decision("WARNING"))
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {
            "command": "complete",
            "from_version": 0,
            "to_version": 1,
            "stage": "reporting",
            "condition": "terminal",
            "outcome": "completed",
            "state": {
                "stage": "reporting",
                "condition": "terminal",
                "outcome": "completed",
                "version": 1,
            },
        },
    )
    log.append(EventType.RUN_COMPLETED, Actor.system())

    ok, reason = smoke._terminal_outcome(smoke._read_events(tmp_path, _RUN_ID))

    assert ok is False
    assert "audit.completed" in reason


def test_completion_with_audit_report_from_another_snapshot_is_rejected() -> None:
    """A binding cannot point at a hash that is absent from the preceding event history."""
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = _bound_mapping_events(
        binding={
            "policy_decision_id": _DECISION_ID,
            "policy_sha256": _POLICY_HASH,
            "finding_ids": [],
            "audit_revision": 1,
            "audit_completed_seq": 1,
            "audit_terminal_hash": "d" * 64,
        }
    )

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "audit report terminal hash" in reason or "terminal hash" in reason


def test_completion_with_unbound_audit_marker_is_rejected() -> None:
    """An audit marker without its typed report cannot authorize terminal completion."""
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = _bound_mapping_events(audit_report={"run_id": _RUN_ID})

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "malformed audit report" in reason


def test_completion_with_an_invented_audit_revision_is_rejected() -> None:
    """A binding cannot invent a revision that is inconsistent with the event history."""
    smoke = load_script("scripts/real_e2e_smoke.py")
    events = _bound_mapping_events(
        binding={
            "policy_decision_id": _DECISION_ID,
            "policy_sha256": _POLICY_HASH,
            "finding_ids": [],
            "audit_revision": 2,
            "audit_completed_seq": 1,
            "audit_terminal_hash": "a" * 64,
        }
    )
    audit_payload = cast("dict[str, object]", events[1]["payload"])
    audit_payload["audit_revision"] = 2

    ok, reason = smoke._terminal_outcome(events)

    assert ok is False
    assert "revision does not match" in reason


def test_completion_bound_to_an_older_audit_is_rejected_when_a_newer_audit_exists(
    tmp_path: Path,
) -> None:
    """A later audit invalidates a terminal binding to the earlier snapshot."""
    smoke = load_script("scripts/real_e2e_smoke.py")
    path = tmp_path / ".thymira" / "runtime" / "runs" / _RUN_ID / "events.jsonl"
    log = JsonlEventLog(path, _RUN_ID)
    started = log.append(EventType.RUN_STARTED, Actor.system())
    log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "audit_revision": 1,
            "audit_report": {
                "run_id": _RUN_ID,
                "status": "passed",
                "controls": [],
                "findings": [],
                "terminal_hash": started.hash,
                "policy_sha256": _POLICY_HASH,
            },
        },
    )
    decision_event = log.append(EventType.POLICY_DECISION, Actor.system(), _decision("WARNING"))
    log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "audit_revision": 2,
            "audit_report": {
                "run_id": _RUN_ID,
                "status": "passed",
                "controls": [],
                "findings": [],
                "terminal_hash": decision_event.hash,
                "policy_sha256": _POLICY_HASH,
            },
        },
    )
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {
            "command": "complete",
            "from_version": 0,
            "to_version": 1,
            "stage": "reporting",
            "condition": "terminal",
            "outcome": "completed",
            "state": {
                "stage": "reporting",
                "condition": "terminal",
                "outcome": "completed",
                "version": 1,
            },
            "policy_decision_id": _DECISION_ID,
            "policy_sha256": _POLICY_HASH,
            "finding_ids": [],
            "audit_revision": 1,
            "audit_completed_seq": 1,
            "audit_terminal_hash": started.hash,
        },
    )
    log.append(EventType.RUN_COMPLETED, Actor.system())

    ok, reason = smoke._terminal_outcome(smoke._read_events(tmp_path, _RUN_ID))

    assert ok is False
    assert "latest audit.completed" in reason


def test_the_cli_is_pointed_at_the_port_the_smoke_started(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    seen: dict[str, object] = {}

    def _capture(argv: list[str], **kwargs: object) -> None:
        seen["argv"] = argv
        seen["env"] = kwargs["env"]

    monkeypatch.setattr(smoke.subprocess, "run", _capture)
    smoke._run_cli("status", _RUN_ID, port=8123)

    assert seen["argv"] == ["uv", "run", "thymira", "status", _RUN_ID]
    env = cast("dict[str, str]", seen["env"])
    assert env["THYMIRA_API_URL"] == "http://127.0.0.1:8123"


def test_an_occupied_port_is_refused_without_killing_its_owner() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        with pytest.raises(smoke.SmokeError, match="refusing to terminate"):
            smoke._ensure_port_free(listener.getsockname()[1])
    finally:
        listener.close()


def test_start_server_closes_the_parent_log_handle(monkeypatch, tmp_path: Path) -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    monkeypatch.setattr(smoke, "REPO_ROOT", tmp_path)
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 123

    def _popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["stdout"] = kwargs["stdout"]
        return _FakeProcess()

    monkeypatch.setattr(smoke.subprocess, "Popen", _popen)
    process, path = smoke._start_server(tmp_path, 8123)

    assert isinstance(process, _FakeProcess)
    assert path.exists()
    assert getattr(captured["stdout"], "closed", False) is True


def test_diagnostics_redact_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    smoke._print_safe("Bearer abcdefghijklmnop")

    assert "abcdefghijklmnop" not in capsys.readouterr().out


def test_an_unset_model_route_allowlist_refuses_to_start_and_names_the_configured_ids() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    environment = {"THYMIRA_MODEL_FAST": "vendor/small", "THYMIRA_THY_MODEL": "vendor/big"}

    with pytest.raises(smoke.SmokeError, match="THYMIRA_ALLOWED_MODEL_ROUTES") as excinfo:
        smoke._require_model_route_allowlist(environment)

    assert "vendor/big, vendor/small" in str(excinfo.value)


def test_a_blank_model_route_allowlist_counts_as_unset() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    with pytest.raises(smoke.SmokeError):
        smoke._require_model_route_allowlist({"THYMIRA_ALLOWED_MODEL_ROUTES": " , "})


def test_a_set_model_route_allowlist_is_returned_as_exact_routes() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    routes = smoke._require_model_route_allowlist(
        {"THYMIRA_ALLOWED_MODEL_ROUTES": " vendor/big , vendor/small "}
    )

    assert routes == ("vendor/big", "vendor/small")


def test_first_ask_uses_the_canned_answer() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    answer = smoke._select_interview_answer("sensitive_attributes", 0)

    assert answer == smoke._INTERVIEW_ANSWERS["sensitive_attributes"]


def test_the_first_three_asks_of_a_field_never_send_the_same_text_twice() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    answers = [smoke._select_interview_answer("sensitive_attributes", n) for n in range(3)]

    assert answers[1] == smoke._FOLLOW_UP_ANSWERS["sensitive_attributes"]
    assert answers[2] == smoke._FINAL_ANSWER
    assert len(set(answers)) == 3  # canned, closing follow-up, final statement


def test_every_canned_field_has_a_distinct_follow_up() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    for field in smoke._INTERVIEW_ANSWERS:
        first = smoke._select_interview_answer(field, 0)
        second = smoke._select_interview_answer(field, 1)
        assert field in smoke._FOLLOW_UP_ANSWERS, field
        assert first != second, field


def test_every_interview_field_has_a_canned_answer() -> None:
    from thymira.core.risk_interview import INTERVIEW_FIELDS

    smoke = load_script("scripts/real_e2e_smoke.py")

    assert set(INTERVIEW_FIELDS) <= set(smoke._INTERVIEW_ANSWERS)
    assert set(INTERVIEW_FIELDS) <= set(smoke._FOLLOW_UP_ANSWERS)


def test_dataset_grounded_answers_name_every_column() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")
    repo = Path(__file__).resolve().parents[2]
    header = (
        (repo / "data" / "german_credit.csv")  # the demo copies it as applications.csv
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    columns = header.split(",")

    assert len(columns) == 21  # the canned answers claim exactly this width
    for column in columns:
        assert column in smoke._INTERVIEW_ANSWERS["data_categories"], column
    for column in ("personal_status_sex", "age", "foreign_worker"):
        assert column in smoke._INTERVIEW_ANSWERS["sensitive_attributes"], column


def test_an_unknown_field_falls_back_on_first_ask() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    answer = smoke._select_interview_answer("some_unmapped_field", 0)

    assert answer == smoke._FALLBACK_ANSWER


def test_an_unknown_field_falls_back_on_repeat_without_repeating_text() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    first = smoke._select_interview_answer("some_unmapped_field", 0)
    second = smoke._select_interview_answer("some_unmapped_field", 1)

    assert second == smoke._FOLLOW_UP_FALLBACK_ANSWER
    assert second != first


def test_from_the_third_ask_on_the_final_statement_is_deliberately_stable() -> None:
    smoke = load_script("scripts/real_e2e_smoke.py")

    third = smoke._select_interview_answer("jurisdiction", 2)
    fourth = smoke._select_interview_answer("jurisdiction", 3)

    assert third == smoke._FINAL_ANSWER
    assert fourth == third
    assert third not in {
        smoke._INTERVIEW_ANSWERS["jurisdiction"],
        smoke._FOLLOW_UP_ANSWERS["jurisdiction"],
    }
