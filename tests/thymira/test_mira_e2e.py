"""Slow local E2E coverage for the bounded credit-risk MIRA closeout.

Real: local JSON/JSONL Run evidence, MIRA evaluators, policy Gate, and RunController.
Faked: the bounded context analyst response through ScriptedProvider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thymira.agents.llm import ScriptedProvider
from thymira.core import (
    AuthorizationScopeError,
    RunController,
    RunEventLog,
    RunTransitionCommand,
    RunTransitionKind,
    apply_transition,
    initial_run_state,
)
from thymira.events import hash_authorization_context, redact_value, verify_events
from thymira.mira import (
    DecisionContextAnalyst,
    MiraAuditOrchestrator,
    MiraAuditSnapshot,
    MiraContextInput,
    load_default_packs,
)
from thymira.mira.preflight import EvidenceObservation
from thymira.policies import Gate, PolicyEngine, auto_approve, load_policy, load_policy_stack
from thymira.schemas import (
    ActionIntent,
    ActivityProfile,
    Actor,
    Approval,
    AuthorizationContext,
    AuthorizationDecision,
    Decision,
    DecisionContext,
    EventType,
    Evidence,
    ModelRoutePolicy,
    Run,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRunStore

pytestmark = pytest.mark.slow

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")
PROJECT = Path(__file__).resolve().parents[2] / "examples" / "credit-risk" / ".thymira"


def test_credit_risk_mira_closeout_is_local_reproducible_and_authorized(  # noqa: PLR0915  # one E2E flow
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the closed local MIRA path and reject authority shortcuts."""
    store = LocalRunStore(tmp_path / "runs")
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Audit the local credit-risk example before its human review.",
    )
    store.create(run, actor=Actor.system())
    artifacts = LocalArtifactStore(tmp_path / "artifacts", run.id)
    artifacts.save_json("run_environment.json", {"python": "3.13"}, produced_by=new_id("agent"))

    state = initial_run_state(run.id)
    for command in (
        RunTransitionKind.START,
        RunTransitionKind.BEGIN_EXECUTION,
        RunTransitionKind.BEGIN_AUDIT,
    ):
        transition = apply_transition(state, RunTransitionCommand(command, state.version))
        store.append(
            run.id,
            EventType.RUN_TRANSITIONED,
            Actor.system(),
            {**transition.event.payload(), "state": transition.state.state.model_dump(mode="json")},
            expected_version=store.version(run.id),
            producer="thymira.core",
        )
        state = transition.state

    start_event = store.events(run.id)[0]
    assert start_event.hash is not None
    start_evidence = Evidence(kind="event", ref="seq:0", sha256=start_event.hash)
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Prioritise consumer-credit applications for human review.",
        affected_population="Applicants for consumer credit in Spain.",
        decision_effect="Changes review order but never grants or denies credit.",
        autonomy="Recommendation only.",
        human_oversight="A credit analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=("credit_history",),
        potential_consequences=("An urgent application could be reviewed too late.",),
        evidence_refs=(start_evidence,),
    )
    store.append(
        run.id,
        EventType.ACTIVITY_PROFILE_RECORDED,
        Actor.system(),
        {"activity_profile": profile.model_dump(mode="json")},
        expected_version=store.version(run.id),
        producer="thymira.mira",
    )

    before_mira_state = RunController(store, now=lambda: NOW).current_state(run.id)
    before_mira_events = tuple(store.events(run.id))
    snapshot = MiraAuditSnapshot(
        run=run,
        activity_profile=profile,
        events=before_mira_events,
        evidence_observations=(
            EvidenceObservation(
                evidence=start_evidence,
                integrity_verified=True,
                observed_at=NOW,
            ),
        ),
        audited_at=NOW,
    )
    result = MiraAuditOrchestrator(load_default_packs(), Actor.system()).audit(
        snapshot, store=artifacts
    )

    # MIRA assesses and proposes, but cannot change either event evidence or Run state.
    controller = RunController(store, now=lambda: NOW)
    assert tuple(store.events(run.id)) == before_mira_events
    assert controller.current_state(run.id) == before_mira_state
    assert result.risk_assessment.risk_level.value == "low"
    assert len(result.pack_bindings) == 2
    assert {evaluation.control_id for evaluation in result.control_evaluations} == {
        "METHODOLOGY-EVIDENCE-001",
        "CREDIT-OVERSIGHT-001",
    }
    assert any(finding.control_id == "A2" for finding in result.audit_findings)
    assert result.action_intents
    assert all(intent.action_kind.value == "review_findings" for intent in result.action_intents)
    assert "authorized" not in ActionIntent.model_fields
    assert not any(event.type is EventType.TOOL_STARTED for event in store.events(run.id))

    store.append(
        run.id,
        EventType.RISK_ASSESSMENT_RECORDED,
        Actor.system(),
        {"risk_assessment": result.risk_assessment.model_dump(mode="json")},
        expected_version=store.version(run.id),
        producer="thymira.mira",
    )
    for binding in result.pack_bindings:
        store.append(
            run.id,
            EventType.PACK_BINDING_RECORDED,
            Actor.system(),
            {"pack_binding": binding.model_dump(mode="json")},
            expected_version=store.version(run.id),
            producer="thymira.mira",
        )
    for evaluation in result.control_evaluations:
        store.append(
            run.id,
            EventType.CONTROL_EVALUATION_RECORDED,
            Actor.system(),
            {"control_evaluation": evaluation.model_dump(mode="json")},
            expected_version=store.version(run.id),
            producer="thymira.mira",
        )
    for finding in result.audit_findings:
        store.append(
            run.id,
            EventType.AUDIT_FINDING,
            Actor.system(),
            {"finding": finding.model_dump(mode="json")},
            expected_version=store.version(run.id),
            subject_id=finding.id,
            producer="thymira.mira",
        )

    context_events = tuple(store.events(run.id))
    context_head = context_events[-1]
    assert context_head.hash is not None
    context_input = MiraContextInput(
        snapshot=MiraAuditSnapshot(
            run=run,
            activity_profile=profile,
            events=context_events,
            audited_at=NOW,
        ),
        activity_profile=profile,
        risk_assessment=result.risk_assessment,
        applicable_pack_bindings=tuple(
            binding for binding in result.pack_bindings if binding.applicable
        ),
        control_evaluations=result.control_evaluations,
        open_findings=result.audit_findings,
        evidence_refs=(start_evidence,),
        evidence_gaps=("Credit oversight artifact evidence was not supplied.",),
        run_version=len(context_events),
        terminal_event_seq=context_head.seq,
        terminal_event_hash=context_head.hash,
        generated_at=NOW,
    )
    response = {
        "summary": {
            "text": "MIRA found evidence that needs human review.",
            "evidence_refs": [start_evidence.model_dump(mode="json")],
        },
        "evidence_gaps": [
            {"text": "Credit oversight artifact evidence is missing.", "unknown": True}
        ],
    }
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "test-model")
    context = DecisionContextAnalyst(
        ScriptedProvider([response]), route_policy=TEST_ROUTE_POLICY
    ).create_context(context_input)

    # The context subagent returns evidence only; explicit persistence cannot authorize it either.
    assert "authorization" not in DecisionContext.model_fields
    assert controller.current_state(run.id) == before_mira_state
    store.create_context(context, actor=Actor.system(), expected_version=store.version(run.id))
    context_path = tmp_path / "runs" / run.id / "mira-context" / f"{context.id}.json"
    context_json = context_path.read_text(encoding="utf-8")
    assert json.loads(context_json) == context.to_json_dict()
    assert controller.current_state(run.id) == before_mira_state

    intent = next(
        intent for intent in result.action_intents if intent.action_kind.value == "review_findings"
    )
    project_policy = load_policy(PROJECT / "policies.yaml")
    engine = PolicyEngine(load_policy_stack("credit_risk").merged_with(project_policy))
    gate = Gate(engine, RunEventLog(store, run.id), approver=auto_approve)
    decision = gate.check_intent(intent)
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    recorded_decision = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.POLICY_DECISION and event.causation_id == intent.id
    )
    assert recorded_decision.payload == redact_value(decision.to_json_dict())
    authorization = AuthorizationContext(
        id=new_id("authorization"),
        run_id=run.id,
        intent_id=intent.id,
        policy_decision_id=decision.id,
        policy_sha256=decision.policy_sha256,
        decision=AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_kind=intent.action_kind,
        constraints={
            "expected_state_version": controller.current_state(run.id).version,
            "transition": RunTransitionKind.WAIT_FOR_APPROVAL.value,
        },
        requires_approval=True,
        expires_at=NOW + timedelta(minutes=5),
    )
    isolated_approval = Approval(
        id=new_id("approval"),
        run_id=run.id,
        policy_decision_id=decision.id,
        authorization_context_sha256=hash_authorization_context(authorization),
        approved=True,
        approved_by=Actor.system(),
    )
    with pytest.raises(AuthorizationScopeError, match="matching Gate record"):
        controller.transition(
            intent=intent,
            decision=decision,
            context=authorization,
            approval=isolated_approval,
        )
    assert controller.current_state(run.id) == before_mira_state

    approval = gate.request_approval(authorization, decision, summary=intent.purpose)
    assert approval is not None
    assert approval.approved
    transition_event, waiting = controller.transition(
        intent=intent,
        decision=decision,
        context=authorization,
        approval=approval,
    )
    assert transition_event.type is EventType.RUN_TRANSITIONED
    assert waiting.condition.value == "waiting"
    assert waiting.wait_reason is not None
    assert waiting.wait_reason.value == "approval"

    store.append(
        run.id,
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"status": result.audit_report.status},
        expected_version=store.version(run.id),
        producer="thymira.mira",
    )
    store.append(
        run.id,
        EventType.RUN_COMPLETED,
        Actor.system(),
        {"status": "COMPLETED"},
        expected_version=store.version(run.id),
    )

    events = store.events(run.id)
    assert verify_events(events).valid
    assert {event.type for event in events} >= {
        EventType.ACTIVITY_PROFILE_RECORDED,
        EventType.RISK_ASSESSMENT_RECORDED,
        EventType.PACK_BINDING_RECORDED,
        EventType.CONTROL_EVALUATION_RECORDED,
        EventType.AUDIT_FINDING,
        EventType.MIRA_CONTEXT_CREATED,
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
        EventType.HUMAN_APPROVAL,
        EventType.RUN_TRANSITIONED,
        EventType.RUN_COMPLETED,
    }

    projection_path = tmp_path / "runs" / run.id / "run.json"
    projection_path.unlink()
    projected = store.get(run.id)
    assert projected.id == run.id
    assert projected.project_id == run.project_id
    assert projected.session_id == run.session_id
    assert projected.prompt == run.prompt
    assert projected.created_at == run.created_at
    assert projected.finding_ids == tuple(finding.id for finding in result.audit_findings)
    assert projected.policy_decision_id == decision.id
    assert projected.final_decision is Decision.REQUIRE_HUMAN_REVIEW
    rebuilt = json.loads(projection_path.read_text(encoding="utf-8"))
    assert rebuilt["run_state"]["condition"] == "waiting"
    assert context.based_on_run_version < store.version(run.id)
    assert context_path.read_text(encoding="utf-8") == context_json

    events_path = tmp_path / "runs" / run.id / "events.jsonl"
    contents = events_path.read_text(encoding="utf-8")
    events_path.write_text(
        contents.replace("credit-risk", "tampered-risk", 1), encoding="utf-8", newline="\n"
    )
    with pytest.raises(ValueError, match=r"events\.jsonl is invalid"):
        store.events(run.id)
    assert context_path.read_text(encoding="utf-8") == context_json
