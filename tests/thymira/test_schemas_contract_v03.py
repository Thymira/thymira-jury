"""Unit tests for the Contract 0.3 shared records and canonical authorization hashes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thymira.events import hash_authorization_context
from thymira.schemas import (
    DECISION_ID_KEYS,
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    Approval,
    AuthorizationContext,
    AuthorizationDecision,
    CompositeRunState,
    Event,
    EventType,
    RiskAssessment,
    RiskLevel,
    RunCondition,
    RunOutcome,
    RunStage,
    RunState,
    WaitReason,
    approval_decision_id,
    approval_names_decision,
    new_id,
)

SHA = "a" * 64


def _intent() -> ActionIntent:
    """Build one bounded, non-authorizing action intent."""
    run_id = new_id("run")
    return ActionIntent(
        id=new_id("intent"),
        run_id=run_id,
        requester=Actor(kind=ActorKind.AGENT, id="mira"),
        action_kind=ActionKind.PAUSE_RUN,
        subject_kind="run",
        subject_id=run_id,
        purpose="Pause until the evidence is reviewed.",
        idempotency_key="pause-after-finding-1",
    )


def _context(intent: ActionIntent) -> AuthorizationContext:
    """Build one authorization context with deterministic semantic content."""
    return AuthorizationContext(
        id=new_id("authorization"),
        run_id=intent.run_id,
        intent_id=intent.id,
        policy_decision_id=new_id("decision"),
        policy_sha256=SHA,
        decision=AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_kind=intent.action_kind,
        constraints={"maximum_attempts": 1},
        requires_approval=True,
    )


@pytest.mark.parametrize(
    ("condition", "wait_reason", "outcome", "match"),
    [
        (RunCondition.WAITING, None, None, "requires wait_reason"),
        (RunCondition.ACTIVE, WaitReason.INFORMATION, None, "only valid while waiting"),
        (RunCondition.TERMINAL, None, None, "requires outcome"),
        (RunCondition.ACTIVE, None, RunOutcome.FAILED, "only valid for terminal states"),
    ],
)
def test_composite_run_state_rejects_impossible_combinations(
    condition: RunCondition,
    wait_reason: WaitReason | None,
    outcome: RunOutcome | None,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        RunState(
            stage=RunStage.EXECUTING,
            condition=condition,
            wait_reason=wait_reason,
            outcome=outcome,
        )


def test_composite_run_state_accepts_a_waiting_approval() -> None:
    state = RunState(
        stage=RunStage.AUDITING,
        condition=RunCondition.WAITING,
        wait_reason=WaitReason.APPROVAL,
    )

    assert state.version == 0
    assert state.outcome is None


def test_completed_outcome_requires_reporting_stage() -> None:
    with pytest.raises(ValidationError, match="requires reporting"):
        RunState(
            stage=RunStage.AUDITING,
            condition=RunCondition.TERMINAL,
            outcome=RunOutcome.COMPLETED,
        )


def test_composite_run_state_remains_a_descriptive_public_alias() -> None:
    assert CompositeRunState is RunState


def test_risk_assessment_is_versioned_immutable_evidence() -> None:
    assessment = RiskAssessment(
        id=new_id("assessment"),
        run_id=new_id("run"),
        activity_profile_id=new_id("profile"),
        activity_profile_version=1,
        assessor=Actor(kind=ActorKind.AGENT, id="risk-agent"),
        subject_kind="model",
        subject_id="model-1",
        risk_level=RiskLevel.HIGH,
        activity_category="credit_scoring",
        confidence=0.9,
        justification="Sensitive attributes require review.",
        assessment_method="risk-rules",
        assessment_method_version="2026.08",
    )

    assert assessment.schema_version == "0.4"
    field_name = "schema_version"
    with pytest.raises(ValidationError):
        setattr(assessment, field_name, "0.4")


def test_action_intent_is_not_an_authorization() -> None:
    intent = _intent()

    assert intent.action_kind is ActionKind.PAUSE_RUN
    assert "decision" not in ActionIntent.model_fields
    assert "authorized" not in ActionIntent.model_fields


def test_authorization_context_hash_excludes_its_nonsemantic_id() -> None:
    context = _context(_intent())
    replacement = context.model_copy(update={"id": new_id("authorization")})

    assert hash_authorization_context(context) == hash_authorization_context(replacement)
    changed = context.model_copy(update={"constraints": {"maximum_attempts": 2}})
    assert hash_authorization_context(context) != hash_authorization_context(changed)


def test_authorization_context_hash_is_canonical_across_constraint_key_order() -> None:
    context = _context(_intent()).model_copy(update={"constraints": {"a": 1, "b": 2}})
    reordered = context.model_copy(update={"constraints": {"b": 2, "a": 1}})

    assert hash_authorization_context(context) == hash_authorization_context(reordered)


def test_approval_references_exactly_one_authorization_context_hash() -> None:
    context = _context(_intent())
    approval = Approval(
        id=new_id("approval"),
        run_id=context.run_id,
        policy_decision_id=context.policy_decision_id,
        authorization_context_sha256=hash_authorization_context(context),
        approved=True,
        approved_by=Actor(kind=ActorKind.HUMAN, id="reviewer"),
    )

    assert approval.authorization_context_sha256 == hash_authorization_context(context)
    with pytest.raises(ValidationError):
        Approval.model_validate({**approval.to_json_dict(), "another_context_sha256": SHA})


def test_event_envelope_carries_contract_03_provenance_fields() -> None:
    event = Event(
        event_id=new_id("event"),
        run_id=new_id("run"),
        seq=0,
        type=EventType.RUN_TRANSITIONED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.core",
        producer_version="0.3.0",
        correlation_id="request-1",
        causation_id="intent-1",
        authorization_context_sha256=SHA,
    )

    assert event.event_id.startswith("event_")
    assert event.schema_version == "0.3"
    assert event.producer == "thymira.core"
    assert event.hashable_dict()["correlation_id"] == "request-1"


# --------------------------------------------------- the decision id an approval payload names


def _approval(decision_id: str) -> Approval:
    """A well-formed Contract 0.3 approval for one decision."""
    return Approval(
        id=new_id("approval"),
        run_id=new_id("run"),
        policy_decision_id=decision_id,
        authorization_context_sha256=SHA,
        approved=True,
        approved_by=Actor(kind=ActorKind.HUMAN, id="alice", authenticated=True),
    )


def test_decision_id_keys_put_the_contract_field_first() -> None:
    """The documented precedence, pinned: the frozen `Approval` field outranks the ad-hoc key."""
    assert DECISION_ID_KEYS == ("policy_decision_id", "decision_id")


def test_the_serialised_approval_names_its_decision_under_the_contract_field() -> None:
    """The accessor's first key is the one `Approval` really writes; this dies on a rename."""
    decision_id = new_id("decision")

    payload = _approval(decision_id).to_json_dict()

    assert payload["policy_decision_id"] == decision_id
    assert "decision_id" not in payload
    assert approval_decision_id(payload) == decision_id


def test_approval_decision_id_reads_the_hand_built_key_too() -> None:
    """The Gate's own answer payload names the same field `decision_id`."""
    decision_id = new_id("decision")

    assert approval_decision_id({"decision_id": decision_id, "approved": True}) == decision_id


def test_approval_decision_id_prefers_the_contract_field_when_the_two_disagree() -> None:
    """Precedence decision: the validated record field wins over a key added beside it.

    Reversing the order would let a `decision_id` added next to a serialised `Approval` redirect a
    genuine approval onto a decision it never approved. This test dies if the order is swapped.
    """
    contract, ad_hoc = new_id("decision"), new_id("decision")

    named = approval_decision_id({"policy_decision_id": contract, "decision_id": ad_hoc})

    assert named == contract
    assert named != ad_hoc


def test_approval_names_decision_follows_the_same_precedence() -> None:
    """The comparison helper inherits the precedence; it does not match on either key."""
    contract, ad_hoc = new_id("decision"), new_id("decision")
    payload = {"policy_decision_id": contract, "decision_id": ad_hoc, "approved": True}

    assert approval_names_decision(payload, contract) is True
    assert approval_names_decision(payload, ad_hoc) is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"approved": True},
        {"policy_decision_id": None, "decision_id": None},
        {"policy_decision_id": "", "decision_id": ""},
        {"policy_decision_id": 17},
        {"policy_decision_id": ["decision_0"]},
    ],
)
def test_approval_decision_id_is_none_when_no_usable_name_is_recorded(
    payload: dict[str, object],
) -> None:
    """`None` is never an identity: only a non-empty string names a decision."""
    assert approval_decision_id(payload) is None


def test_an_unusable_contract_value_falls_through_to_the_hand_built_key() -> None:
    """A garbage value under the first key is not a name, so the second key still counts."""
    decision_id = new_id("decision")

    assert approval_decision_id({"policy_decision_id": None, "decision_id": decision_id}) == (
        decision_id
    )


@pytest.mark.parametrize("decision_id", [None, "", 17, ["decision_0"]])
def test_approval_names_decision_never_matches_a_missing_id(decision_id: object) -> None:
    """`None == None` is the fail-open: a payload naming nothing answers nothing at all."""
    assert approval_names_decision({}, decision_id) is False
    assert approval_names_decision({"approved": True}, decision_id) is False


def test_approval_names_decision_requires_the_exact_id() -> None:
    """Tolerating two names is not tolerating two values."""
    payload = _approval(new_id("decision")).to_json_dict()

    assert approval_names_decision(payload, new_id("decision")) is False
    assert approval_names_decision(payload, payload["policy_decision_id"]) is True
