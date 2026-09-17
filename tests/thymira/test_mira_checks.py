"""MIRA deterministic controls against synthetic runs on the new event log."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, overload

import pytest

from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from tests.thymira.test_tools import _context as _manager_context
from tests.thymira.test_tools import _FakeTool as _RegisteredFakeTool
from thymira.core import MiraControlPlane, RunController, RunEventLog, RunTransitionKind
from thymira.events import InMemoryEventLog, read_events
from thymira.mira.checks import AuditContext, AuditMode, ControlStatus, audit_run, controls
from thymira.mira.checks.controls import a3_authorization_before_tool, a6_denials_recorded
from thymira.policies import (
    ActionRule,
    BudgetRule,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_default_policy,
    load_policy_stack,
)
from thymira.schemas import (
    HUMAN_REVIEW_REFUSAL,
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    ArtifactKind,
    AuthorizationContext,
    AuthorizationDecision,
    Decision,
    Event,
    EventType,
    ExecutionConstraints,
    PolicyDecision,
    Run,
    RunCondition,
    RunStage,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    ToolCallStatus,
    approval_decision_id,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRunStore
from thymira.tools import (
    MISSING_EVIDENCE_REFUSAL,
    BudgetRefusal,
    ToolManager,
    ToolRegistry,
    constraint_refusals,
    tool_intent_sha256,
)

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.events import EventLog


REVIEWER = Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True)
"""The only actor whose `human.approval` MIRA counts, exactly as `ToolManager` counts it."""


def _human_approves(_request) -> bool:
    """A synchronous human approver: what a console reviewer answering "yes" leaves on the log.

    Not `auto_approve`: the Gate marks its own automation answers `automatic` and records them
    under the automation actor, and neither the Tool Manager nor MIRA counts those as an answer.
    """
    return True


def _happy_run(tmp_path: Path) -> tuple[InMemoryEventLog, LocalArtifactStore, PolicyEngine]:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=_human_approves, human=REVIEWER)
    system = Actor.system()
    agent_id, tool_id = new_id("agent"), new_id("tool")
    log.append(
        EventType.RUN_STARTED, system, {"prompt": "x", "run_environment": {"python": "3.13"}}
    )
    log.append(EventType.AGENT_STARTED, system, {"name": "thy"}, subject_id=agent_id)
    # A real run selects a model and classifies risk before it runs any tool (A24, A15).
    log.append(
        EventType.MODEL_SELECTED,
        system,
        {
            "role": "thy",
            "task": "code",
            "tier_requested": "STANDARD",
            "tier_applied": "STANDARD",
            "model": "test-model",
            "reason": "tier table",
        },
    )
    log.append(
        EventType.AGENT_MESSAGE,
        system,
        {"agent": "risk-classifier", "status": "classified", "text": "risk classified: low"},
    )
    gate.check_action(subject_kind="tool_call", subject_id=tool_id, action_type="run_python")
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    artifact = store.save_json(
        "metrics.json", {"auc": 0.8}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": artifact.name, "sha256": artifact.sha256},
        subject_id=artifact.id,
    )
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=tool_id)
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=agent_id)
    log.append(EventType.RUN_COMPLETED, system, {"status": "COMPLETED"})
    return log, store, engine


def test_happy_run_passes_every_applicable_control(tmp_path: Path) -> None:
    log, store, engine = _happy_run(tmp_path)
    report = audit_run(AuditContext(log.run_id, log.events(), store, engine.policy_sha256))
    by_id = {c.control_id: c.status for c in report.controls}
    assert report.status == "passed"
    assert by_id["A1"] is ControlStatus.PASSED
    assert by_id["A3"] is ControlStatus.PASSED  # approval recorded before tool.started
    assert by_id["A7"] is ControlStatus.PASSED
    assert by_id["A10"] is ControlStatus.PASSED
    assert by_id["A16"] is ControlStatus.PASSED
    assert by_id["A17"] is ControlStatus.PASSED
    assert by_id["A6"] is ControlStatus.NOT_APPLICABLE
    assert report.findings == ()
    assert report.terminal_hash == log.events()[-1].hash
    assert "**passed**" in report.to_markdown()


def test_a16_rejects_a_policy_decision_without_a_snapshot() -> None:
    """A policy decision without a hash cannot prove which policy authorized it."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    log.append(
        EventType.POLICY_DECISION,
        system,
        {"decision": Decision.PASS.value, "policy_sha256": None},
    )
    log.append(
        EventType.POLICY_DECISION,
        system,
        {"decision": Decision.PASS.value},
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A16").status is ControlStatus.FAILED
    assert "policy snapshot missing" in _control(report, "A16").detail


def test_audit_run_recomputes_identical_finding_ids_for_identical_events() -> None:
    """Repeated deterministic audits of one event snapshot produce the same report."""
    log, _system = _open_run()

    first = audit_run(AuditContext(log.run_id, log.events()))
    second = audit_run(AuditContext(log.run_id, log.events()))

    assert first == second


def test_in_flight_audit_excludes_controls_that_require_run_close() -> None:
    """In-flight MIRA does not run controls whose evidence only exists after Core closeout."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {})

    report = audit_run(AuditContext(log.run_id, log.events(), audit_mode=AuditMode.IN_FLIGHT))

    control_ids = {control.control_id for control in report.controls}
    assert {"A2", "A7", "A8"}.isdisjoint(control_ids)


def test_a2_fails_for_an_open_final_audit() -> None:
    """A final audit retains A2's closeout check for a genuinely open Run."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {})

    report = audit_run(AuditContext(log.run_id, log.events(), audit_mode=AuditMode.FINAL))

    a2 = next(control for control in report.controls if control.control_id == "A2")
    assert a2.status is ControlStatus.FAILED
    assert any(finding.control_id == "A2" for finding in report.findings)


def test_a2_passes_for_a_closed_final_audit() -> None:
    """A final audit accepts either of the recorded terminal Run events."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    report = audit_run(AuditContext(log.run_id, log.events(), audit_mode=AuditMode.FINAL))

    a2 = next(control for control in report.controls if control.control_id == "A2")
    assert a2.status is ControlStatus.PASSED


@pytest.mark.parametrize("half_run", [False, True], ids=["empty", "started-agent"])
def test_evidence_sufficiency_rejects_empty_and_partial_runs(half_run: bool) -> None:
    """A report cannot pass when a Run has no completed work, artifact, or terminal evidence."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    if half_run:
        log.append(EventType.RUN_STARTED, Actor.system(), {})
        log.append(EventType.AGENT_STARTED, Actor.system(), {}, subject_id=new_id("agent"))

    report = audit_run(AuditContext(run_id, log.events()))

    sufficiency = next(control for control in report.controls if control.control_id == "A28")
    assert sufficiency.status is ControlStatus.FAILED
    assert report.status == "failed"
    assert any(finding.control_id == "A28" for finding in report.findings)


def test_a_hostile_artifact_name_raises_a_finding_instead_of_killing_the_audit(
    tmp_path: Path,
) -> None:
    log, store, engine = _happy_run(tmp_path)
    # The event log is untrusted input: recomputing from it is the whole reason MIRA exists. A
    # name the store refuses to canonicalise reached `ctx.store.exists()`, whose ValueError
    # propagated out of the control and out of `audit_run`, so one hostile payload suppressed the
    # entire audit rather than raising the finding the control is there to raise.
    log.append(
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {"name": "../escaped.json", "sha256": "0" * 64},
        subject_id=new_id("artifact"),
    )

    report = audit_run(AuditContext(log.run_id, log.events(), store, engine.policy_sha256))

    a10 = next(c for c in report.controls if c.control_id == "A10")
    assert a10.status is ControlStatus.FAILED
    assert "../escaped.json" in a10.detail


def test_tool_without_decision_and_after_block_fail(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    rogue = new_id("tool")
    log.append(EventType.TOOL_STARTED, system, {}, subject_id=rogue)
    log.append(EventType.TOOL_COMPLETED, system, {}, subject_id=rogue)
    engine = PolicyEngine(load_policy_stack())
    blocked = new_id("tool")
    Gate(engine, log).check_action(
        subject_kind="tool_call", subject_id=blocked, action_type="deploy_model"
    )
    log.append(EventType.TOOL_STARTED, system, {}, subject_id=blocked)
    log.append(EventType.TOOL_COMPLETED, system, {}, subject_id=blocked)
    log.append(EventType.RUN_COMPLETED, system, {})
    report = audit_run(AuditContext(run_id, log.events()))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A3"].status is ControlStatus.FAILED
    assert by_id["A4"].status is ControlStatus.FAILED
    assert report.status == "failed"
    assert {f.control_id for f in report.findings} >= {"A3", "A4"}
    assert all(
        f.severity is Severity.CRITICAL for f in report.findings if f.control_id in {"A3", "A4"}
    )


def test_broken_chain_stops_evaluation_and_pending_approval_fails(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)  # leaves the review pending
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(EventType.HUMAN_APPROVAL_REQUESTED, Actor.system(), {"decision_id": "orphan"})
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})
    report = audit_run(AuditContext(run_id, log.events()))
    assert {c.control_id: c.status for c in report.controls}["A7"] is ControlStatus.FAILED

    tampered = [
        e.model_copy(update={"payload": {"prompt": "edited"}}) if e.seq == 0 else e
        for e in log.events()
    ]
    broken = audit_run(AuditContext(run_id, tampered))
    statuses = {c.control_id: c.status for c in broken.controls}
    assert statuses["A1"] is ControlStatus.FAILED
    assert statuses["A7"] is ControlStatus.NOT_EVALUATED
    assert broken.status == "failed"


def test_artifact_tampering_is_detected(tmp_path: Path) -> None:
    log, store, engine = _happy_run(tmp_path)
    artifact = store.get("metrics.json")
    assert artifact is not None
    (tmp_path / "artifacts" / artifact.uri).write_text('{"auc": 0.99}', encoding="utf-8")
    report = audit_run(AuditContext(log.run_id, log.events(), store, engine.policy_sha256))
    statuses = {c.control_id: c.status for c in report.controls}
    assert statuses["A5"] is ControlStatus.FAILED
    assert [e.ref for e in _control(report, "A5").evidence] == ["metrics.json", "manifest.json"]
    assert report.status == "failed"
    # The log itself is persisted-compatible: events survive a JSONL round trip.
    path = tmp_path / "trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(e.to_json_dict(), sort_keys=True) for e in log.events()) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assert len(read_events(path)) == len(log.events())


def test_a10_detects_a_coordinated_artifact_and_manifest_rewrite(tmp_path: Path) -> None:
    """The manifest is ordinary JSON that nothing chains; the honest digest is in the log."""
    log, store, engine = _happy_run(tmp_path)
    artifact = store.get("metrics.json")
    assert artifact is not None
    root = tmp_path / "artifacts"
    forged = '{"auc": 0.99}'
    (root / artifact.uri).write_text(forged, encoding="utf-8")

    # Rewrite the manifest so it agrees with the forged file: store.verify() is now satisfied.
    manifest_path = root / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["metrics.json"]["sha256"] = hashlib.sha256(forged.encode("utf-8")).hexdigest()
    raw["metrics.json"]["size_bytes"] = len(forged.encode("utf-8"))
    manifest_path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")

    reopened = LocalArtifactStore(root, log.run_id)
    assert reopened.verify() == []  # file and manifest agree with each other

    report = audit_run(AuditContext(log.run_id, log.events(), reopened, engine.policy_sha256))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A1"].status is ControlStatus.PASSED  # the chain itself was never touched
    assert by_id["A10"].status is ControlStatus.FAILED  # ...but the chained digest disagrees
    created_seq = next(
        event.seq
        for event in log.events()
        if event.type is EventType.ARTIFACT_CREATED and event.payload["name"] == "metrics.json"
    )
    assert [e.ref for e in by_id["A10"].evidence] == [f"seq:{created_seq}", "metrics.json"]
    assert report.status != "passed"


def test_a10_accepts_a_revision_the_store_archived_when_the_name_was_rewritten(
    tmp_path: Path,
) -> None:
    """A superseded revision is still evidence: the store keeps it, so A10 must resolve it.

    A step that parks twice writes its resume bundle under the same logical name each time. The
    first ``artifact.created`` announces a digest the live file no longer carries -- but the store
    archived that exact revision under ``.history``, so nothing was lost and nothing was tampered
    with.
    """
    log, store, engine = _happy_run(tmp_path)
    system = Actor.system()
    tool_id = new_id("tool")
    first = store.get("metrics.json")
    assert first is not None
    second = store.save_json(
        "metrics.json", {"auc": 0.9}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    assert second.sha256 != first.sha256  # the live file genuinely moved on
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": second.name, "sha256": second.sha256},
        subject_id=second.id,
    )

    reopened = LocalArtifactStore(tmp_path / "artifacts", log.run_id)
    report = audit_run(AuditContext(log.run_id, log.events(), reopened, engine.policy_sha256))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A10"].status is ControlStatus.PASSED
    assert by_id["A10"].detail == (
        "2 artifacts present (1 superseded revisions held in the store's history)"
    )


def test_a10_fails_when_the_live_revision_was_never_announced(tmp_path: Path) -> None:
    """Overwriting an announced artifact through the store's own API, with no new announcement.

    The store archives the announced revision, so every announcement still resolves against a
    held revision -- but the live bytes answer to nothing on the log. That is the cheapest attack
    of all (one ``save_json`` call, no manifest edit, no event forged) and A10 must still fail.
    """
    log, store, engine = _happy_run(tmp_path)
    store.save_json("metrics.json", {"auc": 0.99}, produced_by=new_id("tool"))

    reopened = LocalArtifactStore(tmp_path / "artifacts", log.run_id)
    assert reopened.verify() == []  # file and manifest agree: A5 alone cannot see it
    report = audit_run(AuditContext(log.run_id, log.events(), reopened, engine.policy_sha256))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A10"].status is ControlStatus.FAILED
    assert by_id["A10"].detail == "live revision was never announced on the log: ['metrics.json']"
    created_seq = next(
        event.seq
        for event in log.events()
        if event.type is EventType.ARTIFACT_CREATED and event.payload["name"] == "metrics.json"
    )
    assert [e.ref for e in by_id["A10"].evidence] == [f"seq:{created_seq}", "metrics.json"]
    assert report.status != "passed"


def test_a10_reports_a_hostile_unhashable_digest_as_a_finding_not_a_crash(
    tmp_path: Path,
) -> None:
    """One hostile ``artifact.created`` payload must raise a finding, never kill the audit."""
    log, store, engine = _happy_run(tmp_path)
    system = Actor.system()
    second = store.save_json("metrics.json", {"auc": 0.9}, produced_by=new_id("tool"))
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": second.name, "sha256": second.sha256},
        subject_id=second.id,
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": "metrics.json", "sha256": ["deadbeef"]},
        subject_id=new_id("artifact"),
    )

    reopened = LocalArtifactStore(tmp_path / "artifacts", log.run_id)
    report = audit_run(AuditContext(log.run_id, log.events(), reopened, engine.policy_sha256))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A10"].status is ControlStatus.FAILED
    assert "metrics.json" in by_id["A10"].detail
    assert report.status != "passed"


def test_a10_fails_when_the_archived_revision_backing_an_announcement_is_gone(
    tmp_path: Path,
) -> None:
    """Rewriting an artifact and announcing the new digest must not erase the old announcement."""
    log, store, engine = _happy_run(tmp_path)
    system = Actor.system()
    tool_id = new_id("tool")
    first = store.get("metrics.json")
    assert first is not None
    second = store.save_json(
        "metrics.json", {"auc": 0.9}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": second.name, "sha256": second.sha256},
        subject_id=second.id,
    )

    # Drop the archived revision and its manifest entry: the first announcement now backs nothing.
    root = tmp_path / "artifacts"
    manifest_path = root / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    archived = [key for key in raw if key.startswith(".history/")]
    assert archived, "the store should have archived the superseded revision"
    for key in archived:
        (root / key).unlink()
        del raw[key]
    manifest_path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")

    reopened = LocalArtifactStore(root, log.run_id)
    report = audit_run(AuditContext(log.run_id, log.events(), reopened, engine.policy_sha256))
    by_id = {c.control_id: c for c in report.controls}
    assert by_id["A10"].status is ControlStatus.FAILED
    assert "metrics.json" in by_id["A10"].detail
    assert report.status != "passed"


def test_a3_failure_cites_only_the_tool_execution_named_in_its_detail() -> None:
    """A3 finding evidence follows its unauthorised tool.started sequence."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    for _ in range(46):
        log.append(EventType.AGENT_MESSAGE, system, {"text": "filler"})
    tool_id = new_id("tool")
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    log.append(EventType.TOOL_COMPLETED, system, {}, subject_id=tool_id)
    log.append(EventType.RUN_COMPLETED, system, {})

    result = _control(audit_run(AuditContext(run_id, log.events())), "A3")

    assert result.status is ControlStatus.FAILED
    assert result.detail == "tool.started without an allowing decision at seq [47]"
    assert [e.ref for e in result.evidence] == ["seq:47"]


def test_a17_failure_cites_the_run_started_event_it_inspected() -> None:
    """A17 does not cite the terminal event when provenance is absent at run start."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    log.append(EventType.RUN_COMPLETED, system, {})

    result = _control(audit_run(AuditContext(run_id, log.events())), "A17")

    assert result.status is ControlStatus.FAILED
    assert result.detail == "no run_environment payload or artifact"
    assert [e.ref for e in result.evidence] == ["seq:0"]


# --------------------------------------------------------- shared helpers for the MVP controls


def _control(report, control_id):
    """Return the single ControlResult for `control_id`."""
    return next(c for c in report.controls if c.control_id == control_id)


def _findings(report, control_id):
    """Return only the findings raised by `control_id`."""
    return [f for f in report.findings if f.control_id == control_id]


def _open_run() -> tuple[InMemoryEventLog, Actor]:
    """A started (not yet closed) run with recorded provenance."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    return log, system


def _selection(role: str, tier: str) -> dict[str, str]:
    """A `model.selected` payload shaped like `routing.ModelChoice.event_payload()`."""
    return {
        "role": role,
        "task": "audit_judgement",
        "tier_requested": tier,
        "tier_applied": tier,
        "model": "test-model",
        "reason": "test",
    }


class _CountingEvents(Sequence[Event]):
    """Sequence wrapper counting full event-log iterations."""

    def __init__(self, events: Sequence[Event]) -> None:
        self._events = events
        self.iterations = 0

    def __iter__(self) -> Iterator[Event]:
        self.iterations += 1
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    @overload
    def __getitem__(self, index: int) -> Event: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Event]: ...

    def __getitem__(self, index: int | slice) -> Event | Sequence[Event]:
        return self._events[index]


def test_a3_and_a6_scan_the_event_log_once_per_control() -> None:
    """Authorization and denial controls do not rescan the full log per event."""
    log, system = _open_run()
    for _ in range(40):
        tool_id = new_id("tool")
        log.append(
            EventType.POLICY_DECISION,
            system,
            # `subject_kind` is part of every recorded decision, and A3 reads it: a decision about
            # anything but the tool call itself authorises no execution.
            {"decision": Decision.PASS.value, "subject_kind": "tool_call"},
            subject_id=tool_id,
        )
        log.append(EventType.TOOL_STARTED, system, {}, subject_id=tool_id)

    a3_events = _CountingEvents(log.events())
    assert (
        a3_authorization_before_tool(AuditContext(log.run_id, a3_events))[0] is ControlStatus.PASSED
    )
    assert a3_events.iterations == 1

    denial_log, denial_system = _open_run()
    for _ in range(40):
        tool_id = new_id("tool")
        denial_log.append(
            EventType.POLICY_DECISION,
            denial_system,
            {"decision": Decision.BLOCK.value},
            subject_id=tool_id,
        )
        denial_log.append(EventType.TOOL_DENIED, denial_system, {}, subject_id=tool_id)

    a6_events = _CountingEvents(denial_log.events())
    assert (
        a6_denials_recorded(AuditContext(denial_log.run_id, a6_events))[0] is ControlStatus.PASSED
    )
    assert a6_events.iterations == 1


# ------------------------------------------------------------------ A19: confinement enforced


def _tool_completed(log: InMemoryEventLog, system: Actor, enforcement: str, mode: str) -> None:
    """Record one code-executing `tool.completed` carrying the confinement facts A19 reads."""
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {"tool": "run_python", "sandbox_mode": mode, "sandbox_enforcement": enforcement},
        subject_id=new_id("tool"),
    )


def test_a19_partial_confinement_raises_one_sandbox_finding() -> None:
    log, system = _open_run()
    _tool_completed(
        log, system, SandboxEnforcement.PARTIAL.value, SandboxMode.WORKSPACE_WRITE.value
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A19")
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    assert _control(report, "A19").status is ControlStatus.FAILED


def test_a19_full_confinement_raises_no_sandbox_finding() -> None:
    log, system = _open_run()
    _tool_completed(log, system, SandboxEnforcement.FULL.value, SandboxMode.WORKSPACE_WRITE.value)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _findings(report, "A19") == []
    assert _control(report, "A19").status is ControlStatus.PASSED


def test_a19_unusable_confinement_is_a_critical_sandbox_finding() -> None:
    log, system = _open_run()
    _tool_completed(
        log, system, SandboxEnforcement.UNUSABLE.value, SandboxMode.WORKSPACE_WRITE.value
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A19")
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL


def test_a19_partial_danger_full_access_is_a_high_sandbox_finding() -> None:
    log, system = _open_run()
    _tool_completed(
        log, system, SandboxEnforcement.PARTIAL.value, SandboxMode.DANGER_FULL_ACCESS.value
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A19")
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH


def test_a19_reports_one_sandbox_finding_at_the_worst_severity() -> None:
    log, system = _open_run()
    _tool_completed(
        log, system, SandboxEnforcement.PARTIAL.value, SandboxMode.WORKSPACE_WRITE.value
    )
    _tool_completed(
        log, system, SandboxEnforcement.UNUSABLE.value, SandboxMode.WORKSPACE_WRITE.value
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A19")
    assert len(findings) == 1  # one control -> one finding, no matter how many weak executions
    assert findings[0].severity is Severity.CRITICAL


def test_a19_is_not_applicable_without_a_recorded_sandbox_enforcement() -> None:
    log, system = _open_run()
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {"tool": "read_file", "exit_code": 0},  # a non-executing tool records no enforcement
        subject_id=new_id("tool"),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A19").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A19") == []


# ------------------------------------------------------- A29: resolved execution specification


def _resolved_spec(
    *,
    backend: str = "container",
    network: str = "none",
    workspace_mount: str = "/host:/workspace:rw",
    environment_names: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build the ``sandbox_spec`` event payload a backend records (``ResolvedExecutionSpec``)."""
    return {
        "backend": backend,
        "image": "thymira:dev" if backend == "container" else None,
        "workspace_mount": workspace_mount,
        "network": network,
        "memory": "1g" if backend == "container" else None,
        "cpus": "1.0" if backend == "container" else None,
        "pids_limit": 128 if backend == "container" else None,
        "environment_names": list(environment_names),
        "excluded_environment_names": [],
        "unenforced": ["rlimits", "workspace_quota"],
    }


def _tool_completed_with_evidence(
    log: InMemoryEventLog,
    system: Actor,
    *,
    enforcement: str,
    mode: str,
    spec: dict[str, object] | None,
    cleanup_confirmed: bool | None = None,
    exit_code: int = 0,
    requested_mode: str | None = None,
) -> None:
    """Record one code-executing `tool.completed` carrying the full sandbox evidence set.

    ``requested_mode`` defaults to ``mode``: every current producer (``ToolManager``) sets
    ``sandbox_mode``/``requested_sandbox_mode`` equal by construction, so a test that wants the
    ordinary, consistent case should not have to spell that out.
    """
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {
            "tool": "run_python",
            "sandbox_mode": mode,
            "requested_sandbox_mode": mode if requested_mode is None else requested_mode,
            "sandbox_enforcement": enforcement,
            "sandbox_spec": spec,
            "sandbox_cleanup_confirmed": cleanup_confirmed,
            "exit_code": exit_code,
        },
        subject_id=new_id("tool"),
    )


def test_a29_passes_when_every_execution_records_a_consistent_specification() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=_resolved_spec(),
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A29").status is ControlStatus.PASSED
    assert _findings(report, "A29") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_limit_bytes", 0),
        ("output_limit_bytes", True),
        ("workspace_quota_bytes", -1),
        ("workspace_quota_bytes", "8192"),
    ],
)
def test_a29_rejects_a_malformed_optional_resource_limit(field: str, value: object) -> None:
    log, system = _open_run()
    spec = _resolved_spec()
    spec[field] = value
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=spec,
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    finding = _findings(report, "A29")
    assert finding
    assert finding[0].severity is Severity.CRITICAL


def test_a29_execution_without_a_specification_is_a_critical_finding() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=None,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert _control(report, "A29").status is ControlStatus.FAILED


def test_a29_container_network_inconsistent_with_a_confined_mode_is_critical() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=_resolved_spec(network="bridge"),
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL


def test_a29_read_only_mode_with_a_writable_mount_is_critical() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.READ_ONLY.value,
        spec=_resolved_spec(workspace_mount="/host:/workspace:rw"),
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL


def test_a29_credential_shaped_forwarded_variable_is_critical() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=_resolved_spec(environment_names=("AWS_SECRET_ACCESS_KEY",)),
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL


def test_a29_failed_cleanup_is_a_medium_finding_that_keeps_the_exit_outcome() -> None:
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=_resolved_spec(),
        cleanup_confirmed=False,
        exit_code=0,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    # A29's cleanup finding never rewrites the exit outcome A19/the ToolCall already recorded.
    completed = next(e for e in log.events() if e.type is EventType.TOOL_COMPLETED)
    assert completed.payload["exit_code"] == 0


def test_a29_all_null_spec_under_danger_full_access_is_critical() -> None:
    """Every required key present but every value ``None`` must not pass as well-formed."""
    log, system = _open_run()
    null_spec = dict.fromkeys(
        (
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
        )
    )
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.DANGER_FULL_ACCESS.value,
        spec=null_spec,
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert findings, "an all-null spec must not pass under danger_full_access"
    assert findings[0].severity is Severity.CRITICAL
    assert _control(report, "A29").status is ControlStatus.FAILED


def test_a29_credential_name_as_a_tuple_is_still_critical() -> None:
    """A malformed (non-list) `environment_names` must fail closed, not skip the check."""
    log, system = _open_run()
    spec = _resolved_spec()
    spec["environment_names"] = ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY")  # a tuple, not a list
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=spec,
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert findings, "a non-list environment_names must not silently skip the credential check"
    assert findings[0].severity is Severity.CRITICAL
    assert _control(report, "A29").status is ControlStatus.FAILED


def test_a29_sandbox_mode_disagreeing_with_requested_mode_is_critical() -> None:
    """A self-declared danger_full_access must match what the tool actually requested."""
    log, system = _open_run()
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.PARTIAL.value,
        mode=SandboxMode.DANGER_FULL_ACCESS.value,
        requested_mode=SandboxMode.READ_ONLY.value,
        spec=_resolved_spec(network="bridge"),
        cleanup_confirmed=True,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert findings, "sandbox_mode must not be able to disagree with requested_sandbox_mode"
    assert findings[0].severity is Severity.CRITICAL
    assert _control(report, "A29").status is ControlStatus.FAILED


def test_a29_validates_a_recorded_spec_even_when_the_execution_is_unusable() -> None:
    """A spec is graded on its own shape whenever it is recorded, whatever the enforcement was.

    This is the exact shape the demo acceptance Run records: a ``local_subprocess`` refusal is
    ``UNUSABLE`` but still attaches an honest all-``unenforced`` spec (F6.2), and A29's PASS for
    that Run must mean something -- not merely that ``UNUSABLE`` executions are skipped over.
    """
    log, system = _open_run()
    malformed_spec: dict[str, object] = {
        "backend": "local_subprocess",
        "unenforced": ["workspace_quota"],
    }
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.UNUSABLE.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=malformed_spec,
        exit_code=125,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert findings, "a two-key spec missing eight required keys must not pass under UNUSABLE"
    assert findings[0].severity is Severity.CRITICAL
    assert _control(report, "A29").status is ControlStatus.FAILED


def test_a29_respects_an_honestly_declared_unenforced_network() -> None:
    """A backend that names "network" in `unenforced` is not held to the confined-mode promise.

    This is the exact shape `LocalSubprocessSandbox` records for every refusal: `network: "host"`
    under a confined mode, honestly declared unenforced rather than silently wrong.
    """
    log, system = _open_run()
    local_refusal_spec: dict[str, object] = {
        "backend": "local_subprocess",
        "image": None,
        "workspace_mount": "/workspace",
        "network": "host",
        "memory": None,
        "cpus": None,
        "pids_limit": None,
        "environment_names": [],
        "excluded_environment_names": [],
        "unenforced": ["cpu", "filesystem", "memory", "network", "pids", "rlimits"],
    }
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.UNUSABLE.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=local_refusal_spec,
        exit_code=125,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A29").status is ControlStatus.PASSED
    assert _findings(report, "A29") == []


def test_a29_still_flags_network_not_declared_unenforced() -> None:
    """The `unenforced` escape hatch does not apply to a network value that is not listed there."""
    log, system = _open_run()
    spec: dict[str, object] = {
        "backend": "local_subprocess",
        "image": None,
        "workspace_mount": "/workspace",
        "network": "host",
        "memory": None,
        "cpus": None,
        "pids_limit": None,
        "environment_names": [],
        "excluded_environment_names": [],
        "unenforced": ["cpu", "filesystem", "memory", "pids", "rlimits"],  # "network" missing
    }
    _tool_completed_with_evidence(
        log,
        system,
        enforcement=SandboxEnforcement.UNUSABLE.value,
        mode=SandboxMode.WORKSPACE_WRITE.value,
        spec=spec,
        exit_code=125,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    findings = _findings(report, "A29")
    assert findings, "an un-declared network!='none' under a confined mode must still be CRITICAL"
    assert findings[0].severity is Severity.CRITICAL


def test_a29_is_not_applicable_without_any_recorded_execution() -> None:
    log, system = _open_run()
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {"tool": "read_file", "exit_code": 0},
        subject_id=new_id("tool"),
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A29").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A29") == []


# ---------------------------------------------------------------- A7: approval requests answered


def _control_plane_review(tmp_path, *, approver=auto_approve):
    """Drive one REVIEW_FINDINGS intent through the MIRA control plane on a started Run.

    This is the second writer of approval evidence: `Gate.request_approval` appends
    `Approval.to_json_dict()`, whose decision field is `policy_decision_id`, while
    `Gate.check_action` writes `decision_id`. Both shapes end up on the same append-only chain.
    """
    store = LocalRunStore(tmp_path / "runs")
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Review the MIRA findings.",
    )
    store.create(run, actor=Actor.system())
    engine = PolicyEngine(load_default_policy("base"))
    plane = MiraControlPlane(store, engine, approver=approver)
    plane.controller.advance(run.id, RunTransitionKind.START)
    intent = ActionIntent(
        id=new_id("intent"),
        run_id=run.id,
        requester=Actor(kind=ActorKind.AGENT, id="mira", authenticated=True),
        action_kind=ActionKind.REVIEW_FINDINGS,
        subject_kind="findings",
        subject_id=new_id("finding"),
        purpose="Review the findings before the run closes.",
        idempotency_key=new_id("intent"),
    )
    return store, run, engine, plane.handle(intent)


def test_a7_passes_for_an_approval_recorded_by_the_control_plane_path(tmp_path: Path) -> None:
    """A run a human really approved through MiraControlPlane must not audit as FAILED.

    Regression for finding #46-1: A7 paired requests with approvals on `decision_id` only, so
    every control-plane approval -- which records `policy_decision_id` -- looked unanswered.
    """
    store, run, engine, result = _control_plane_review(tmp_path)
    # The approval really happened and really authorised the transition.
    assert result.context.requires_approval
    assert result.approval is not None
    assert result.approval.approved is True
    assert result.event is not None

    events = store.events(run.id)
    approvals = [e for e in events if e.type is EventType.HUMAN_APPROVAL]
    assert [e.payload["policy_decision_id"] for e in approvals] == [result.decision.id]
    assert "decision_id" not in approvals[0].payload  # the key A7 used to look for is absent

    report = audit_run(AuditContext(run.id, events, None, engine.policy_sha256))

    assert _control(report, "A7").status is ControlStatus.PASSED
    assert _findings(report, "A7") == []


def test_a7_passes_for_an_approval_recorded_by_the_check_action_path() -> None:
    """The `Gate.check_action` writer shape (`decision_id`) keeps passing A7."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=auto_approve)
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})
    approval = next(e for e in log.events() if e.type is EventType.HUMAN_APPROVAL)
    assert "decision_id" in approval.payload

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A7").status is ControlStatus.PASSED
    assert _findings(report, "A7") == []


def test_a7_still_fails_a_request_answered_only_by_an_unrelated_approval() -> None:
    """The tolerant reader must stay a control: any decision id is not the requested one.

    A reader that accepted an approval naming a *different* decision -- or naming none at all --
    would make A7 vacuous, which is worse than the bug it replaced.
    """
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)  # the review is requested and never answered
    log.append(EventType.RUN_STARTED, system, {})
    gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    # An approval for some other decision, recorded under the control-plane key.
    log.append(
        EventType.HUMAN_APPROVAL,
        system,
        {"policy_decision_id": new_id("decision"), "approved": True},
    )
    # ...and one that names no decision at all.
    log.append(EventType.HUMAN_APPROVAL, system, {"approved": True})
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a7 = _control(report, "A7")
    assert a7.status is ControlStatus.FAILED
    assert "without an answer" in a7.detail
    assert _findings(report, "A7") != []


@pytest.mark.parametrize("approved", [True, False])
def test_a7_does_not_credit_a_declared_human_answer(approved: bool) -> None:
    """A hash-valid answer from an unauthenticated human remains unresolved to MIRA."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)
    log.append(EventType.RUN_STARTED, system, {})
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False),
        {"decision_id": decision.id, "approved": approved, "automatic": False},
        subject_id="run",
    )

    result = controls.a7_approvals_resolved(AuditContext(log.run_id, log.events()))

    assert result[0] is ControlStatus.FAILED
    assert "without an answer" in result[1]


def test_a7_does_not_credit_a_human_answer_marked_automatic() -> None:
    """A human event with ``automatic=True`` remains unresolved to the independent audit."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)
    log.append(EventType.RUN_STARTED, system, {})
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": True},
        subject_id="run",
    )

    result = controls.a7_approvals_resolved(AuditContext(log.run_id, log.events()))

    assert result[0] is ControlStatus.FAILED
    assert "without an answer" in result[1]


# ------------------------------------------------------ A8: reviews resolved before run close


def test_a8_fails_an_unresolved_review_at_run_close() -> None:
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)  # a review is recorded but never answered
    log.append(EventType.RUN_STARTED, system, {})
    gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A8").status is ControlStatus.FAILED
    assert "A8" in {f.control_id for f in report.findings}


def test_a8_does_not_credit_a_human_answer_marked_automatic() -> None:
    """A human event with ``automatic=True`` cannot resolve A8 before Run close."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)
    log.append(EventType.RUN_STARTED, system, {})
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": True},
        subject_id="run",
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A8").status is ControlStatus.FAILED


def test_a8_passes_when_a_review_is_resolved_before_close() -> None:
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=auto_approve)  # the review is answered synchronously
    log.append(EventType.RUN_STARTED, system, {})
    gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A8").status is ControlStatus.PASSED
    assert _findings(report, "A8") == []


def test_a8_is_not_applicable_without_a_review() -> None:
    log, system = _open_run()
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A8").status is ControlStatus.NOT_APPLICABLE


# ----------------------------------------------------- A15: risk classified before tool execution


def test_a15_fails_when_a_tool_runs_before_any_risk_classification() -> None:
    log, system = _open_run()
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=new_id("tool"))
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A15").status is ControlStatus.FAILED
    assert "A15" in {f.control_id for f in report.findings}


def test_a15_passes_when_risk_classification_precedes_the_first_tool() -> None:
    log, system = _open_run()
    log.append(
        EventType.AGENT_MESSAGE,
        system,
        {"agent": "risk-classifier", "status": "classified", "text": "low"},
    )
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=new_id("tool"))
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A15").status is ControlStatus.PASSED
    assert _findings(report, "A15") == []


def test_a15_is_not_applicable_without_any_tool() -> None:
    log, system = _open_run()
    log.append(EventType.AGENT_MESSAGE, system, {"agent": "risk-classifier"})
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A15").status is ControlStatus.NOT_APPLICABLE


# --------------------------------------------- A24: model routing respects the role floor


def test_a24_fails_a_mira_lifecycle_running_below_the_role_floor() -> None:
    # MIRA's floor is STANDARD; a FAST selection violates it even beside a valid one.
    log, system = _open_run()
    agent_id = new_id("agent")
    log.append(EventType.AGENT_STARTED, system, {"agent": "mira-audit"}, subject_id=agent_id)
    log.append(EventType.MODEL_SELECTED, system, _selection("mira", "STANDARD"))
    log.append(EventType.MODEL_SELECTED, system, _selection("mira", "FAST"))
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=agent_id)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a24 = _control(report, "A24")
    assert a24.status is ControlStatus.FAILED
    assert "floor" in a24.detail


def test_a24_fails_a_tool_event_before_any_valid_selection() -> None:
    log, system = _open_run()
    agent_id = new_id("agent")
    log.append(EventType.AGENT_STARTED, system, {}, subject_id=agent_id)
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=new_id("tool"))
    log.append(EventType.MODEL_SELECTED, system, _selection("agent", "FAST"))  # too late
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=agent_id)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a24 = _control(report, "A24")
    assert a24.status is ControlStatus.FAILED
    assert "before a valid model.selected" in a24.detail


def test_a24_does_not_fail_a_replayed_deferred_call_resumed_without_a_fresh_selection() -> None:
    """A resumed step's replayed tool call needs no `model.selected` of its own.

    Bug-hunt follow-up: the deferred-tool resume mechanism.

    `thymira.agents.resume` lets an approved deferred call replay through `deferred_tool_results`
    instead of asking the model again, so the resumed lifecycle's `tool.started`/`tool.completed`
    legitimately have no preceding selection in *their own* window -- the call was proposed, and
    its proposing selection checked, in the earlier (now-closed) parking lifecycle.
    """
    log, system = _open_run()
    decision_id = new_id("decision")
    tool_call_id = new_id("tool")
    parking_agent, resumed_agent = new_id("agent"), new_id("agent")
    # The parking lifecycle: a valid selection proposes the call, which is left for a human.
    log.append(EventType.AGENT_STARTED, system, {}, subject_id=parking_agent)
    log.append(EventType.MODEL_SELECTED, system, _selection("agent", "STANDARD"))
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED, system, {"decision_id": decision_id}, subject_id=None
    )
    log.append(EventType.TOOL_DENIED, system, {"decision_id": decision_id}, subject_id=None)
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=parking_agent)
    # A human answers; the resumed lifecycle replays the same call with no fresh selection.
    log.append(EventType.HUMAN_APPROVAL, system, {"decision_id": decision_id})
    log.append(EventType.AGENT_STARTED, system, {}, subject_id=resumed_agent)
    log.append(
        EventType.TOOL_STARTED,
        system,
        {"decision_id": decision_id},
        subject_id=tool_call_id,
    )
    log.append(EventType.TOOL_COMPLETED, system, {}, subject_id=tool_call_id)
    log.append(EventType.MODEL_SELECTED, system, _selection("agent", "STANDARD"))
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=resumed_agent)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A24").status is ControlStatus.PASSED
    assert _findings(report, "A24") == []


def test_a24_fails_agent_completed_without_any_selection() -> None:
    log, system = _open_run()
    agent_id = new_id("agent")
    log.append(EventType.AGENT_STARTED, system, {}, subject_id=agent_id)
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=agent_id)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a24 = _control(report, "A24")
    assert a24.status is ControlStatus.FAILED
    assert "no valid model.selected" in a24.detail


def test_a24_does_not_fail_when_agent_started_opens_the_lifecycle() -> None:
    # agent.started is the first lifecycle event and needs no preceding selection.
    log, system = _open_run()
    agent_id = new_id("agent")
    log.append(EventType.AGENT_STARTED, system, {}, subject_id=agent_id)
    log.append(EventType.MODEL_SELECTED, system, _selection("mira", "STANDARD"))
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=agent_id)
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A24").status is ControlStatus.PASSED
    assert _findings(report, "A24") == []


def test_a24_fails_a_below_floor_selection_made_outside_every_agent_lifecycle() -> None:
    """The floor is a property of the routed call, not of the window it sits in.

    Core routes the risk classifier and MIRA routes its own finding verifier without opening an
    agent lifecycle, so a selection folded into no lifecycle used to escape the control entirely.
    """
    log, system = _open_run()
    log.append(
        EventType.MODEL_SELECTED, system, _selection("mira", "FAST")
    )  # outside any lifecycle
    log.append(EventType.RUN_COMPLETED, system, {})

    a24 = _control(audit_run(AuditContext(log.run_id, log.events())), "A24")

    assert a24.status is ControlStatus.FAILED
    assert "outside any agent lifecycle is below the role floor" in a24.detail


def test_a24_passes_a_compliant_selection_made_outside_every_agent_lifecycle() -> None:
    """Auditing an unscoped selection must not convict one that respects its floor."""
    log, system = _open_run()
    log.append(EventType.MODEL_SELECTED, system, _selection("mira", "FRONTIER"))
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A24").status is ControlStatus.PASSED
    assert _findings(report, "A24") == []


def test_a24_is_not_applicable_without_any_routing_at_all() -> None:
    """With neither a lifecycle nor a selection there is no routing to audit."""
    log, system = _open_run()
    log.append(EventType.RUN_COMPLETED, system, {})

    a24 = _control(audit_run(AuditContext(log.run_id, log.events())), "A24")

    assert a24.status is ControlStatus.NOT_APPLICABLE
    assert a24.detail == "no agent lifecycle and no model selection"


# ------------------------------------------ the approval decision-id accessor: one shared reader


def _request_approval(log: InMemoryEventLog, system: Actor, decision_id: str) -> None:
    """Record an approval request naming `decision_id`, the way both Gate paths write it."""
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        system,
        {"decision_id": decision_id, "summary": "review"},
    )


def test_the_controls_read_the_shared_approval_accessor() -> None:
    """MIRA keeps no second copy of the accessor: one name, one precedence, one repository.

    The deleted local copy read `("decision_id", "policy_decision_id")` -- the *reverse* of the
    shared `DECISION_ID_KEYS` -- so one repository held two functions of the same name that
    disagreed about which decision a payload carrying both keys names. This dies if a copy comes
    back.
    """
    assert controls.approval_decision_id is approval_decision_id
    assert not hasattr(controls, "_APPROVAL_DECISION_ID_KEYS")


def test_a7_prefers_the_contract_field_when_an_approval_names_two_decisions() -> None:
    """Precedence pinned at the control: the validated `Approval` field outranks the ad-hoc key.

    A payload carrying both keys is one where something added a key beside a serialised
    `Approval`, so believing the record's own field is the fail-closed reading. This test and its
    twin below swap outcomes if the precedence is swapped.
    """
    requested, other = new_id("decision"), new_id("decision")
    log, system = _open_run()
    _request_approval(log, system, requested)
    log.append(
        EventType.HUMAN_APPROVAL,
        system,
        {"policy_decision_id": requested, "decision_id": other, "approved": True},
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A7").status is ControlStatus.PASSED
    assert _findings(report, "A7") == []


def test_a7_does_not_let_the_added_key_answer_a_request_the_contract_field_disowns() -> None:
    """The reversed precedence would let a key added beside an `Approval` redirect the answer."""
    requested, other = new_id("decision"), new_id("decision")
    log, system = _open_run()
    _request_approval(log, system, requested)
    log.append(
        EventType.HUMAN_APPROVAL,
        system,
        {"policy_decision_id": other, "decision_id": requested, "approved": True},
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A7").status is ControlStatus.FAILED
    assert _findings(report, "A7") != []


@pytest.mark.parametrize("key", ["id", "approval_id", "policy_id", "decision"])
def test_a7_ignores_a_decision_named_under_any_other_key(key: str) -> None:
    """The alternation is exactly two names; widening it would make A7 answerable by anything."""
    requested = new_id("decision")
    log, system = _open_run()
    _request_approval(log, system, requested)
    log.append(EventType.HUMAN_APPROVAL, system, {key: requested, "approved": True})
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A7").status is ControlStatus.FAILED


def test_a7_reads_the_request_side_through_the_same_accessor() -> None:
    """One vocabulary on both sides: a request is read under the same two names as its answer."""
    decision_id = new_id("decision")
    log, system = _open_run()
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        system,
        {"policy_decision_id": decision_id, "summary": "review"},
    )
    log.append(EventType.HUMAN_APPROVAL, system, {"decision_id": decision_id, "approved": True})
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A7").status is ControlStatus.PASSED


def _reviewed_tool_run(payload_keys: tuple[str, str]) -> tuple[InMemoryEventLog, PolicyEngine]:
    """A tool run authorised by a review whose approval names its decision under `payload_keys`.

    `payload_keys` is `(the key naming this decision, the key naming an unrelated one)`, so the
    caller chooses which of the two recorded field names carries the real id.
    """
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=None)  # the Gate records the review and answers nothing
    tool_id = new_id("tool")
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    decision = gate.check_action(
        subject_kind="tool_call", subject_id=tool_id, action_type="run_python"
    )
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW  # otherwise A3 passes vacuously
    names, unrelated = payload_keys
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,  # only a human's own answer authorises, whichever key names the decision
        {names: decision.id, unrelated: new_id("decision"), "approved": True},
        subject_id=tool_id,
    )
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=tool_id)
    log.append(EventType.RUN_COMPLETED, system, {})
    return log, engine


def test_a3_authorises_a_tool_on_an_approval_recorded_under_the_contract_field() -> None:
    """`_allowing` reads the control-plane approval shape (`policy_decision_id`) too."""
    log, engine = _reviewed_tool_run(("policy_decision_id", "decision_id"))

    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert _control(report, "A3").status is ControlStatus.PASSED


def test_a3_does_not_authorise_a_tool_whose_approval_names_another_decision() -> None:
    """Same precedence at the authorization call site: the added key cannot authorise a tool."""
    log, engine = _reviewed_tool_run(("decision_id", "policy_decision_id"))

    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    a3 = _control(report, "A3")
    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail


RUN_PYTHON_ARGUMENTS = {"code": "x"}
TICKET = tool_intent_sha256("run_python", RUN_PYTHON_ARGUMENTS)
"""The truthful `tool_intent_sha256` for the synthetic `run_python` effect."""


def _review_gated_call(
    gate: Gate, subject_id: str, *, ticket: str | None = None, tool: str = "run_python"
):
    """Ask the Gate for a review-gated tool-call decision the way `ToolManager` asks.

    Low confidence triggers the engine's fail-safe escalation, and `details` puts the call's
    `tool` and `tool_intent_sha256` on the `human.approval_requested` -- so the answer is bound to
    one exact call rather than to the tool's name.
    """
    resolved_ticket = ticket or tool_intent_sha256(tool, RUN_PYTHON_ARGUMENTS)
    decision = gate.check_capability(
        subject_id=subject_id,
        capability=ToolCapability(id=tool, external_effects=()),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
        summary=tool,
        details={
            "tool": tool,
            "arguments": RUN_PYTHON_ARGUMENTS,
            "tool_intent_sha256": resolved_ticket,
        },
    )
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW  # otherwise A3 passes vacuously
    return decision


def _reviewed_retry_run(*, approved: bool) -> tuple[InMemoryEventLog, PolicyEngine]:
    """A review-gated call, answered by a human, then retried as a *new* call naming the decision.

    This is the shape `ToolManager` writes for a one-shot ticket: the denied first attempt and
    the retry are two `ToolCall`s (two subjects); the retry mints no decision and names the
    answered one as `decision_id`, carrying the same `tool_intent_sha256` the request named.
    `approved` selects whether the retry ran (`tool.started`) or was denied as a rejection
    (`tool.denied`).
    """
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=None)
    first, retry = new_id("tool"), new_id("tool")
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    decision = _review_gated_call(gate, first)
    log.append(
        EventType.TOOL_DENIED,
        system,
        {
            "tool_call_id": first,
            "decision_id": decision.id,
            "reason": "review",
            "tool": "run_python",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "tool_intent_sha256": TICKET,
        },
        subject_id=first,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": approved, "automatic": False},
        subject_id=first,
    )
    if approved:
        log.append(
            EventType.TOOL_STARTED,
            system,
            {
                "tool": "run_python",
                "arguments": RUN_PYTHON_ARGUMENTS,
                "decision_id": decision.id,
                "tool_intent_sha256": TICKET,
            },
            subject_id=retry,
        )
        log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=retry)
    else:
        log.append(
            EventType.TOOL_DENIED,
            system,
            {
                "tool_call_id": retry,
                "decision_id": decision.id,
                "reason": "rejected",
                "tool": "run_python",
                "arguments": RUN_PYTHON_ARGUMENTS,
                "tool_intent_sha256": TICKET,
            },
            subject_id=retry,
        )
    log.append(EventType.RUN_COMPLETED, system, {})
    return log, engine


def _a3_a6(log: InMemoryEventLog, engine: PolicyEngine):
    """Audit a finished log and return its (A3, A6) results."""
    report = audit_run(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))
    return _control(report, "A3"), _control(report, "A6")


def _effect_review(
    log: InMemoryEventLog,
    engine: PolicyEngine,
    *,
    subject: str,
    tool: str,
    arguments: dict[str, str],
    ticket: str,
) -> PolicyDecision:
    """Record one review request carrying the exact effect evidence the manager records."""
    decision = Gate(engine, log).check_capability(
        subject_id=subject,
        capability=ToolCapability(id=tool, external_effects=()),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
        details={"tool": tool, "arguments": arguments, "tool_intent_sha256": ticket},
    )
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    return decision


@pytest.mark.parametrize("same_subject", [False, True], ids=["cross-subject", "same-subject"])
def test_a3_recomputes_the_started_effect_before_spending_a_ticket(same_subject: bool) -> None:
    """Copying an approved digest onto altered arguments authorises no execution."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    approved_arguments = {"value": "approved"}
    ticket = tool_intent_sha256("echo", approved_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=approved_arguments,
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    started = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": {"value": "altered"},
            "tool_intent_sha256": ticket,
            "decision_id": decision.id,
        },
        subject_id=reviewed if same_subject else new_id("tool"),
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_recomputes_the_effect_shown_in_the_approval_request() -> None:
    """A request cannot carry another call's digest beside the arguments shown to the human."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    executed_arguments = {"value": "executed"}
    ticket = tool_intent_sha256("echo", executed_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments={"value": "shown-to-human"},
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    started = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": executed_arguments,
            "tool_intent_sha256": ticket,
            "decision_id": decision.id,
        },
        subject_id=new_id("tool"),
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


@pytest.mark.parametrize(
    "arguments",
    [None, ["not", "an", "object"]],
    ids=["missing", "malformed"],
)
def test_a3_rejects_a_ticketed_start_without_object_arguments(arguments: object) -> None:
    """A claimed ticket with no recomputable argument object fails closed."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    approved_arguments = {"value": "approved"}
    ticket = tool_intent_sha256("echo", approved_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=approved_arguments,
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    payload: dict[str, object] = {
        "tool": "echo",
        "tool_intent_sha256": ticket,
        "decision_id": decision.id,
    }
    if arguments is not None:
        payload["arguments"] = arguments
    started = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        payload,
        subject_id=reviewed,
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_rejects_a_start_missing_the_digest_its_request_was_bound_to() -> None:
    """Arguments alone retain legacy meaning only when the approval request was unticketed."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    approved_arguments = {"value": "approved"}
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=approved_arguments,
        ticket=tool_intent_sha256("echo", approved_arguments),
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    started = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {"tool": "echo", "arguments": approved_arguments, "decision_id": decision.id},
        subject_id=reviewed,
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_allows_description_only_changes_to_a_recomputed_effect() -> None:
    """Description is narration, so a retry may rephrase it without changing its ticket."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    ticket = tool_intent_sha256("echo", {"value": "same", "description": "first wording"})
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments={"value": "same", "description": "first wording"},
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": {"value": "same", "description": "second wording"},
            "tool_intent_sha256": ticket,
            "decision_id": decision.id,
        },
        subject_id=new_id("tool"),
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.PASSED


@pytest.mark.parametrize("reviewed", [False, True], ids=["passing", "human-reviewed"])
def test_a3_accepts_legacy_unticketed_starts_that_record_arguments(reviewed: bool) -> None:
    """Pre-ticket manager starts remain auditable even though they already carried arguments."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    subject = new_id("tool")
    gate = Gate(engine, log)
    if reviewed:
        decision = gate.check_action(
            subject_kind="tool_call", subject_id=subject, action_type="run_python"
        )
        assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
        log.append(
            EventType.HUMAN_APPROVAL,
            REVIEWER,
            {"decision_id": decision.id, "approved": True, "automatic": False},
            subject_id=subject,
        )
    else:
        decision = gate.check_capability(
            subject_id=subject,
            capability=ToolCapability(id="echo", external_effects=()),
            risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0),
        )
        assert decision.decision is Decision.PASS
    log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {"tool": "echo", "arguments": {"value": "legacy"}},
        subject_id=subject,
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.PASSED


def test_a3_does_not_pool_an_approval_from_a_request_with_a_copied_ticket() -> None:
    """A second approval counts only when its request independently describes the same effect."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    approved_arguments = {"value": "approved"}
    ticket = tool_intent_sha256("echo", approved_arguments)
    valid = _effect_review(
        log,
        engine,
        subject=new_id("tool"),
        tool="echo",
        arguments=approved_arguments,
        ticket=ticket,
    )
    copied = _effect_review(
        log,
        engine,
        subject=new_id("tool"),
        tool="echo",
        arguments={"value": "different"},
        ticket=ticket,
    )
    for decision in (valid, copied):
        log.append(
            EventType.HUMAN_APPROVAL,
            REVIEWER,
            {"decision_id": decision.id, "approved": True, "automatic": False},
            subject_id=decision.subject_id,
        )
    payload = {
        "tool": "echo",
        "arguments": approved_arguments,
        "tool_intent_sha256": ticket,
        "decision_id": valid.id,
    }
    log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        payload,
        subject_id=new_id("tool"),
    )
    replay = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        payload,
        subject_id=new_id("tool"),
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[replay.seq]}"


@pytest.mark.parametrize(
    "malformed",
    [
        {"tool": "echo", "tool_intent_sha256": "a1" * 32},
        {
            "tool": "echo",
            "arguments": ["not", "an", "object"],
            "tool_intent_sha256": "a1" * 32,
        },
        {"tool": "echo", "arguments": {"value": "approved"}},
        {
            "tool": "echo",
            "arguments": {"value": "approved"},
            "tool_intent_sha256": 7,
        },
    ],
    ids=["missing-arguments", "malformed-arguments", "missing-digest", "malformed-digest"],
)
def test_a3_does_not_fall_back_or_rebind_after_a_malformed_first_request(
    malformed: dict[str, object],
) -> None:
    """Malformed ticket evidence stays first and cannot become legacy per-decision authority."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    decision = Gate(engine, log).check_action(
        subject_kind="tool_call", subject_id=reviewed, action_type="run_python"
    )
    ticket = tool_intent_sha256("echo", {"value": "approved"})
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"decision_id": decision.id, **malformed},
        subject_id=reviewed,
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision.id,
            "tool": "echo",
            "arguments": {"value": "approved"},
            "tool_intent_sha256": ticket,
        },
        subject_id=reviewed,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    started = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": {"value": "approved"},
            "tool_intent_sha256": ticket,
            "decision_id": decision.id,
        },
        subject_id=reviewed,
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_a_malformed_start_spends_the_legacy_decision_before_a_replay() -> None:
    """An invalid ticket claim cannot leave an unticketed decision available to the next start."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    subject = new_id("tool")
    decision = Gate(engine, log).check_action(
        subject_kind="tool_call", subject_id=subject, action_type="run_python"
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": True, "automatic": False},
        subject_id=subject,
    )
    malformed = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": {"value": "altered"},
            "tool_intent_sha256": tool_intent_sha256("echo", {"value": "other"}),
        },
        subject_id=subject,
    )
    replay = log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {"tool": "echo", "arguments": {"value": "legacy"}},
        subject_id=subject,
    )

    result = a3_authorization_before_tool(
        AuditContext(log.run_id, log.events(), None, engine.policy_sha256)
    )

    assert result[0] is ControlStatus.FAILED
    assert result[1] == (
        f"tool.started without an allowing decision at seq {[malformed.seq, replay.seq]}"
    )


@pytest.mark.parametrize("same_subject", [False, True], ids=["cross-subject", "same-subject"])
def test_a6_recomputes_a_rejected_denials_own_effect(same_subject: bool) -> None:
    """A rejected call's ticket cannot be copied onto a denial of another effect."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    approved_arguments = {"value": "reviewed"}
    ticket = tool_intent_sha256("echo", approved_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=approved_arguments,
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": False, "automatic": False},
        subject_id=reviewed,
    )
    denied_arguments = {"value": "changed"}
    denied = log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": denied_arguments,
            "tool_intent_sha256": tool_intent_sha256("echo", denied_arguments),
            "decision_id": decision.id,
            "reason": "tool call rejected by a human",
        },
        subject_id=reviewed if same_subject else new_id("tool"),
    )

    result = a6_denials_recorded(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.denied without a denying decision at seq {[denied.seq]}"


@pytest.mark.parametrize(
    "malformed",
    [
        {"tool": "echo", "tool_intent_sha256": "a1" * 32},
        {"tool": "echo", "arguments": {"value": "denied"}},
    ],
    ids=["missing-arguments", "missing-digest"],
)
def test_a6_does_not_fall_back_after_a_malformed_same_subject_request(
    malformed: dict[str, object],
) -> None:
    """A malformed first ticket binding cannot become a same-subject denial authority."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    decision = Gate(engine, log).check_action(
        subject_kind="tool_call", subject_id=reviewed, action_type="run_python"
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"decision_id": decision.id, **malformed},
        subject_id=reviewed,
    )
    denied_arguments = {"value": "denied"}
    denied = log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": denied_arguments,
            "tool_intent_sha256": tool_intent_sha256("echo", denied_arguments),
            "decision_id": decision.id,
            "reason": "tool call denied: needs human approval",
        },
        subject_id=reviewed,
    )

    result = a6_denials_recorded(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.denied without a denying decision at seq {[denied.seq]}"


@pytest.mark.parametrize(
    "arguments",
    [None, ["not", "an", "object"]],
    ids=["missing", "malformed"],
)
def test_a6_rejects_a_ticketed_denial_without_object_arguments(arguments: object) -> None:
    """A denial cannot inherit a rejection through ticket evidence MIRA cannot recompute."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    requested_arguments = {"value": "reviewed"}
    ticket = tool_intent_sha256("echo", requested_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=requested_arguments,
        ticket=ticket,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision.id, "approved": False, "automatic": False},
        subject_id=reviewed,
    )
    payload: dict[str, object] = {
        "tool": "echo",
        "decision_id": decision.id,
        "reason": "tool call rejected by a human",
        "tool_intent_sha256": ticket,
    }
    if arguments is not None:
        payload["arguments"] = arguments
    denied = log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        payload,
        subject_id=new_id("tool"),
    )

    result = a6_denials_recorded(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.denied without a denying decision at seq {[denied.seq]}"


def test_a6_rejects_a_denial_missing_the_digest_its_request_was_bound_to() -> None:
    """A ticket-dependent rejection cannot fall back to subject bookkeeping without its digest."""
    log = InMemoryEventLog(new_id("run"))
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    reviewed = new_id("tool")
    requested_arguments = {"value": "reviewed"}
    ticket = tool_intent_sha256("echo", requested_arguments)
    decision = _effect_review(
        log,
        engine,
        subject=reviewed,
        tool="echo",
        arguments=requested_arguments,
        ticket=ticket,
    )
    denied = log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool": "echo",
            "arguments": requested_arguments,
            "decision_id": decision.id,
            "reason": "tool call denied: needs human approval",
        },
        subject_id=reviewed,
    )

    result = a6_denials_recorded(AuditContext(log.run_id, log.events(), None, engine.policy_sha256))

    assert result[0] is ControlStatus.FAILED
    assert result[1] == f"tool.denied without a denying decision at seq {[denied.seq]}"


def _run_python_start(log: InMemoryEventLog, decision_id: str, *, ticket: str = TICKET) -> int:
    """Record a `run_python` execution on a brand-new subject naming `decision_id`."""
    system = Actor.system()
    subject = new_id("tool")
    seq = log.append(
        EventType.TOOL_STARTED,
        system,
        {
            "tool": "run_python",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "decision_id": decision_id,
            "tool_intent_sha256": ticket,
        },
        subject_id=subject,
    ).seq
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=subject)
    return seq


def test_a3_authorises_a_retry_that_names_the_decision_a_human_approved() -> None:
    """The manager's ticket trace: one human answer, one execution of that exact call."""
    log, engine = _reviewed_retry_run(approved=True)

    a3, a6 = _a3_a6(log, engine)

    assert a3.status is ControlStatus.PASSED
    assert a6.status is ControlStatus.PASSED


def test_a6_explains_a_retry_denied_under_the_decision_a_human_rejected() -> None:
    """A rejected review still explains the denial of the retry that names its ticket."""
    log, engine = _reviewed_retry_run(approved=False)

    _, a6 = _a3_a6(log, engine)

    assert a6.status is ControlStatus.PASSED
    assert a6.detail == "2 denials explained"


def test_a3_does_not_authorise_a_start_naming_an_unknown_or_unapproved_decision() -> None:
    """Naming a decision the log never recorded authorises nothing."""
    log, engine = _reviewed_retry_run(approved=True)
    _run_python_start(log, new_id("decision"))

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail


def test_a3_does_not_authorise_a_start_naming_a_review_the_human_rejected() -> None:
    """The other half of the same guard: a recorded decision still has to be an allowing one."""
    log, engine = _reviewed_retry_run(approved=False)
    _run_python_start(log, _decision_ids(log)[0])

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail


# ------------------------------- A3 refuses authorization laundered through a decision it names


def _decision_ids(log: InMemoryEventLog) -> list[str]:
    """Every policy decision id the log recorded, in order."""
    return [e.payload["id"] for e in log.events() if e.type is EventType.POLICY_DECISION]


def _laundering_run() -> tuple[InMemoryEventLog, PolicyEngine, Gate]:
    """A started run whose risk is classified, ready to record a decision to launder."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    log.append(EventType.AGENT_MESSAGE, system, {"agent": "risk-classifier", "text": "limited"})
    return log, engine, Gate(engine, log, approver=None)


def test_a3_does_not_authorise_a_start_naming_the_run_level_execution_decision() -> None:
    """A decision about the *run* authorises no tool call, however loudly a start names it."""
    log, engine, gate = _laundering_run()
    run_level = gate.check_action(
        subject_kind="run", subject_id="run", action_type="execution.start"
    )
    assert run_level.decision is Decision.PASS
    _run_python_start(log, run_level.id)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail


def test_a3_does_not_authorise_a_start_naming_another_tool_calls_passing_decision() -> None:
    """One call's PASS is not a licence for a different call, even under the same subject kind."""
    log, engine, gate = _laundering_run()
    benign = new_id("tool")
    passing = gate.check_capability(
        subject_id=benign,
        capability=ToolCapability(id="read_file", external_effects=()),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0),
        summary="read_file",
    )
    assert passing.decision is Decision.PASS
    log.append(EventType.TOOL_STARTED, Actor.system(), {"tool": "read_file"}, subject_id=benign)
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=benign)
    dangerous = _run_python_start(log, passing.id)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[dangerous]}"


def test_a3_reports_a_call_a_human_refused_that_ran_under_the_run_level_decision() -> None:
    """The clean bypass: a human said no, the writer ran it and stamped the run's PASS."""
    log, engine, gate = _laundering_run()
    run_level = gate.check_action(
        subject_kind="run", subject_id="run", action_type="execution.start"
    )
    refused = new_id("tool")
    review = _review_gated_call(gate, refused)
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": False, "automatic": False},
        subject_id=refused,
    )
    started = log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": "run_python",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "decision_id": run_level.id,
            "tool_intent_sha256": TICKET,
        },
        subject_id=refused,
    ).seq
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=refused)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[started]}"


def test_a3_spends_a_human_approval_on_one_execution_only() -> None:
    """One answer, one execution: the second and third calls under one ticket are unauthorised."""
    log, engine = _reviewed_retry_run(approved=True)
    approved_decision = _decision_ids(log)[0]
    second = _run_python_start(log, approved_decision)
    third = _run_python_start(log, approved_decision)

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[second, third]}"


def test_a3_does_not_authorise_a_start_under_an_automatically_answered_review() -> None:
    """An automatic answer is not a human's: `Gate(approver=auto_approve)` authorises nothing."""
    log, engine, _ = _laundering_run()
    gate = Gate(engine, log, approver=auto_approve)
    review = _review_gated_call(gate, new_id("tool"))
    answer = next(e for e in log.events() if e.type is EventType.HUMAN_APPROVAL)
    assert answer.actor.kind is ActorKind.SYSTEM  # the Gate's automation actor, not a human
    _run_python_start(log, review.id)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert "without an allowing decision" in a3.detail


def test_a3_does_not_authorise_a_start_whose_ticket_differs_from_the_request() -> None:
    """The answer is bound to one exact call; a different call may not spend it."""
    log, engine = _reviewed_retry_run(approved=True)
    other_call = _run_python_start(log, _decision_ids(log)[0], ticket="b2" * 32)

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[other_call]}"


def test_a3_authorises_a_start_on_the_subject_its_own_review_approved() -> None:
    """A synchronous human approval authorises the call it was asked about, on its own subject."""
    log, engine, gate = _laundering_run()
    subject = new_id("tool")
    review = _review_gated_call(gate, subject)
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": True, "automatic": False},
        subject_id=subject,
    )
    log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": "run_python",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "decision_id": review.id,
            "tool_intent_sha256": TICKET,
        },
        subject_id=subject,
    )
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)
    assert a3.status is ControlStatus.PASSED

    spent = _run_python_start(log, review.id)
    a3_again, _ = _a3_a6(log, engine)

    assert a3_again.status is ControlStatus.FAILED
    assert a3_again.detail == f"tool.started without an allowing decision at seq {[spent]}"


def test_a8_resolves_a_review_answered_under_the_contract_field() -> None:
    """A8's equality test reads both recorded names, like A7's."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    engine = PolicyEngine(load_policy_stack())
    gate = Gate(engine, log, approver=None)
    log.append(EventType.RUN_STARTED, system, {})
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    log.append(
        EventType.HUMAN_APPROVAL, system, {"policy_decision_id": decision.id, "approved": True}
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    assert _control(report, "A8").status is ControlStatus.PASSED
    assert _findings(report, "A8") == []


def test_a8_does_not_resolve_a_review_with_no_id_by_an_approval_with_no_id() -> None:
    """`None == None` is the fail-open A8 had: a payload naming nothing resolves nothing.

    A `policy.decision` payload without an `id` compared equal to an approval naming no decision,
    so an unanswerable review audited as resolved. The shared `approval_names_decision` refuses
    both halves of that match.
    """
    log, system = _open_run()
    log.append(
        EventType.POLICY_DECISION,
        system,
        {"decision": Decision.REQUIRE_HUMAN_REVIEW.value, "reason": "review", "rule_id": "r"},
    )
    log.append(EventType.HUMAN_APPROVAL, system, {"approved": True})
    log.append(EventType.RUN_COMPLETED, system, {})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a8 = _control(report, "A8")
    assert a8.status is ControlStatus.FAILED
    assert "unresolved before run close" in a8.detail


# ------------------------------------------------------------------------- A9: lifecycle pairing


def _agent_started(log: InMemoryEventLog, system: Actor, subject: str | None) -> None:
    """Record an `agent.started`, with or without the subject that identifies the agent."""
    log.append(EventType.AGENT_STARTED, system, {"name": "thy"}, subject_id=subject)


def _agent_completed(log: InMemoryEventLog, system: Actor, subject: str | None) -> None:
    """Record an `agent.completed`, with or without the subject that identifies the agent."""
    log.append(EventType.AGENT_COMPLETED, system, {}, subject_id=subject)


def _a9(log: InMemoryEventLog) -> tuple[ControlStatus, str, int]:
    """Audit `log` and return A9's status, detail and finding count."""
    report = audit_run(AuditContext(log.run_id, log.events()))
    result = _control(report, "A9")
    return result.status, result.detail, len(_findings(report, "A9"))


def test_a9_does_not_pass_a_log_whose_lifecycle_events_carry_no_subject() -> None:
    """Legacy evidence: every lifecycle event written before wave 2 carries `subject_id=None`.

    Pairing was set membership, so the started set was `{None}`, the ended set was `{None}`, and
    an agent that started and died read as PAIRED -- A9 returned PASSED on exactly the evidence it
    exists to police. `None` is not an identity: nothing here is verifiable, and the honest verdict
    is that the evidence this control audits is absent, not that the run is clean and not a MEDIUM
    finding on every run recorded before the writers stamped a subject.
    """
    log, system = _open_run()
    _agent_started(log, system, None)  # this one completed...
    _agent_completed(log, system, None)
    _agent_started(log, system, None)  # ...and this one died
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is not ControlStatus.PASSED
    assert status is ControlStatus.NOT_APPLICABLE
    assert "not verifiable" in detail
    assert findings == 0


def test_a9_pairs_current_evidence_by_subject() -> None:
    """Wave-2 evidence: every lifecycle event names its agent, so pairing is really verified."""
    log, system = _open_run()
    first, second = new_id("agent"), new_id("agent")
    _agent_started(log, system, first)
    _agent_started(log, system, second)
    _agent_completed(log, system, second)
    _agent_completed(log, system, first)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, _, findings = _a9(log)

    assert status is ControlStatus.PASSED
    assert findings == 0


def test_a9_still_detects_an_unpaired_current_agent() -> None:
    """The control still convicts: an identified start no end can belong to is proof."""
    log, system = _open_run()
    finished, died = new_id("agent"), new_id("agent")
    _agent_started(log, system, finished)
    _agent_completed(log, system, finished)
    _agent_started(log, system, died)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.FAILED
    assert died in detail
    assert finished not in detail
    assert findings == 1


def test_a9_mixed_evidence_does_not_pass_on_the_identified_half() -> None:
    """A run spanning the upgrade: a verified pair proves nothing about an anonymous start."""
    log, system = _open_run()
    identified = new_id("agent")
    _agent_started(log, system, identified)
    _agent_completed(log, system, identified)
    _agent_started(log, system, None)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.NOT_APPLICABLE
    assert "not verifiable" in detail
    assert findings == 0


def test_a9_mixed_evidence_still_fails_a_start_no_end_could_belong_to() -> None:
    """An unidentified *start* excuses nothing: it is not an end, so it closes no lifecycle."""
    log, system = _open_run()
    died = new_id("agent")
    _agent_started(log, system, None)
    _agent_started(log, system, died)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.FAILED
    assert died in detail
    assert findings == 1


def test_a9_does_not_convict_a_start_an_unidentified_end_could_close() -> None:
    """The deliberate mixed-case call: an end that identifies nobody could be anybody's.

    A start stamped by the new writer and an end recorded by the old one is what the upgrade looks
    like from the reader's side. Convicting there would cry wolf on a lifecycle that may well have
    closed; only a start no recorded end could belong to is proof of a dangling lifecycle.
    """
    log, system = _open_run()
    _agent_started(log, system, new_id("agent"))
    _agent_completed(log, system, None)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.NOT_APPLICABLE
    assert "not verifiable" in detail
    assert findings == 0


def test_a9_convicts_the_starts_no_unidentified_end_can_cover() -> None:
    """One end that identifies nobody excuses one unmatched start, not every unmatched start."""
    log, system = _open_run()
    first, second = new_id("agent"), new_id("agent")
    _agent_started(log, system, first)
    _agent_started(log, system, second)
    _agent_completed(log, system, None)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.FAILED
    assert (first in detail) != (second in detail)  # exactly one is proved dangling
    assert findings == 1


def test_a9_passes_when_a_stray_end_identifies_nobody() -> None:
    """A9 audits starts, not ends: an unattributable end cannot falsify a verified pairing."""
    log, system = _open_run()
    agent_id = new_id("agent")
    _agent_started(log, system, agent_id)
    _agent_completed(log, system, agent_id)
    _agent_completed(log, system, None)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, _, findings = _a9(log)

    assert status is ControlStatus.PASSED
    assert findings == 0


def test_a9_reads_tool_lifecycles_by_the_same_rule() -> None:
    """Tools pair by `subject_id` too, so legacy tool evidence is unverifiable in the same way."""
    log, system = _open_run()
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=None)
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=None)
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, findings = _a9(log)

    assert status is ControlStatus.NOT_APPLICABLE
    assert "not verifiable" in detail
    assert findings == 0


def test_a9_separates_missing_evidence_from_unverifiable_evidence() -> None:
    """Both are NOT_APPLICABLE; only the detail tells a reader which run they are looking at."""
    log, system = _open_run()
    log.append(EventType.RUN_COMPLETED, system, {})

    status, detail, _ = _a9(log)

    assert status is ControlStatus.NOT_APPLICABLE
    assert detail == "no agent or tool lifecycle events"


# ------------------------------------------ A3 and A6 over the traces the real ToolManager writes


def _echo_manager() -> ToolManager:
    """A Tool Manager holding one validated local tool, so a call has a real ticket identity."""
    capability = ToolCapability(id="echo", external_effects=())
    return ToolManager(ToolRegistry((FakeTool("echo", capability, arguments_model=EchoArguments),)))


def _answer(context, decision_id: str, *, approved: bool) -> None:
    """Record the human's own answer to a pending review, the way the API's approver would."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def _audited(context):
    """Close the Run the manager wrote to and return its (A3, A6) results."""
    context.event_log.append(EventType.RUN_COMPLETED, Actor.system(), {})
    report = audit_run(
        AuditContext(
            context.run_id, context.event_log.events(), None, context.gate.engine.policy_sha256
        )
    )
    return _control(report, "A3"), _control(report, "A6")


def test_a3_and_a6_accept_the_manager_trace_of_an_approved_call_and_a_spent_retry(
    tmp_path: Path,
) -> None:
    """One human answer buys one execution; the manager denies the third call and MIRA agrees."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _answer(context, first.pending_approval.id, approved=True)
    second = manager.execute(context, "echo", {"value": "x"})
    third = manager.execute(context, "echo", {"value": "x"})
    assert (second.call.status, third.call.status) == (
        ToolCallStatus.COMPLETED,
        ToolCallStatus.DENIED,
    )

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.PASSED
    assert a6.status is ControlStatus.PASSED


def test_a3_and_a6_accept_the_manager_trace_of_a_rejected_call(tmp_path: Path) -> None:
    """A rejection is final for the Run: nothing ran, and both denials point at the review."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _answer(context, first.pending_approval.id, approved=False)
    second = manager.execute(context, "echo", {"value": "x"})
    assert second.call.status is ToolCallStatus.DENIED

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.NOT_APPLICABLE
    assert a6.status is ControlStatus.PASSED


@pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
def test_a3_and_a6_recompute_manager_tickets_when_the_tool_name_is_exact(
    tmp_path: Path, approved: bool
) -> None:
    """The canonical ticket evidence keeps the model-visible tool name exact."""
    tool_name = "-----BEGIN " + "PRIVATE KEY-----"
    capability = ToolCapability(id=tool_name, external_effects=())
    manager = ToolManager(
        ToolRegistry((FakeTool(tool_name, capability, arguments_model=EchoArguments),))
    )
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    first = manager.execute(context, tool_name, {"value": "x"})
    assert first.pending_approval is not None
    request = next(
        event
        for event in context.event_log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    assert request.payload["tool"] == tool_name
    _answer(context, first.pending_approval.id, approved=approved)

    second = manager.execute(context, tool_name, {"value": "x"})

    assert second.call.status is (ToolCallStatus.COMPLETED if approved else ToolCallStatus.DENIED)
    a3, a6 = _audited(context)
    expected_a3 = ControlStatus.PASSED if approved else ControlStatus.NOT_APPLICABLE
    assert a3.status is expected_a3
    assert a6.status is ControlStatus.PASSED


def test_a3_and_a6_accept_the_manager_trace_under_an_automatic_approver(tmp_path: Path) -> None:
    """The Gate's automatic answer authorises nothing, so the manager's refusals are explained."""
    context = review_gated_context(tmp_path, run_id=new_id("run"), approver=auto_approve)
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    second = manager.execute(context, "echo", {"value": "x"})
    assert (first.call.status, second.call.status) == (
        ToolCallStatus.DENIED,
        ToolCallStatus.DENIED,
    )

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.NOT_APPLICABLE
    assert a6.status is ControlStatus.PASSED


def _manager_ticket(context) -> str:
    """The `tool_intent_sha256` the manager put on the first *ticketed* approval request it wrote.

    Not the first request of any kind: a Run whose execution decision requires a human is parked
    on its own `execution.start` review first, and the Gate writes that one with no ticket at all
    -- the human is shown a Run, not an effect.
    """
    return next(
        event.payload["tool_intent_sha256"]
        for event in context.event_log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and "tool_intent_sha256" in event.payload
    )


def _forged_start(context, decision_id: str, *, tool: str = "echo"):
    """Append a `tool.started` on a brand-new subject claiming `decision_id` and the ticket."""
    return context.event_log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": tool,
            "arguments": {"value": "x"},
            "decision_id": decision_id,
            "tool_intent_sha256": _manager_ticket(context),
        },
        subject_id=new_id("tool"),
    )


def _two_pending_reviews(context, manager: ToolManager) -> list:
    """Ask for the identical call twice before anyone answers: two decisions on one ticket."""
    pending = []
    for _ in range(2):
        request = manager.execute(context, "echo", {"value": "x"})
        assert request.pending_approval is not None
        pending.append(request.pending_approval)
    assert pending[0].id != pending[1].id
    return pending


@pytest.mark.parametrize("reverse_answers", [False, True])
def test_a3_pools_two_human_approvals_of_one_ticket(tmp_path: Path, reverse_answers: bool) -> None:
    """Two answers to one call buy two executions, both stamped with the decisive decision.

    The manager binds an answer to the call, not to the decision that requested it: the identical
    call asked twice leaves two pending decisions, a human answering both leaves two approvals on
    one ticket, and *both* executions name the decisive answer's decision. Counting approvals and
    starts per decision id -- as A3 did -- makes the second execution look unauthorised on a Run
    the runtime allowed, and `GOV-101` turns that CRITICAL into a BLOCK.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    pending = _two_pending_reviews(context, manager)
    answered = list(reversed(pending)) if reverse_answers else list(pending)
    for decision in answered:
        _answer(context, decision.id, approved=True)
    executions = [manager.execute(context, "echo", {"value": "x"}) for _ in range(3)]
    assert [execution.call.status for execution in executions] == [
        ToolCallStatus.COMPLETED,
        ToolCallStatus.COMPLETED,
        ToolCallStatus.DENIED,
    ]
    started = [e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED]
    assert {e.payload["decision_id"] for e in started} == {answered[-1].id}

    a3, a6 = _audited(context)

    assert (a3.status, a3.detail) == (ControlStatus.PASSED, "2 executions authorised")
    assert a6.status is ControlStatus.PASSED


def test_a3_spends_a_twice_approved_ticket_on_two_executions_only(tmp_path: Path) -> None:
    """Two answers, two executions: a third start on the same ticket is the only finding."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    pending = _two_pending_reviews(context, manager)
    for decision in pending:
        _answer(context, decision.id, approved=True)
    for _ in range(2):
        assert manager.execute(context, "echo", {"value": "x"}).call.status is (
            ToolCallStatus.COMPLETED
        )
    forged = _forged_start(context, pending[-1].id)

    a3, _ = _audited(context)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


@pytest.mark.parametrize("rejected", [0, 1], ids=["reject-first", "reject-second"])
def test_a3_a_sibling_rejection_is_final_for_the_ticket(tmp_path: Path, rejected: int) -> None:
    """A rejection anywhere among a ticket's answers denies it, whichever sibling a start names.

    The manager refuses the next identical call outright (`_human_answer` lets a rejection win
    wherever it sits), so a start claiming the *approved* sibling of that same ticket is an
    execution the runtime never allowed.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    pending = _two_pending_reviews(context, manager)
    for index, decision in enumerate(pending):
        _answer(context, decision.id, approved=index != rejected)
    denied = manager.execute(context, "echo", {"value": "x"})
    assert denied.call.status is ToolCallStatus.DENIED
    assert denied.call.error is not None
    assert "rejected by a human" in denied.call.error
    forged = _forged_start(context, pending[1 - rejected].id)

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"
    assert a6.status is ControlStatus.PASSED


@pytest.mark.parametrize("has_ticket", [False, True], ids=["no-ticket", "ticket"])
@pytest.mark.parametrize("named", [False, True], ids=["by-subject", "named"])
def test_a3_same_subject_cannot_replay_a_human_approval(named: bool, has_ticket: bool) -> None:
    """One human answer buys one execution on its own subject too, named or paired by subject.

    The same-subject branch used to authorise a decision without ever spending it, so a writer
    could replay one approved review as many executions as it liked under one `ToolCall`.
    """
    log, engine, gate = _laundering_run()
    subject = new_id("tool")
    if has_ticket:
        review = _review_gated_call(gate, subject)
    else:
        review = gate.check_action(
            subject_kind="tool_call", subject_id=subject, action_type="run_python"
        )
        assert review.decision is Decision.REQUIRE_HUMAN_REVIEW
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": True, "automatic": False},
        subject_id=subject,
    )
    payload: dict[str, object] = {"tool": "run_python"}
    if named:
        payload["decision_id"] = review.id
    if has_ticket:
        payload["arguments"] = RUN_PYTHON_ARGUMENTS
        payload["tool_intent_sha256"] = TICKET
    log.append(EventType.TOOL_STARTED, Actor.system(), payload, subject_id=subject)
    replay = log.append(EventType.TOOL_STARTED, Actor.system(), payload, subject_id=subject)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[replay.seq]}"


@pytest.mark.parametrize("named", [False, True], ids=["by-subject", "named"])
def test_a3_does_not_read_a_run_level_decision_as_tool_authority_on_its_own_subject(
    named: bool,
) -> None:
    """A `tool.started` whose subject *is* the run cannot inherit the run-level `execution.start`.

    The legacy same-subject path paired a start with the latest decision for its subject without
    ever asking what that decision was about, so a start recorded under `subject_id="run"` was
    authorised by the run's own PASS.
    """
    log, engine, gate = _laundering_run()
    run_level = gate.check_action(
        subject_kind="run", subject_id="run", action_type="execution.start"
    )
    assert run_level.decision is Decision.PASS
    payload = (
        {"tool": "run_python", "decision_id": run_level.id} if named else {"tool": "run_python"}
    )
    forged = log.append(EventType.TOOL_STARTED, Actor.system(), payload, subject_id="run")
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id="run")
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


@pytest.mark.parametrize(
    ("tool", "expected"),
    [("run_python", ControlStatus.FAILED), ("echo", ControlStatus.PASSED)],
    ids=["borrowed", "the-approved-call"],
)
def test_a3_refuses_a_ticket_borrowed_by_a_different_tool(
    tool: str, expected: ControlStatus
) -> None:
    """The answer names a tool as well as a digest: another tool may not spend it.

    A3 compared the ticket string a start carried with the one the request recorded, but never
    the tool, so a `run_python` execution copying an approved `echo` call's still-unspent ticket
    was authorised evidence.
    """
    log, engine, gate = _laundering_run()
    review = _review_gated_call(gate, new_id("tool"), tool="echo")
    echo_ticket = tool_intent_sha256("echo", RUN_PYTHON_ARGUMENTS)
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": True, "automatic": False},
        subject_id=review.subject_id,
    )
    subject = new_id("tool")
    started = log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": tool,
            "arguments": RUN_PYTHON_ARGUMENTS,
            "decision_id": review.id,
            "tool_intent_sha256": echo_ticket,
        },
        subject_id=subject,
    )
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is expected
    if expected is ControlStatus.FAILED:
        assert a3.detail == f"tool.started without an allowing decision at seq {[started.seq]}"


@pytest.mark.parametrize("with_ticket", [False, True], ids=["no-ticket", "ticket"])
def test_a3_refuses_a_cross_subject_claim_on_a_review_that_named_no_call(
    with_ticket: bool,
) -> None:
    """A review requested without a ticket authorises its own subject and nobody else.

    `Gate.check_action` records no `tool_intent_sha256`, so there is nothing to recompute: a start
    on another subject naming that decision is claiming an authorization the log cannot verify,
    and carrying a ticket string of its own proves nothing either.
    """
    log, engine, gate = _laundering_run()
    reviewed = new_id("tool")
    review = gate.check_action(
        subject_kind="tool_call", subject_id=reviewed, action_type="run_python"
    )
    assert review.decision is Decision.REQUIRE_HUMAN_REVIEW
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": True, "automatic": False},
        subject_id=reviewed,
    )
    payload: dict[str, str] = {"tool": "run_python", "decision_id": review.id}
    if with_ticket:
        payload["tool_intent_sha256"] = TICKET
    subject = new_id("tool")
    forged = log.append(EventType.TOOL_STARTED, Actor.system(), payload, subject_id=subject)
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


def _control_plane_answer(context, decision, *, approver) -> None:
    """Answer a pending tool review through `Gate.request_approval`, the control plane's writer.

    That path appends a serialised `Approval` -- the decision under `policy_decision_id`, and no
    `automatic` key at all -- plus a second `human.approval_requested` carrying no ticket. Both
    readers have to survive it: the actor is the only tell, and the later ticketless request may
    not unbind the call the first one named.
    """
    gate = Gate(context.gate.engine, context.event_log, approver=approver, human=REVIEWER)
    authorization = AuthorizationContext(
        id=new_id("authorization"),
        run_id=context.run_id,
        intent_id=new_id("intent"),
        policy_decision_id=decision.id,
        policy_sha256=context.gate.engine.policy_sha256,
        decision=AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
        subject_kind="tool_call",
        subject_id=decision.subject_id,
        action_kind=ActionKind.EXECUTE_TOOL,
        requires_approval=True,
    )
    gate.request_approval(authorization, decision, summary="echo")


def _control_plane_reviewed_call(tmp_path: Path, *, approver):
    """One review-gated manager call answered through the control-plane approval writer."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _control_plane_answer(context, first.pending_approval, approver=approver)
    return context, manager, first.pending_approval


def test_a3_and_a6_accept_a_retry_approved_through_the_control_plane(tmp_path: Path) -> None:
    """The other writer of human answers authorises the retry, and MIRA agrees with the manager."""
    context, manager, _ = _control_plane_reviewed_call(tmp_path, approver=_human_approves)
    answer = [e for e in context.event_log.events() if e.type is EventType.HUMAN_APPROVAL][-1]
    assert answer.actor == REVIEWER
    assert "automatic" not in answer.payload  # a serialised Approval carries no such key
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED

    a3, a6 = _audited(context)

    assert (a3.status, a3.detail) == (ControlStatus.PASSED, "1 executions authorised")
    assert a6.status is ControlStatus.PASSED


def test_a3_and_a6_refuse_an_automatic_control_plane_answer(tmp_path: Path) -> None:
    """`approver=auto_approve` answers under the automation actor: nothing is authorised."""
    context, manager, _ = _control_plane_reviewed_call(tmp_path, approver=auto_approve)
    answer = [e for e in context.event_log.events() if e.type is EventType.HUMAN_APPROVAL][-1]
    assert answer.actor.kind is ActorKind.SYSTEM
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.DENIED

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.NOT_APPLICABLE
    assert a6.status is ControlStatus.PASSED


def test_a3_reports_a_start_claiming_an_automatic_control_plane_answer(tmp_path: Path) -> None:
    """The same trace with one forged execution: the automatic answer buys it nothing."""
    context, _, decision = _control_plane_reviewed_call(tmp_path, approver=auto_approve)
    forged = _forged_start(context, decision.id)

    a3, _ = _audited(context)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


def test_a6_explains_a_denial_of_a_call_a_human_approved_and_then_refused(tmp_path: Path) -> None:
    """A human refusal is final for the exact call, whatever a sibling answer had allowed.

    Two answers on one review leave the manager denying the next identical call as "rejected by a
    human" under a decision the approval had already retired from A6's non-allowing set. The
    refusal still explains it, because the manager's unit is the ticket, not the decision id.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _answer(context, first.pending_approval.id, approved=True)
    _answer(context, first.pending_approval.id, approved=False)
    denied = manager.execute(context, "echo", {"value": "x"})
    assert denied.call.status is ToolCallStatus.DENIED
    assert denied.call.error is not None
    assert "rejected by a human" in denied.call.error

    a3, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "2 denials explained")
    assert a3.status is ControlStatus.NOT_APPLICABLE


SUBSTITUTE_TICKET = tool_intent_sha256("run_python", RUN_PYTHON_ARGUMENTS)
"""A `tool_intent_sha256` no human was ever shown: the digest of the substituted call."""


def _forged_request(context, decision_id: str, *, tool: str, ticket: str):
    """Append a second `human.approval_requested` re-pointing a decision at another call."""
    return context.event_log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {
            "decision_id": decision_id,
            "tool": tool,
            "arguments": RUN_PYTHON_ARGUMENTS,
            "tool_intent_sha256": ticket,
        },
        subject_id=new_id("tool"),
    )


def _completed_start(context, payload: dict[str, object], *, subject: str):
    """Append a `tool.started`/`tool.completed` pair on `subject` and return the start."""
    started = context.event_log.append(
        EventType.TOOL_STARTED, Actor.system(), payload, subject_id=subject
    )
    context.event_log.append(
        EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject
    )
    return started


def test_a3_refuses_a_start_under_an_answer_a_second_request_re_pointed(tmp_path: Path) -> None:
    """A human's answer stays bound to the call it was given for, whoever asks again.

    A second `human.approval_requested` naming the reviewed decision with another call's ticket
    is evidence of nothing -- the Gate writes one per decision -- and reading it as a rebinding
    would authorise a `run_python` under a human's yes about `echo`. The manager refuses the
    substituted call for the same reason, so this shape only ever reaches the log as a forged
    execution; the legitimate retry beside it stays authorised.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    reviewed = manager.execute(context, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    _forged_request(
        context, reviewed.pending_approval.id, tool="run_python", ticket=SUBSTITUTE_TICKET
    )
    _answer(context, reviewed.pending_approval.id, approved=True)
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED
    forged = _completed_start(
        context,
        {
            "tool": "run_python",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "decision_id": reviewed.pending_approval.id,
            "tool_intent_sha256": SUBSTITUTE_TICKET,
        },
        subject=new_id("tool"),
    )

    a3, a6 = _audited(context)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"
    assert a6.status is ControlStatus.PASSED


@pytest.mark.parametrize(
    ("tool", "expected"),
    [("run_python", ControlStatus.FAILED), ("echo", ControlStatus.PASSED)],
    ids=["substituted", "the-reviewed-tool"],
)
def test_a3_checks_the_requested_tool_on_the_reviewed_subject_too(
    tmp_path: Path, tool: str, expected: ControlStatus
) -> None:
    """The tool a human was asked about is checked on the start's own subject as well.

    Only the cross-subject claim compared it, so a start stamped on the reviewed call's *own*
    subject could keep the approved ticket and swap the tool.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    reviewed = manager.execute(context, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    request = next(
        e for e in context.event_log.events() if e.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    _answer(context, reviewed.pending_approval.id, approved=True)
    started = _completed_start(
        context,
        {
            "tool": tool,
            "arguments": {"value": "x"},
            "decision_id": reviewed.pending_approval.id,
            "tool_intent_sha256": request.payload["tool_intent_sha256"],
            # The scope the manager records when it spends this credit: without it the start
            # would be refused for having left its approval scope unattributed, which is a
            # different refusal from the tool substitution this node is about.
            "approval_scope_sha256": request.payload["approval_scope_sha256"],
            "delegation_depth": request.payload["delegation_depth"],
        },
        subject=reviewed.call.id,  # the subject the review was recorded for
    )

    a3, _ = _audited(context)

    assert a3.status is expected
    if expected is ControlStatus.FAILED:
        assert a3.detail == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_spends_a_passing_decision_on_one_execution() -> None:
    """A `PASS` is about one call: the manager asks the Gate once per `ToolCall` id.

    Returning True without counting let one PASS authorise unlimited starts on its subject, of
    any tool -- the same replay the review path already refused.
    """
    log, engine, gate = _laundering_run()
    subject = new_id("tool")
    passing = gate.check_capability(
        subject_id=subject,
        capability=ToolCapability(id="read_file", external_effects=()),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0),
        summary="read_file",
    )
    assert passing.decision is Decision.PASS
    log.append(EventType.TOOL_STARTED, Actor.system(), {"tool": "read_file"}, subject_id=subject)
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject)
    replay = log.append(
        EventType.TOOL_STARTED, Actor.system(), {"tool": "run_python"}, subject_id=subject
    )
    log.append(EventType.TOOL_COMPLETED, Actor.system(), {"exit_code": 0}, subject_id=subject)
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    a3, _ = _a3_a6(log, engine)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[replay.seq]}"


def test_a3_refuses_a_start_under_a_decision_id_the_log_records_twice(tmp_path: Path) -> None:
    """A forged `policy.decision` re-using a recorded id shadows it, and neither one authorises.

    Ids are unique by construction, so a second decision under one id means the log can no longer
    say which decision a start named -- and the forger chose the answer it would find.
    """
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    manager = _echo_manager()
    reviewed = manager.execute(context, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    recorded = next(e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION)
    victim = new_id("tool")
    context.event_log.append(
        EventType.POLICY_DECISION,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {**recorded.payload, "decision": Decision.PASS.value},
        subject_id=victim,
    )
    forged = _completed_start(
        context,
        {"tool": "run_python", "decision_id": reviewed.pending_approval.id},
        subject=victim,
    )

    a3, _ = _audited(context)

    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


def test_a6_explains_the_allowlist_refusal_the_manager_records_with_no_decision(
    tmp_path: Path,
) -> None:
    """A tool outside the agent's allowlist is refused before the Gate is ever asked.

    There is no decision to point at -- `decision_id` is `None` -- so the refusal itself has to
    explain the denial, and only the manager's own allowlist sentence does.
    """
    context = replace(
        review_gated_context(tmp_path, run_id=new_id("run")),
        allowed_tools=frozenset({"other"}),
    )
    denied = ToolManager(
        ToolRegistry((FakeTool("echo", ToolCapability(id="echo", external_effects=())),))
    ).execute(context, "echo", {"value": "x"})
    assert denied.call.status is ToolCallStatus.DENIED
    denial = next(e for e in context.event_log.events() if e.type is EventType.TOOL_DENIED)
    assert denial.payload["decision_id"] is None
    assert "is not allowed to call tool" in denial.payload["reason"]

    a3, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")
    assert a3.status is ControlStatus.NOT_APPLICABLE


def test_a6_explains_an_allowlist_denial_of_a_tool_named_like_a_pem_banner(tmp_path: Path) -> None:
    """A tool name that looks like a secret remains exact on the canonical chain.

    API/export/trace projections apply redaction later, while A6 recomputes the allowlist refusal
    from the exact canonical payload and the event actor.
    """
    # Assembled at run time so the repository's private-key hook does not read a fixture as a
    # leaked key; the value is exactly the PEM banner the redaction recognises.
    tool_name = "-----BEGIN " + "PRIVATE KEY-----"
    capability = ToolCapability(id=tool_name, external_effects=())
    context = replace(
        _manager_context(tmp_path, capability=capability),
        allowed_tools=frozenset({"other"}),
    )
    manager = ToolManager(ToolRegistry((_RegisteredFakeTool(tool_name, capability),)))

    denied = manager.execute(context, tool_name, {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    denial = next(e for e in context.event_log.events() if e.type is EventType.TOOL_DENIED)
    assert denial.payload["tool"] == tool_name
    assert tool_name in denial.payload["reason"]

    _, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")


def test_a6_explains_a_denial_naming_a_blocking_run_level_decision() -> None:
    """The run-level rule is about the subject kind, not the verdict: a BLOCK explains too."""
    log, engine, gate = _laundering_run()
    run_level = gate.check_action(subject_kind="run", subject_id="run", action_type="deploy_model")
    assert run_level.decision is Decision.BLOCK
    subject = new_id("tool")
    log.append(
        EventType.TOOL_DENIED,
        Actor.system(),
        {"tool_call_id": subject, "decision_id": run_level.id, "reason": "blocked"},
        subject_id=subject,
    )
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    _, a6 = _a3_a6(log, engine)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")


def _constrained_engine(constraints: ExecutionConstraints) -> PolicyEngine:
    """A policy that lets a run start and narrows its execution surface with `constraints`.

    The point is that `decide_execution` *derives* the constraints from an applicable capability
    rule and records them on the run-level decision it returns, so the `policy.decision` event the
    log carries is the one the manager then enforces -- no `model_copy` behind the log's back.
    """
    return PolicyEngine(
        Policy(
            name="constrained-execution",
            version="1.0",
            action_rules=(
                ActionRule(
                    id="ALLOW-START",
                    action_types=("execution.start",),
                    decision=Decision.PASS,
                    reason="execution may start",
                ),
            ),
            capability_rules=(
                CapabilityRule(
                    id="LOCAL-TOOLS",
                    decision=Decision.PASS,
                    reason="local tools are allowed under the recorded constraints",
                    external_effects=(),
                    execution_constraints=constraints,
                ),
            ),
        )
    )


def _constrained_context(
    tmp_path: Path,
    constraints: ExecutionConstraints,
    *,
    log: EventLog | None = None,
    run_id: str | None = None,
):
    """A tool context whose recorded run-level decision really carries `constraints`.

    The risk profile is confident, so a call the constraints allow outright is authorised without
    review -- what most callers are testing is the constraint refusal, not the review path. A
    caller passing `requires_human_review=True` gets the opposite on purpose: the run-level
    decision itself is `REQUIRE_HUMAN_REVIEW`, and the recorded constraints are the same
    non-allowing decision's, not a PASS one a later call's constraints narrow.

    `log` and `run_id` let a caller put this context on a Run whose lifecycle a real
    `RunController` is driving, so the transitions and the Gate facts share one authoritative log.
    """
    context = replace(
        review_gated_context(
            tmp_path,
            run_id=run_id or new_id("run"),
            log=log,
            engine=_constrained_engine(constraints),
        ),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    capability = ToolCapability(id="echo", external_effects=())
    execution = context.gate.authorize_execution(context.risk_profile, (capability,))
    expected = Decision.REQUIRE_HUMAN_REVIEW if constraints.requires_human_review else Decision.PASS
    assert execution.decision is expected
    assert execution.execution_constraints == constraints
    return (
        replace(
            context,
            execution_constraints=execution.execution_constraints,
            execution_decision=execution,
        ),
        execution,
    )


def _denials(context) -> list[Event]:
    """Every `tool.denied` the log holds, in order."""
    return [e for e in context.event_log.events() if e.type is EventType.TOOL_DENIED]


def test_a6_explains_a_denial_the_recorded_execution_constraints_produced(tmp_path: Path) -> None:
    """A call the Run's constraints prohibit is denied under the decision that records them.

    The run-level decision *allows* the run to start, so its verdict explains nothing: what
    explains the denial is that these recorded `ExecutionConstraints` produce exactly this
    refusal, recomputed with `ExecutionConstraints.denies_tool`.
    """
    context, execution = _constrained_context(
        tmp_path, ExecutionConstraints(prohibited_tools=("echo",))
    )
    denied = _echo_manager().execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    denial = _denials(context)[0]
    assert denial.payload["decision_id"] == execution.id
    assert denial.payload["reason"] == "tool 'echo' is prohibited by the execution constraints"

    _, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")


def test_a6_explains_a_denial_the_recorded_call_ceiling_produced(tmp_path: Path) -> None:
    """The same rule covers a constraint that is about the Run rather than about one tool."""
    context, execution = _constrained_context(tmp_path, ExecutionConstraints(max_tool_calls=1))
    manager = _echo_manager()
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED
    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    denial = _denials(context)[0]
    assert denial.payload["decision_id"] == execution.id
    assert (
        denial.payload["reason"] == "execution constraints reached the maximum number of tool calls"
    )

    a3, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")
    assert a3.status is ControlStatus.PASSED


@pytest.mark.parametrize(
    ("constraints", "tool_name", "effects"),
    [
        (ExecutionConstraints(max_tool_calls=0), "echo", ()),
        (ExecutionConstraints(required_evidence=("approval",)), "echo", ()),
        (ExecutionConstraints(allowed_tools=("other",)), "echo", ()),
        (ExecutionConstraints(prohibited_tools=("ec'ho",)), "ec'ho", ()),
        (ExecutionConstraints(prohibited_tools=("ec\\ho",)), "ec\\ho", ()),
        (ExecutionConstraints(local_execution_only=True), "echo", ("external",)),
    ],
)
def test_a6_explains_every_runtime_constraint_refusal(
    tmp_path: Path, constraints: ExecutionConstraints, tool_name: str, effects: tuple[str, ...]
) -> None:
    """Every row here records a PASS run-level decision, so A6 recomputes the refusal.

    `requires_human_review=True` is deliberately not one of these rows, and no longer could be:
    the manager stopped refusing calls outright under it, so nothing here produces the denial this
    parametrization asserts. It is a *review* the Gate now inherits onto each call
    (`_constraint_error`), and the legacy denial that quoted it is pinned by
    `test_a6_explains_a_denial_the_run_level_decision_itself_requires_review` on hand-authored
    events instead.
    """
    context, execution = _constrained_context(tmp_path, constraints)
    capability = ToolCapability(id=tool_name, external_effects=effects)
    manager = ToolManager(
        ToolRegistry((FakeTool(tool_name, capability, arguments_model=EchoArguments),))
    )

    denied = manager.execute(context, tool_name, {"value": "x"})
    assert denied.call.status is ToolCallStatus.DENIED
    assert _denials(context)[0].payload["decision_id"] == execution.id
    assert denied.call.error in constraint_refusals(constraints, tool_name)
    _, a6 = _audited(context)

    assert a6.status is ControlStatus.PASSED, a6.detail


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "approved"])
def test_a6_explains_a_denial_the_run_level_decision_itself_requires_review(
    tmp_path: Path, answered: bool
) -> None:
    """The legacy blanket refusal stays explained, before and after a human approves the Run.

    Until the Gate inherited the Run's constraints onto each call, the manager refused *every*
    tool call under `requires_human_review=True` with one sentence naming the run-level decision
    (`HUMAN_REVIEW_REFUSAL`), so a log written by that manager holds this shape and A6 still has
    to read it. No manager writes it now, which is why it is hand-authored here.

    Both halves matter. Before an answer, the run-level decision is `REQUIRE_HUMAN_REVIEW` and
    A6's run-level rule explains the denial through that verdict alone
    (`decision_id in self.non_allowing`). After the human approves it, the review is retired and
    the verdict explains nothing -- what still explains the denial is that these *recorded*
    `ExecutionConstraints` produce exactly this reason, recomputed with `constraint_refusals`.
    """
    constraints = ExecutionConstraints(requires_human_review=True)
    context, execution = _constrained_context(tmp_path, constraints)
    assert execution.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert HUMAN_REVIEW_REFUSAL in constraint_refusals(constraints, "echo")
    denial = context.event_log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.TOOL, id="echo"),
        {
            "tool_call_id": new_id("tool"),
            "decision_id": execution.id,
            "reason": HUMAN_REVIEW_REFUSAL,
            "tool": "echo",
            "agent_id": context.agent_id,
        },
        subject_id=new_id("tool"),
    )
    if answered:
        _answer(context, execution.id, approved=True)

    _, a6 = _audited(context)

    assert _denials(context) == [denial]
    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")


def test_a6_keeps_explaining_the_previous_allowlist_event_shape(tmp_path: Path) -> None:
    context = replace(
        review_gated_context(tmp_path, run_id=new_id("run")), allowed_tools=frozenset()
    )
    denied = _echo_manager().execute(context, "echo", {"value": "x"})
    recorded = _denials(context)[0]
    legacy = InMemoryEventLog(context.run_id)
    legacy.append(
        EventType.TOOL_DENIED,
        recorded.actor,
        {"tool_call_id": denied.call.id, "decision_id": None, "reason": denied.call.error},
        subject_id=denied.call.id,
    )

    _, a6 = _a3_a6(legacy, context.gate.engine)

    assert a6.status is ControlStatus.PASSED, a6.detail


def test_a6_reports_a_denial_an_allowing_run_level_decision_does_not_refuse(
    tmp_path: Path,
) -> None:
    """Naming the run's own decision is not an explanation: it has to have refused this call.

    The run-level `execution.start` PASS authorises the Run; a denial quoting its id with any
    other reason is a refusal the log does not record.
    """
    context, execution = _constrained_context(
        tmp_path, ExecutionConstraints(prohibited_tools=("echo",))
    )
    victim = new_id("tool")
    forged = context.event_log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {
            "tool_call_id": victim,
            "decision_id": execution.id,
            "reason": "totally unrelated: the model felt like it",
        },
        subject_id=victim,
    )

    _, a6 = _audited(context)

    assert a6.status is ControlStatus.FAILED
    assert a6.detail == f"tool.denied without a denying decision at seq {[forged.seq]}"


def test_a6_reports_a_denial_from_a_non_tool_actor_that_names_no_tool(tmp_path: Path) -> None:
    """The run-level constraint check never borrows a non-``TOOL`` actor's id as a tool name.

    ``MISSING_EVIDENCE_REFUSAL`` does not depend on which tool was denied, so without the
    ``ActorKind.TOOL`` guard on the ``event.actor.id`` fallback, a forged denial from an agent
    actor -- carrying no ``tool`` key at all -- could still borrow its own actor id as a stand-in
    tool name and have the constraint recomputation match anyway. Guarded, there is no tool name
    to recompute with, so the denial stays unexplained, exactly like any other forgery.
    """
    context, execution = _constrained_context(
        tmp_path, ExecutionConstraints(required_evidence=("approval",))
    )
    victim = new_id("tool")
    forged = context.event_log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {"tool_call_id": victim, "decision_id": execution.id, "reason": MISSING_EVIDENCE_REFUSAL},
        subject_id=victim,
    )

    _, a6 = _audited(context)

    assert a6.status is ControlStatus.FAILED
    assert a6.detail == f"tool.denied without a denying decision at seq {[forged.seq]}"


def test_a6_reports_a_denial_naming_a_passing_budget_decision(tmp_path: Path) -> None:
    """A run-scope budget decision that crossed no ceiling refuses nothing either."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    budget = context.gate.check_budget({"cost_usd": 1.0, "tokens": 1, "tool_calls": 1})
    assert budget.decision is Decision.PASS
    victim = new_id("tool")
    forged = context.event_log.append(
        EventType.TOOL_DENIED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {"tool_call_id": victim, "decision_id": budget.id, "reason": "fabricated"},
        subject_id=victim,
    )

    _, a6 = _audited(context)

    assert a6.status is ControlStatus.FAILED
    assert a6.detail == f"tool.denied without a denying decision at seq {[forged.seq]}"


def test_a6_reports_a_denial_explained_by_a_forged_run_level_shadow() -> None:
    """A forged run-level decision re-using a review's id explains nothing for it.

    Flipping `subject_kind` to "run" on a copy of a recorded review used to turn a denial the
    log does not explain into an explained one. A duplicated decision id now qualifies for
    nothing, on this side as on A3's.
    """
    log, engine, gate = _laundering_run()
    subject = new_id("tool")
    review = _review_gated_call(gate, subject, tool="echo")
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": review.id, "approved": True, "automatic": False},
        subject_id=subject,
    )
    recorded = next(
        e
        for e in log.events()
        if e.type is EventType.POLICY_DECISION and e.payload.get("id") == review.id
    )
    log.append(
        EventType.POLICY_DECISION,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        {**recorded.payload, "subject_kind": "run"},
        subject_id=log.run_id,
    )
    denial = log.append(
        EventType.TOOL_DENIED,
        Actor.system(),
        {
            "tool_call_id": subject,
            "decision_id": review.id,
            "reason": "tool call denied: needs human approval",
            "tool": "echo",
            "arguments": RUN_PYTHON_ARGUMENTS,
            "tool_intent_sha256": tool_intent_sha256("echo", RUN_PYTHON_ARGUMENTS),
        },
        subject_id=subject,
    )
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})

    _, a6 = _a3_a6(log, engine)

    assert a6.status is ControlStatus.FAILED
    assert a6.detail == f"tool.denied without a denying decision at seq {[denial.seq]}"


def _budget_guard(context, usage: dict[str, float | int | None]):
    """The Core guard's shape: refuse under the budget decision the Gate just recorded.

    Mirrors `thymira.core.graph.adapters._tool_budget_guard`, which MIRA and the Tool Manager
    both stay below in the layer order: a WARNING is allowed through, and anything stricter
    refuses the call and names the decision it was refused under.
    """

    def guard() -> BudgetRefusal | None:
        decision = context.gate.check_budget(usage, summary="tool budget")
        if decision.decision in (Decision.PASS, Decision.WARNING):
            return None
        return BudgetRefusal(
            reason=f"tool call blocked by budget decision {decision.id}: {decision.reason}",
            decision_id=decision.id,
        )

    return guard


def test_a6_explains_the_budget_denial_under_the_decision_that_refused_it(tmp_path: Path) -> None:
    """A budget-guarded call names the run-level budget decision that stopped it.

    The denial used to name the capability decision that had just *allowed* the call, so nothing
    in the log refused it and A6 reported a legitimate trace. The guard now hands the manager the
    decision it took, and that decision does not allow execution.
    """
    engine = PolicyEngine(
        Policy(
            name="tool-budget",
            version="1.0",
            capability_rules=(
                CapabilityRule(
                    id="ALLOW-ECHO",
                    decision=Decision.PASS,
                    reason="allow the local echo tool",
                    external_effects=(),
                ),
            ),
            budget_rules=(
                BudgetRule(
                    id="HARD-CALLS",
                    decision=Decision.BLOCK,
                    reason="tool-call budget exhausted",
                    max_tool_calls=1,
                ),
            ),
        )
    )
    context = replace(
        review_gated_context(tmp_path, run_id=new_id("run"), engine=engine),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    context = replace(
        context,
        budget_guard=_budget_guard(context, {"cost_usd": 0.0, "tokens": 0, "tool_calls": 2}),
    )
    denied = _echo_manager().execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    budget = next(
        e
        for e in context.event_log.events()
        if e.type is EventType.POLICY_DECISION and e.payload["decision"] == Decision.BLOCK.value
    )
    denial = _denials(context)[0]
    assert denial.payload["decision_id"] == budget.payload["id"]
    assert denial.payload["reason"].startswith("tool call blocked by budget decision ")

    a3, a6 = _audited(context)

    assert (a6.status, a6.detail) == (ControlStatus.PASSED, "1 denials explained")
    assert a3.status is ControlStatus.NOT_APPLICABLE


# ------------------- A3: the Run's own review requirement, its scope and its ticket pool


REVIEW_EVERY_CALL = ExecutionConstraints(requires_human_review=True)
"""The run-level constraint that puts a human in front of every THY tool call."""


def _a3_now(context):
    """A3 over the log as it stands, without closing the Run first."""
    return a3_authorization_before_tool(
        AuditContext(context.run_id, context.event_log.events(), None)
    )


def _a6_now(context):
    """A6 over the log as it stands, without closing the Run first."""
    return a6_denials_recorded(AuditContext(context.run_id, context.event_log.events(), None))


def _reviewed_call_under_the_flag(tmp_path: Path):
    """One THY tool call under the flag: pending review, human yes, and the retry that ran.

    The manager no longer refuses every call outright under `requires_human_review=True`: the Gate
    inherits the Run's constraints onto this call, so the call gets its own ticketed review, its
    own answer, and runs once.
    """
    context, execution = _constrained_context(tmp_path, REVIEW_EVERY_CALL)
    manager = _echo_manager()
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.call.status is ToolCallStatus.DENIED
    assert first.pending_approval is not None
    assert first.pending_approval.id != execution.id  # its own review, not the Run's
    assert first.pending_approval.execution_constraints.requires_human_review
    return context, manager, first.pending_approval, execution


def test_a3_and_a6_accept_one_ticketed_call_under_the_run_wide_review(tmp_path: Path) -> None:
    """The whole trace the flag now produces: pending denial, human answer, one execution."""
    context, manager, review, _ = _reviewed_call_under_the_flag(tmp_path)
    assert _a6_now(context)[0] is ControlStatus.PASSED  # the pending review explains its denial
    _answer(context, review.id, approved=True)

    retry = manager.execute(context, "echo", {"value": "x"})

    assert retry.call.status is ToolCallStatus.COMPLETED
    a3, a6 = _audited(context)
    assert (a3.status, a3.detail) == (ControlStatus.PASSED, "1 executions authorised")
    assert a6.status is ControlStatus.PASSED


def test_a3_reports_a_second_start_reusing_the_ticket_spent_under_the_run_wide_review(
    tmp_path: Path,
) -> None:
    """One human answer still buys one execution; the flag does not turn a ticket into a licence."""
    context, manager, review, _ = _reviewed_call_under_the_flag(tmp_path)
    _answer(context, review.id, approved=True)
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED

    replay = _forged_start(context, review.id)

    a3, _ = _audited(context)
    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[replay.seq]}"


def test_a3_does_not_authorise_a_start_naming_the_approved_execution_start_decision(
    tmp_path: Path,
) -> None:
    """Approving the Run's own start authorises THY, never one tool call.

    The start review carries no ticket -- the human was shown a Run, not an effect -- so a start
    quoting its id is claiming an authorization nobody granted, however exactly the effect it
    names matches the call a human was later asked about.
    """
    context, _, _, execution = _reviewed_call_under_the_flag(tmp_path)
    _answer(context, execution.id, approved=True)

    forged = _forged_start(context, execution.id)

    a3, _ = _audited(context)
    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[forged.seq]}"


def _permissive_call(log, *, tool: str = "echo") -> Event:
    """A hand-authored PASS tool-call decision and the execution recorded on its subject."""
    subject = new_id("tool")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": new_id("decision"),
            "decision": Decision.PASS.value,
            "subject_kind": "tool_call",
            "policy_sha256": "0" * 64,
            "execution_constraints": ExecutionConstraints().to_json_dict(),
        },
        subject_id=subject,
    )
    return log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {"tool": tool, "arguments": {"value": "x"}},
        subject_id=subject,
    )


def test_a3_reports_a_permissive_call_decision_taken_under_the_run_wide_review(
    tmp_path: Path,
) -> None:
    """A `PASS` recorded while the Run demands a human is a decision the engine would never take.

    Under the flag the Policy Engine escalates every capability outcome it would otherwise pass,
    so a permissive tool-call decision on THY's surface is either a forgery or a decision taken
    against constraints the log does not record. Either way it authorises nothing.
    """
    context, _ = _constrained_context(tmp_path, REVIEW_EVERY_CALL)

    started = _permissive_call(context.event_log)

    a3, _ = _audited(context)
    assert a3.status is ControlStatus.FAILED
    assert a3.detail == f"tool.started without an allowing decision at seq {[started.seq]}"


THY_CAPABILITY = ToolCapability(id="echo", risk_tags=("workspace",), external_effects=())
"""THY's workspace tool: the capability whose rule puts the review on the Run's whole surface."""

MIRA_CAPABILITY = ToolCapability(id="read_evidence", external_effects=())
"""MIRA's evidence read: matched only by the unconstrained rule, so its own decision is a PASS."""


def _scoped_engine() -> PolicyEngine:
    """A policy that reviews THY's tool surface and leaves MIRA's evidence reads passing.

    Both halves of the scope rule need one policy, because the runtime uses one: the run-level
    decision inherits `requires_human_review` from the rule matching THY's workspace tools, while
    MIRA's read matches only the unconstrained rule and is decided on its own merits -- the shape
    `MiraSubgraph` produces once Core has handed it the Run.
    """
    return PolicyEngine(
        Policy(
            name="scoped-execution",
            version="1.0",
            action_rules=(
                ActionRule(
                    id="ALLOW-START",
                    action_types=("execution.start",),
                    decision=Decision.PASS,
                    reason="execution may start",
                ),
            ),
            capability_rules=(
                CapabilityRule(
                    id="THY-TOOLS",
                    decision=Decision.PASS,
                    reason="THY's workspace tools run under a human review",
                    risk_tags=("workspace",),
                    external_effects=(),
                    execution_constraints=REVIEW_EVERY_CALL,
                ),
                CapabilityRule(
                    id="LOCAL-READS",
                    decision=Decision.PASS,
                    reason="a local read is allowed",
                    external_effects=(),
                ),
            ),
        )
    )


def _scoped_manager() -> ToolManager:
    """One manager holding THY's reviewed tool and MIRA's unconstrained read tool."""
    return ToolManager(
        ToolRegistry(
            (
                FakeTool("echo", THY_CAPABILITY, arguments_model=EchoArguments),
                FakeTool("read_evidence", MIRA_CAPABILITY, arguments_model=EchoArguments),
            )
        )
    )


def _executing_run(tmp_path: Path):
    """A real Run on `LocalRunStore`, advanced to executing, under a run-wide review requirement.

    The lifecycle is driven by `RunController` and the Gate facts go to the same authoritative
    log, so the `run.transitioned` events A3 reads are the ones Core really writes -- envelope,
    flattened fields, serialised state and all.
    """
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="scope",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    log = RunEventLog(store, run.id)
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    context = replace(
        review_gated_context(tmp_path, run_id=run.id, log=log, engine=_scoped_engine()),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = context.gate.authorize_execution(context.risk_profile, (THY_CAPABILITY,))
    assert execution.execution_constraints.requires_human_review
    thy = replace(
        context,
        execution_constraints=execution.execution_constraints,
        execution_decision=execution,
    )
    controller.advance(run.id, RunTransitionKind.BEGIN_EXECUTION)
    return run, controller, log, thy


def test_a3_suspends_the_run_wide_review_while_core_says_mira_holds_the_tools(
    tmp_path: Path,
) -> None:
    """MIRA's own reads after a genuine `begin_audit` are authorised on their own PASS decisions.

    THY's surface required a human for every call, and its one call got one. MIRA then calls the
    same Tool Manager under a context carrying no execution constraints and no execution decision
    at all, and the Policy Engine passes its read outright. Reading THY's requirement as still
    binding there would report every correct, independently authorised audit read as unauthorised.
    """
    run, controller, log, thy = _executing_run(tmp_path)
    manager = _scoped_manager()
    reviewed = manager.execute(thy, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    _answer(thy, reviewed.pending_approval.id, approved=True)
    assert manager.execute(thy, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED
    controller.advance(run.id, RunTransitionKind.BEGIN_AUDIT)
    mira = replace(
        thy,
        agent_id=new_id("agent"),
        execution_constraints=ExecutionConstraints(),
        execution_decision=None,
    )

    read = manager.execute(mira, "read_evidence", {"value": "y"})

    assert read.call.status is ToolCallStatus.COMPLETED
    audit_decision = next(
        event
        for event in reversed(log.events())
        if event.type is EventType.POLICY_DECISION and event.payload["subject_kind"] == "tool_call"
    )
    assert audit_decision.payload["decision"] == Decision.PASS.value
    a3 = a3_authorization_before_tool(AuditContext(run.id, log.events()))
    assert (a3[0], a3[1]) == (ControlStatus.PASSED, "2 executions authorised")


def test_a3_re_arms_the_run_wide_review_when_core_reopens_the_run_for_rework(
    tmp_path: Path,
) -> None:
    """A rework puts the tools back on THY's surface, so the Run's requirement binds again."""
    run, controller, log, thy = _executing_run(tmp_path)
    manager = _scoped_manager()
    reviewed = manager.execute(thy, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    _answer(thy, reviewed.pending_approval.id, approved=True)
    assert manager.execute(thy, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED
    controller.advance(run.id, RunTransitionKind.BEGIN_AUDIT)
    controller.advance(run.id, RunTransitionKind.REOPEN)

    started = _permissive_call(log)

    a3 = a3_authorization_before_tool(AuditContext(run.id, log.events()))
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def _armed_log() -> InMemoryEventLog:
    """A log whose run-level decision demands a human before every tool call."""
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": new_id("decision"),
            "decision": Decision.PASS.value,
            "subject_kind": "run",
            "policy_sha256": "0" * 64,
            "execution_constraints": REVIEW_EVERY_CALL.to_json_dict(),
        },
    )
    return log


def _begin_audit_payload(**overrides: object) -> dict[str, object]:
    """The payload `RunController` writes for `begin_audit`, with `overrides` folded in.

    Authored to the letter of `RunTransitionEvent.payload` plus the serialised `RunState` Core
    stores beside it, so every row below differs from a genuine transition in exactly one field.
    """
    payload: dict[str, object] = {
        "command": "begin_audit",
        "from_version": 4,
        "to_version": 5,
        "previous_stage": RunStage.EXECUTING.value,
        "previous_condition": RunCondition.ACTIVE.value,
        "previous_wait_reason": None,
        "previous_outcome": None,
        "stage": RunStage.AUDITING.value,
        "condition": RunCondition.ACTIVE.value,
        "wait_reason": None,
        "outcome": None,
        "state": {
            "stage": RunStage.AUDITING.value,
            "condition": RunCondition.ACTIVE.value,
            "wait_reason": None,
            "outcome": None,
            "version": 5,
        },
    }
    payload.update(overrides)
    return payload


def _transition(log: InMemoryEventLog, payload: dict[str, object], **envelope) -> None:
    """Append one `run.transitioned` with Core's own envelope, bar what `envelope` replaces."""
    log.append(
        EventType.RUN_TRANSITIONED,
        envelope.pop("actor", Actor.system()),
        payload,
        subject_id=envelope.pop("subject_id", log.run_id),
        producer=envelope.pop("producer", "thymira.core"),
        producer_version="0.1",
    )


def test_a3_lets_a_genuine_begin_audit_envelope_release_the_run_wide_review() -> None:
    """The positive control for the forged rows below: this exact record is trusted."""
    log = _armed_log()
    _transition(log, _begin_audit_payload())

    started = _permissive_call(log)

    a3 = a3_authorization_before_tool(AuditContext(log.run_id, log.events()))
    assert (a3[0], a3[1]) == (ControlStatus.PASSED, "1 executions authorised"), started.seq


@pytest.mark.parametrize(
    ("payload", "envelope"),
    [
        pytest.param({}, {"actor": Actor(kind=ActorKind.TOOL, id="echo")}, id="tool-actor"),
        pytest.param({}, {"actor": Actor(kind=ActorKind.AGENT, id=new_id("agent"))}, id="agent"),
        pytest.param({}, {"actor": REVIEWER}, id="human-actor"),
        pytest.param({}, {"producer": "thymira.tools"}, id="other-producer"),
        pytest.param({}, {"subject_id": new_id("task")}, id="not-about-the-run"),
        pytest.param({"stage": RunStage.EXECUTING.value}, {}, id="flat-stage-executing"),
        pytest.param(
            {
                "stage": RunStage.EXECUTING.value,
                "state": {
                    "stage": RunStage.EXECUTING.value,
                    "condition": RunCondition.ACTIVE.value,
                    "wait_reason": None,
                    "outcome": None,
                    "version": 5,
                },
            },
            {},
            id="state-still-executing",
        ),
        pytest.param({"state": "not-a-state"}, {}, id="unparseable-state"),
        pytest.param(
            {
                "condition": RunCondition.WAITING.value,
                "wait_reason": "approval",
                "state": {
                    "stage": RunStage.AUDITING.value,
                    "condition": RunCondition.WAITING.value,
                    "wait_reason": "approval",
                    "outcome": None,
                    "version": 5,
                },
            },
            {},
            id="state-waiting",
        ),
        pytest.param(
            {
                "state": {
                    "stage": RunStage.AUDITING.value,
                    "condition": RunCondition.WAITING.value,
                    "wait_reason": "approval",
                    "outcome": None,
                    "version": 5,
                }
            },
            {},
            id="flat-active-state-waiting",
        ),
        pytest.param({"to_version": 9}, {}, id="version-not-the-states"),
        pytest.param({"to_version": None}, {}, id="no-version-at-all"),
        pytest.param({"from_version": 1}, {}, id="version-it-cannot-come-from"),
        pytest.param(
            {
                "from_version": False,
                "to_version": 1,
                "state": {
                    "stage": RunStage.AUDITING.value,
                    "condition": RunCondition.ACTIVE.value,
                    "wait_reason": None,
                    "outcome": None,
                    "version": 1,
                },
            },
            {},
            id="a-bool-that-compares-like-a-version",
        ),
        pytest.param({"command": "resume"}, {}, id="a-command-that-moves-nothing"),
    ],
)
def test_a3_keeps_the_run_wide_review_after_a_forged_lifecycle_boundary(
    payload: dict[str, object], envelope: dict[str, object]
) -> None:
    """Only Core's own coherent record hands the tools to MIRA; every near miss changes nothing.

    Each row is a genuine `begin_audit` with exactly one thing wrong -- a writer who is not the
    Run itself, a producer that is not Core, a subject that is not the Run, or a payload that
    contradicts the state it carries. A transition MIRA cannot fully verify says nothing about who
    holds the tools, so the Run's requirement still applies and the permissive start after it is
    still unauthorised.
    """
    log = _armed_log()
    _transition(log, _begin_audit_payload(**payload), **envelope)

    started = _permissive_call(log)

    a3 = a3_authorization_before_tool(AuditContext(log.run_id, log.events()))
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_does_not_read_a_preflight_audit_started_as_the_lifecycle_boundary() -> None:
    """MIRA's preflight writes `audit.started` before THY runs; it moves no boundary.

    It is written by `Actor.system()` like a real transition, so nothing but its type tells it
    apart -- and its type is exactly what says it is not the Run's own record of a stage change.
    """
    log = _armed_log()
    log.append(EventType.AUDIT_STARTED, Actor.system(), {"mode": "preflight"})

    started = _permissive_call(log)

    a3 = a3_authorization_before_tool(AuditContext(log.run_id, log.events()))
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


POOL_TOOL = "echo"
POOL_ARGUMENTS = {"value": "pooled"}
POOL_TICKET = tool_intent_sha256(POOL_TOOL, POOL_ARGUMENTS)
POOL_POLICY = "a" * 64
"""One recorded policy snapshot for every pooled decision below: only the constraints differ."""


def _pooled_review(log: InMemoryEventLog, constraints: ExecutionConstraints) -> str:
    """Record a ticketed review decision carrying `constraints`, and the request that names it."""
    decision_id = new_id("decision")
    subject = new_id("tool")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": decision_id,
            "decision": Decision.REQUIRE_HUMAN_REVIEW.value,
            "subject_kind": "tool_call",
            "policy_sha256": POOL_POLICY,
            "execution_constraints": constraints.to_json_dict(),
        },
        subject_id=subject,
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision_id,
            "tool": POOL_TOOL,
            "arguments": POOL_ARGUMENTS,
            "tool_intent_sha256": POOL_TICKET,
        },
        subject_id=subject,
    )
    return decision_id


def _pooled_answer(log: InMemoryEventLog, decision_id: str, *, approved: bool = True) -> None:
    """Record the human's own answer to one pooled review."""
    log.append(
        EventType.HUMAN_APPROVAL,
        REVIEWER,
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def _pooled_start(log: InMemoryEventLog, decision_id: str) -> Event:
    """Record one execution of the pooled effect, on its own subject, naming `decision_id`."""
    return log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": POOL_TOOL,
            "arguments": POOL_ARGUMENTS,
            "decision_id": decision_id,
            "tool_intent_sha256": POOL_TICKET,
        },
        subject_id=new_id("tool"),
    )


def _pooled_a3(log: InMemoryEventLog):
    """A3 over a hand-authored ticket-pool log."""
    return a3_authorization_before_tool(AuditContext(log.run_id, log.events()))


def _arm(log: InMemoryEventLog) -> None:
    """Record the run-level decision that puts a human in front of every later tool call.

    Appended part-way through these logs on purpose: that is the chronology the runtime produces
    -- a call reviewed and answered on its own, and only then a Run whose execution decision
    narrows every call after it.
    """
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": new_id("decision"),
            "decision": Decision.PASS.value,
            "subject_kind": "run",
            "policy_sha256": POOL_POLICY,
            "execution_constraints": REVIEW_EVERY_CALL.to_json_dict(),
        },
    )


def test_a3_reports_a_replay_paid_for_by_a_credit_from_another_surface() -> None:
    """Two answers to one effect are not two credits when they were given under different surfaces.

    The weaker review was answered before the Run inherited anything; the stronger one after. Both
    requests name the identical effect, so counting answers alone makes the second execution look
    bought and paid for. Pairing each answer with the record it was given under leaves exactly one
    credit standing in the surface the starts execute under.
    """
    log = InMemoryEventLog(new_id("run"))
    weaker = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, weaker)
    _arm(log)
    stronger = _pooled_review(log, REVIEW_EVERY_CALL)
    _pooled_answer(log, stronger)
    first = _pooled_start(log, stronger)

    replay = _pooled_start(log, stronger)

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[replay.seq]}", first.seq


def test_a3_does_not_let_an_older_execution_lock_out_a_fresh_stronger_approval() -> None:
    """An execution that spent what stood at the time costs the later, stronger answer nothing.

    Order is the whole difference from the replay above: this start ran while only the weaker
    credit existed and spent it, so the stronger approval that arrives afterwards is still unspent
    when its own execution runs.
    """
    log = InMemoryEventLog(new_id("run"))
    weaker = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, weaker)
    _pooled_start(log, weaker)
    _arm(log)
    stronger = _pooled_review(log, REVIEW_EVERY_CALL)
    _pooled_answer(log, stronger)

    _pooled_start(log, stronger)

    a3 = _pooled_a3(log)
    assert (a3[0], a3[1]) == (ControlStatus.PASSED, "2 executions authorised")


@pytest.mark.parametrize("names_the_weaker", [True, False], ids=["weaker", "never-recorded"])
def test_a3_charges_a_late_start_against_the_credit_it_forged_itself_onto(
    names_the_weaker: bool,
) -> None:
    """An execution recorded after the fresh approval spends it, whatever decision it points at.

    The manager counts `tool.started` events, not authorised ones, so an attacker cannot get a
    free execution by mislabelling it. Whether the extra start names the stale weaker decision or
    an id the log never recorded, the credit standing when it ran is gone and the honest execution
    that follows has nothing left to spend -- the *second* seq in the failure is the point, since
    `test_a3_does_not_let_an_older_execution_lock_out_a_fresh_stronger_approval` shows that same
    honest start passing when nothing consumed its credit first.
    """
    log = InMemoryEventLog(new_id("run"))
    weaker = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, weaker)
    _arm(log)
    stronger = _pooled_review(log, REVIEW_EVERY_CALL)
    _pooled_answer(log, stronger)
    debt = _pooled_start(log, weaker if names_the_weaker else new_id("decision"))

    honest = _pooled_start(log, stronger)

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == (f"tool.started without an allowing decision at seq {[debt.seq, honest.seq]}")


def test_a3_keeps_a_human_rejection_final_across_a_later_stronger_approval() -> None:
    """A refusal is about the call, not about the surface: no later answer reopens it.

    The rejected review's own request names this exact effect, which is what makes the refusal
    belong to it (`_ticket_was_rejected`). Constraints and order decide which *approvals* are
    currency; they decide nothing about a human's no.
    """
    log = InMemoryEventLog(new_id("run"))
    refused = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, refused, approved=False)
    _arm(log)
    stronger = _pooled_review(log, REVIEW_EVERY_CALL)
    _pooled_answer(log, stronger)

    started = _pooled_start(log, stronger)

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def test_a3_keeps_the_legacy_per_decision_allowance_for_an_unticketed_review() -> None:
    """A review whose request recorded no call at all still buys one execution per answer.

    Nothing about the ticket pool reaches this path: the decision's recorded policy hash and
    constraints are never consulted, because there is no ticket to pair them with. One human
    answer, one execution, exactly as before -- and a second start on that decision is a replay.
    """
    log = InMemoryEventLog(new_id("run"))
    subject = new_id("tool")
    decision_id = new_id("decision")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": decision_id,
            "decision": Decision.REQUIRE_HUMAN_REVIEW.value,
            "subject_kind": "tool_call",
        },
        subject_id=subject,
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"decision_id": decision_id, "summary": "run_python"},
        subject_id=subject,
    )
    _pooled_answer(log, decision_id)
    log.append(EventType.TOOL_STARTED, Actor.system(), {"tool": "echo"}, subject_id=subject)
    assert _pooled_a3(log)[0] is ControlStatus.PASSED

    replay = log.append(
        EventType.TOOL_STARTED, Actor.system(), {"tool": "echo"}, subject_id=subject
    )

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[replay.seq]}"


def test_a3_gives_no_pool_identity_to_a_digest_with_anything_glued_to_it() -> None:
    """A recorded `policy_sha256` is the digest or it is not a policy snapshot at all.

    A trailing newline is the case that decides between `re.match` and `re.fullmatch`: `$` also
    matches before a final newline, so a value one byte away from the real digest would otherwise
    read as the real digest, and two different recorded policies would share one identity. Both
    halves of the identity have to be readable and exact, so this decision has none -- its own
    approval funds nothing, and the start naming it is unauthorised.
    """
    log = InMemoryEventLog(new_id("run"))
    subject = new_id("tool")
    decision_id = new_id("decision")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": decision_id,
            "decision": Decision.REQUIRE_HUMAN_REVIEW.value,
            "subject_kind": "tool_call",
            "policy_sha256": f"{POOL_POLICY}\n",
            "execution_constraints": ExecutionConstraints().to_json_dict(),
        },
        subject_id=subject,
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision_id,
            "tool": POOL_TOOL,
            "arguments": POOL_ARGUMENTS,
            "tool_intent_sha256": POOL_TICKET,
        },
        subject_id=subject,
    )
    _pooled_answer(log, decision_id)

    started = _pooled_start(log, decision_id)

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[started.seq]}"


def _blocking_run_level(log: InMemoryEventLog) -> str:
    """A run-level decision whose allow-list shares no tool with the pooled effect's own."""
    decision_id = new_id("decision")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": decision_id,
            "decision": Decision.PASS.value,
            "subject_kind": "run",
            "policy_sha256": POOL_POLICY,
            "execution_constraints": ExecutionConstraints(
                allowed_tools=("append_probe",)
            ).to_json_dict(),
        },
    )
    return decision_id


def test_a6_explains_a_denial_recorded_under_a_blocking_call_decision() -> None:
    """The trace a cached answer now ends in: the engine BLOCKs the call and the denial names it.

    Two allow-lists that share no tool cannot be merged into anything truthful, so the engine
    refuses the call outright rather than reading the empty intersection as "unconstrained". The
    manager's refusal quotes that `BLOCK`, and A6 explains it through the subject's own decision.
    """
    log = InMemoryEventLog(new_id("run"))
    approved = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, approved)
    _blocking_run_level(log)
    subject = new_id("tool")
    blocked = new_id("decision")
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {
            "id": blocked,
            "decision": Decision.BLOCK.value,
            "subject_kind": "tool_call",
            "policy_sha256": POOL_POLICY,
            "execution_constraints": ExecutionConstraints().to_json_dict(),
        },
        subject_id=subject,
    )
    log.append(
        EventType.TOOL_DENIED,
        Actor.system(),
        {"tool_call_id": subject, "decision_id": blocked, "reason": "blocked", "tool": POOL_TOOL},
        subject_id=subject,
    )

    a6 = a6_denials_recorded(AuditContext(log.run_id, log.events()))

    assert (a6[0], a6[1]) == (ControlStatus.PASSED, "1 denials explained")


def test_a3_reports_a_start_naming_the_answer_the_conflicting_run_level_decision_invalidated() -> (
    None
):
    """The same trace with one forged execution: the cached answer buys it nothing.

    The stored approval was given before the run-wide allow-list narrowed the surface, so it is a
    credit under a record that no longer describes this call. Under the Run's review requirement
    the decision it names does not even carry that requirement, and a start quoting it is
    unauthorised twice over.
    """
    log = InMemoryEventLog(new_id("run"))
    approved = _pooled_review(log, ExecutionConstraints())
    _pooled_answer(log, approved)
    _arm(log)
    _blocking_run_level(log)

    forged = _pooled_start(log, approved)

    a3 = _pooled_a3(log)
    assert a3[0] is ControlStatus.FAILED
    assert a3[1] == f"tool.started without an allowing decision at seq {[forged.seq]}"
