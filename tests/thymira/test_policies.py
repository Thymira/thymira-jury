"""Policy Engine and Gate: rule matching, precedence, fail-safe escalation, recorded approvals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira import observability
from thymira.events import InMemoryEventLog
from thymira.policies import (
    ActionRule,
    ApprovalRequest,
    Approver,
    BudgetRule,
    CapabilityRule,
    Gate,
    ModelRule,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    allows_execution,
    auto_approve,
    auto_reject,
    load_default_policy,
    load_policy,
    load_policy_stack,
    policy_sha256,
)
from thymira.schemas import (
    ActionKind,
    Actor,
    ActorKind,
    Approval,
    AuditFinding,
    AuthorizationContext,
    AuthorizationDecision,
    Decision,
    EventType,
    ExecutionAction,
    ExecutionConstraints,
    Framework,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

RUN = new_id("run")
CONFIDENT = RiskProfile(risk_level="high", activity_category="model_development", confidence=0.9)


def _finding(
    severity: Severity, *, confidence: float = 0.5, evidence: bool = True, control: str = "X-1"
) -> AuditFinding:
    from thymira.schemas import Evidence

    return AuditFinding(
        id=new_id("finding"),
        run_id=RUN,
        control_id=control,
        framework=Framework.METHODOLOGY,
        title="t",
        finding="f",
        severity=severity,
        confidence=confidence,
        evidence=(Evidence(kind="event", ref="seq:1"),) if evidence else (),
    )


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(load_policy_stack("credit_risk"))


def test_default_policies_load_and_hash_deterministically() -> None:
    base = load_default_policy("base")
    assert base.label == "base@1.0"
    assert policy_sha256(base) == policy_sha256(load_default_policy("base"))
    stack = load_policy_stack("credit_risk")
    assert stack.name == "base+credit-risk"
    assert stack.capability_rules[0].id == "CR-001"  # overlay rules come first
    assert policy_sha256(stack) != policy_sha256(base)
    with pytest.raises(FileNotFoundError):
        load_default_policy("nope")


def test_base_policy_hash_is_pinned_against_silent_field_additions() -> None:
    """`policy_sha256` hashes every field, defaults included (product-final.md, correction C-1).

    A future field added to `Policy` with an empty default still changes this hash and
    invalidates every recorded `PolicyDecision.policy_sha256` (control A16's replay). This pins
    the hash of the shipped `base` policy so that silent perturbation fails loudly here instead
    of surfacing as an unexplained replay mismatch later. Update the constant only in the same
    change that documents the new field in a correction, never to make this test pass.

    Rewritten when GOV-103b closed the HIGH-severity confidence hole and again when C8 added
    effect-aware capability rules: each is a deliberate, documented change to the policy's rule
    content, which is exactly the kind of change this pin is meant to make visible rather than the
    silent kind it forbids.
    """
    assert (
        policy_sha256(load_default_policy("base"))
        == "67b1b375e948aeeec9db7ffef2540844b21e58b52abb73bae8690a1681584cff"
    )


def test_budget_and_model_rules_are_frozen_and_overlay_before_the_base(
    engine: PolicyEngine,
) -> None:
    """`GOV-03`: the fields exist now; `POL-01`/`POL-02` add the deciding logic later."""
    base = Policy(
        name="base",
        version="1.0",
        budget_rules=(BudgetRule(id="B-BASE", decision=Decision.WARNING, reason="base ceiling"),),
        model_rules=(ModelRule(id="M-BASE", decision=Decision.WARNING, reason="base models"),),
    )
    overlay = Policy(
        name="overlay",
        version="1.0",
        budget_rules=(
            BudgetRule(id="B-OVERLAY", decision=Decision.BLOCK, reason="overlay ceiling"),
        ),
        model_rules=(ModelRule(id="M-OVERLAY", decision=Decision.BLOCK, reason="overlay models"),),
    )
    merged = base.merged_with(overlay)

    assert [rule.id for rule in merged.budget_rules] == ["B-OVERLAY", "B-BASE"]
    assert [rule.id for rule in merged.model_rules] == ["M-OVERLAY", "M-BASE"]
    # Populating an already-frozen field changes the hash, same as any other rule field
    # (product-final.md correction C-1) — proving the field participates in replay (control A16)
    # instead of being silently excluded.
    assert policy_sha256(engine.policy) != policy_sha256(
        engine.policy.model_copy(update={"budget_rules": base.budget_rules})
    )


def test_action_rules_first_match_then_default(engine: PolicyEngine) -> None:
    blocked = engine.decide_action(
        run_id=RUN, subject_kind="task", subject_id="t1", action_type="deploy_model"
    )
    assert blocked.decision is Decision.BLOCK
    assert blocked.rule_id == "GOV-001"
    assert blocked.policy_sha256 == engine.policy_sha256
    final = engine.decide_action(
        run_id=RUN, subject_kind="run", subject_id="run", action_type="final_decision"
    )
    assert final.decision is Decision.REQUIRE_HUMAN_REVIEW
    unknown = engine.decide_action(
        run_id=RUN, subject_kind="task", subject_id="t2", action_type="frobnicate"
    )
    assert (unknown.decision, unknown.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "default")


def test_capability_block_precedence_pass_and_fail_safe(engine: PolicyEngine) -> None:
    external = ToolCapability(id="send_email", external_effects=("network",))
    assert (
        engine.decide_capability(
            run_id=RUN, subject_id="c1", capability=external, risk=CONFIDENT
        ).decision
        is Decision.BLOCK
    )
    local = ToolCapability(id="profile_dataset")
    passed = engine.decide_capability(run_id=RUN, subject_id="c2", capability=local, risk=CONFIDENT)
    assert (passed.decision, passed.rule_id) == (Decision.PASS, "GOV-005")
    uncertain = engine.decide_capability(
        run_id=RUN, subject_id="c3", capability=local, risk=RiskProfile(confidence=0.2)
    )
    assert uncertain.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert "Fail-safe escalation" in uncertain.reason
    assert "unknown risk level" in uncertain.reason
    training = ToolCapability(id="train", risk_tags=("model_training",))
    sensitive = CONFIDENT.model_copy(update={"risk_factors": ("sensitive_attributes",)})
    review = engine.decide_capability(
        run_id=RUN, subject_id="c4", capability=training, risk=sensitive
    )
    assert (review.decision, review.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "CR-001")


def test_base_policy_requires_review_for_local_side_effects() -> None:
    """A local mutation must not be authorised by the read-only capability rule."""
    decision = _base_engine().decide_capability(
        run_id=RUN,
        subject_id="writer",
        capability=ToolCapability(
            id="write_file",
            side_effects=("workspace_write",),
            external_effects=(),
        ),
        risk=CONFIDENT,
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "GOV-008"


@pytest.mark.parametrize(
    ("rule_effects", "capability_effects", "expected"),
    [
        pytest.param(None, ("workspace_write",), True, id="ignored"),
        pytest.param((), (), True, id="empty-matches-empty"),
        pytest.param((), ("workspace_write",), False, id="empty-requires-none"),
        pytest.param(("workspace_write",), ("workspace_write",), True, id="specific-intersects"),
        pytest.param(("network",), ("workspace_write",), False, id="specific-does-not-intersect"),
        pytest.param(("*",), ("workspace_write",), True, id="wildcard-matches"),
    ],
)
def test_capability_rule_matches_declared_side_effects(
    rule_effects: tuple[str, ...] | None,
    capability_effects: tuple[str, ...],
    expected: bool,
) -> None:
    rule = CapabilityRule(
        id="EFFECTS",
        decision=Decision.PASS,
        reason="effect matching",
        side_effects=rule_effects,
    )

    capability = ToolCapability(id="tool", side_effects=capability_effects)
    assert rule.matches(CONFIDENT, capability) is expected


def test_execution_gate_derives_and_records_the_typed_constraints() -> None:
    constraints = ExecutionConstraints(
        allowed_tools=("profile",),
        prohibited_tools=("publish",),
        local_execution_only=True,
        required_evidence=("dataset-registered",),
        prohibited_actions=(ExecutionAction.PLAN,),
        max_tool_calls=2,
    )
    policy = Policy(
        name="constraints",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=Decision.PASS,
                reason="execution is authorised",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="LOCAL",
                decision=Decision.PASS,
                reason="local analysis",
                execution_constraints=constraints,
            ),
        ),
    )
    log = InMemoryEventLog(RUN)
    decision = Gate(PolicyEngine(policy), log).authorize_execution(
        CONFIDENT, (ToolCapability(id="profile"),)
    )

    assert decision.decision is Decision.PASS
    assert decision.execution_constraints == constraints
    assert log.events()[-1].payload["execution_constraints"] == constraints.model_dump(mode="json")


def test_execution_gate_blocks_an_authorised_prohibited_action() -> None:
    policy = Policy(
        name="no-execution",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=Decision.PASS,
                reason="would otherwise start",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="NO-START",
                decision=Decision.PASS,
                reason="the tool is known",
                execution_constraints=ExecutionConstraints(prohibited_actions=("execution.start",)),
            ),
        ),
    )

    decision = PolicyEngine(policy).decide_execution(
        run_id=RUN, risk=CONFIDENT, capabilities=(ToolCapability(id="profile"),)
    )

    assert decision.decision is Decision.BLOCK
    assert "prohibited" in decision.reason


def test_findings_mapping_and_precedence(engine: PolicyEngine) -> None:
    assert engine.decide_findings(run_id=RUN, findings=[]).decision is Decision.PASS
    assert (
        engine.decide_findings(run_id=RUN, findings=[_finding(Severity.LOW)]).decision
        is Decision.WARNING
    )
    assert (
        engine.decide_findings(run_id=RUN, findings=[_finding(Severity.MEDIUM)]).rule_id
        == "GOV-104"
    )
    no_evidence = engine.decide_findings(
        run_id=RUN, findings=[_finding(Severity.HIGH, evidence=False)]
    )
    assert (no_evidence.decision, no_evidence.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "GOV-102")
    leakage = engine.decide_findings(run_id=RUN, findings=[_finding(Severity.LOW, control="A20")])
    assert (leakage.decision, leakage.rule_id) == (Decision.BLOCK, "CR-101")
    mixed = engine.decide_findings(
        run_id=RUN, findings=[_finding(Severity.MEDIUM), _finding(Severity.CRITICAL)]
    )
    assert mixed.decision is Decision.BLOCK
    assert len(mixed.finding_ids) == 2


def test_gate_records_decision_request_and_answer(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)
    human = Actor(kind=ActorKind.HUMAN, id="alice", role="risk-officer", authenticated=True)
    seen: list[ApprovalRequest] = []

    def approver(request: ApprovalRequest) -> bool:
        seen.append(request)
        return True

    gate = Gate(engine, log, approver=approver, human=human)
    decision = gate.check_action(
        subject_kind="run",
        subject_id="run",
        action_type="final_decision",
        summary="close the run",
        cost_so_far={"total_cost_usd": 0.12},
    )
    assert decision.requires_human_approval
    assert not allows_execution(decision)
    assert seen[0].cost_so_far == {"total_cost_usd": 0.12}
    types = [event.type for event in log.events()]
    assert types == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
        EventType.HUMAN_APPROVAL,
    ]
    assert log.events()[2].actor == human
    assert log.verify().valid


def test_gate_rejects_a_declared_human_answerer(engine: PolicyEngine) -> None:
    """A synchronous Gate cannot persist approval evidence for an unauthenticated human."""
    log = InMemoryEventLog(RUN)
    human = Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False)

    with pytest.raises(ValueError, match="authenticated human"):
        Gate(
            engine,
            log,
            approver=lambda _request: True,
            human=human,
        ).check_action(
            subject_kind="run",
            subject_id="run",
            action_type="final_decision",
        )

    assert not any(event.type is EventType.HUMAN_APPROVAL for event in log.events())


@pytest.mark.parametrize("answer", ["no", "yes", 1, None])
def test_a_non_bool_synchronous_answer_is_no_answer_and_leaves_the_review_pending(
    engine: PolicyEngine, answer: object
) -> None:
    """Ruling R16: a non-bool approver answer is never coerced to yes nor fabricated as a no.

    `Gate._record` -- the path every `check_action`/`check_capability`/... call shares -- writes
    no `human.approval` at all when the approver's own answer is not exactly a ``bool``: the
    request stays recorded and unanswered, so a real human can still answer it later.
    """
    log = InMemoryEventLog(RUN)
    human = Actor(kind=ActorKind.HUMAN, id="alice", role="risk-officer", authenticated=True)
    approver = cast("Approver", lambda _request: answer)
    gate = Gate(engine, log, approver=approver, human=human)

    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert not allows_execution(decision)
    types = [event.type for event in log.events()]
    assert types == [EventType.POLICY_DECISION, EventType.HUMAN_APPROVAL_REQUESTED]
    assert EventType.HUMAN_APPROVAL not in types
    assert log.verify().valid


def test_check_capability_details_land_on_the_request_and_never_on_the_decision() -> None:
    log = InMemoryEventLog(new_id("run"))
    gate = Gate(PolicyEngine(load_policy_stack()), log)
    risk = RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1)

    decision = gate.check_capability(
        subject_id="tool_1",
        capability=ToolCapability(id="echo", external_effects=()),
        risk=risk,
        summary="echo",
        details={"tool": "echo", "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    requested = next(e for e in log.events() if e.type is EventType.HUMAN_APPROVAL_REQUESTED)
    assert requested.payload["tool"] == "echo"
    assert requested.payload["arguments"] == {"value": "x"}
    assert requested.payload["tool_intent_sha256"] == "a" * 64
    assert requested.payload["decision_id"] == decision.id
    assert "tool" not in decision.to_json_dict()


def test_gate_rejection_pending_and_block(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)
    rejected = Gate(engine, log, approver=auto_reject).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )
    assert not allows_execution(rejected)
    assert log.events()[-1].payload["automatic"] is True

    pending = Gate(engine, log).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )
    assert pending.requires_human_approval
    assert not allows_execution(pending)

    approved = Gate(engine, log, approver=auto_approve).review_findings(
        [_finding(Severity.HIGH, evidence=False)]
    )
    assert not allows_execution(approved)
    blocked = Gate(engine, log, approver=auto_approve).review_findings(
        [_finding(Severity.CRITICAL)]
    )
    assert blocked.decision is Decision.BLOCK
    assert EventType.AUDIT_BLOCK in [event.type for event in log.events()]
    assert log.verify().valid


def test_load_policy_from_yaml_and_json(tmp_path: Path) -> None:
    base = load_default_policy("base")
    (tmp_path / "p.json").write_text(base.model_dump_json(), encoding="utf-8")
    assert load_policy(tmp_path / "p.json") == base
    yaml_text = (
        "name: tiny\nversion: '1'\naction_rules:\n"
        "  - id: R1\n    decision: BLOCK\n    reason: never\n"
    )
    (tmp_path / "p.yaml").write_text(yaml_text, encoding="utf-8")
    tiny = load_policy(tmp_path / "p.yaml")
    assert isinstance(tiny, Policy)
    assert tiny.action_rules[0].matches("anything", {}, "unknown")
    with pytest.raises(ValueError, match="unsupported"):
        load_policy(tmp_path / "p.txt")


def test_policy_sha256_tracks_the_policy_actually_evaluated() -> None:
    """A cached digest would pin what the engine was built with, not what decided."""
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    original = engine.policy_sha256

    # The rule models are frozen, but the mappings inside them are ordinary dicts.
    engine.policy.action_rules[0].payload_equals["injected"] = "x"

    assert engine.policy_sha256 != original
    decision = engine.decide_action(
        run_id=new_id("run"),
        subject_kind="tool_call",
        subject_id=new_id("tool"),
        action_type="run_python",
    )
    # The recorded digest is the mutated policy's, so an auditor replaying it sees the change
    # rather than a decision that silently disagrees with the policy it claims to come from.
    assert decision.policy_sha256 == engine.policy_sha256
    assert decision.policy_sha256 != original


def test_policy_carries_the_rule_families_its_engine_does_not_evaluate_yet() -> None:
    """Fields must exist before the contract freezes, not when their engine arrives.

    policy_sha256 hashes the whole model_dump, so adding a field later would change the hash of
    every policy already in use and break A16 replay for every decision ever recorded.
    """
    policy = load_policy_stack("credit_risk")
    assert policy.budget_rules == ()
    assert policy.model_rules == ()
    # They are part of the hashed content, which is exactly why they had to land now.
    assert "budget_rules" in policy.to_json_dict()
    assert "model_rules" in policy.to_json_dict()


def test_an_overlay_merges_every_rule_family() -> None:
    base = load_policy_stack("base")
    overlay = base.model_copy(
        update={
            "name": "overlay",
            "budget_rules": (
                BudgetRule(id="B1", decision=Decision.WARNING, reason="over budget", max_usd=10.0),
            ),
            "model_rules": (
                ModelRule(id="M1", decision=Decision.BLOCK, reason="model not allowed"),
            ),
        }
    )
    merged = base.merged_with(overlay)

    # A family that merged_with forgot would silently drop an overlay's rules.
    assert [r.id for r in merged.budget_rules] == ["B1"]
    assert [r.id for r in merged.model_rules] == ["M1"]


def test_only_a_low_finding_ever_reaches_the_findings_default(engine: PolicyEngine) -> None:
    """`findings_default_decision` is a floor, not a fail-open gap.

    It reads like one next to the two `REQUIRE_HUMAN_REVIEW` defaults, and has been raised as one
    more than once. The base policy `load_policy_stack` always loads first carries the full ladder,
    so every severity above LOW is matched by a rule and never falls through.
    """
    reached_default = []
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        decision = engine.decide_findings(run_id=RUN, findings=[_finding(severity)])
        if decision.rule_id == "findings_default":
            reached_default.append(severity)

    assert reached_default == [Severity.LOW]
    # And the severities that do not fall through are handled at least as strictly.
    assert (
        engine.decide_findings(run_id=RUN, findings=[_finding(Severity.CRITICAL)]).decision
        is Decision.BLOCK
    )


@pytest.mark.parametrize(
    ("confidence", "evidence", "rule_id"),
    [
        (0.95, True, "GOV-103"),
        (0.95, False, "GOV-102"),
        (0.80, True, "GOV-103b"),
        (0.80, False, "GOV-102"),
        (0.10, True, "GOV-103b"),
    ],
)
def test_every_high_finding_reaches_a_human_whatever_its_confidence_and_evidence(
    confidence: float, evidence: bool, rule_id: str
) -> None:
    """A HIGH finding is never softened by the audit agent's own confidence number.

    The base ladder used to route HIGH through GOV-102 (no evidence) and GOV-103 (confidence at
    least 0.9) only, so a well-evidenced HIGH finding whose author self-assessed below 0.9 fell
    through to GOV-104 and decided WARNING — treated *more* leniently than the same finding with
    no evidence at all. Confidence is a model output; the catch-all GOV-103b keeps it from
    relaxing an authorization, and precedence resolution keeps the overlap safe.
    """
    engine = PolicyEngine(load_default_policy("base"))

    decision = engine.decide_findings(
        run_id=RUN,
        findings=[_finding(Severity.HIGH, confidence=confidence, evidence=evidence)],
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == rule_id


@pytest.mark.parametrize("confidence", [0.95, 0.10])
@pytest.mark.parametrize("evidence", [True, False])
def test_a_critical_finding_blocks_whatever_its_confidence_and_evidence(
    confidence: float, evidence: bool
) -> None:
    """The rung above HIGH carries no confidence or evidence condition, so it has no such hole."""
    engine = PolicyEngine(load_default_policy("base"))

    decision = engine.decide_findings(
        run_id=RUN,
        findings=[_finding(Severity.CRITICAL, confidence=confidence, evidence=evidence)],
    )

    assert (decision.decision, decision.rule_id) == (Decision.BLOCK, "GOV-101")


def test_resolve_pending_approval_refuses_a_decision_the_control_plane_already_answered(
    engine: PolicyEngine,
) -> None:
    """Two writers, one field: an approval under the contract key already resolved the request.

    `Gate.request_approval` appends `Approval.to_json_dict()`, whose Contract 0.3 field is
    `policy_decision_id`; a reader looking only for `decision_id` would see the request as
    unresolved and record a second, possibly contradictory, answer on the append-only chain.
    """
    log = InMemoryEventLog(RUN)
    gate = Gate(engine, log, approver=None)
    decision = gate.check_action(subject_kind="run", subject_id="run", action_type="final_decision")
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {"policy_decision_id": decision.id, "approved": False},
        subject_id=decision.subject_id,
    )
    answering = Gate(engine, log, approver=auto_approve)

    with pytest.raises(ValueError, match="already been resolved"):
        answering.resolve_pending_approval(decision)

    assert [e.type for e in log.events()].count(EventType.HUMAN_APPROVAL) == 1


@pytest.mark.parametrize("answer", ["no", "yes", 1, None])
def test_resolve_pending_approval_raises_on_a_non_bool_answer(
    engine: PolicyEngine, answer: object
) -> None:
    """Ruling R16: a non-bool answer raises instead of writing a fabricated `human.approval`.

    This is the synchronous MVP seam the API's approve/reject route resolves through
    (``runtime/core/src/thymira/core/runs.py``); its ``ValueError`` is already mapped to a 409
    there, the same contract every other invalid-state check in this method uses.
    """
    log = InMemoryEventLog(RUN)
    decision = Gate(engine, log, approver=None).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )
    gate = Gate(engine, log, approver=cast("Approver", lambda _request: answer))

    with pytest.raises(ValueError, match="non-bool answer"):
        gate.resolve_pending_approval(decision)

    assert [e.type for e in log.events()].count(EventType.HUMAN_APPROVAL) == 0


@pytest.mark.parametrize("answer", ["no", "yes", 1, None])
def test_request_approval_records_no_answer_for_a_non_bool_approver(
    engine: PolicyEngine, answer: object
) -> None:
    """Ruling R16 at the control-plane writer: a non-bool answer buys neither yes nor no."""
    log = InMemoryEventLog(RUN)
    pending = Gate(engine, log, approver=None).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )
    reviewer = Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True)
    approver = cast("Approver", lambda _request: answer)
    gate = Gate(engine, log, approver=approver, human=reviewer)
    authorization = AuthorizationContext(
        id=new_id("authorization"),
        run_id=RUN,
        intent_id=new_id("intent"),
        policy_decision_id=pending.id,
        policy_sha256=engine.policy_sha256,
        decision=AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
        subject_kind="run",
        subject_id=pending.subject_id,
        action_kind=ActionKind.TRANSITION_RUN,
        requires_approval=True,
    )

    approval = gate.request_approval(authorization, pending, summary="close the run")

    assert approval is None
    types = [event.type for event in log.events()]
    assert types.count(EventType.HUMAN_APPROVAL) == 0
    assert types[-1] is EventType.HUMAN_APPROVAL_REQUESTED


def test_a_permissive_action_rule_never_shadows_a_stricter_later_one() -> None:
    """Overlapping action rules resolve by precedence, not by policy order.

    `decide_capability` was fixed for this and its two siblings were not, which is the shape the
    nightly sweep keeps finding: the same defect surviving in the copy nobody opened. It matters
    more here than for capabilities, because ``Policy.merged_with`` *prepends* an overlay, so the
    shadowing rule is exactly the one a project is invited to add -- an overlay granting
    ``deploy_model`` outranked the hard prohibition the shipped EU AI Act rule carries, and the
    BLOCK it displaced never reached the record.
    """
    permissive = ActionRule(
        id="OVERLAY-DEPLOY",
        decision=Decision.PASS,
        reason="This project deploys its own models.",
        action_types=("deploy_model",),
    )
    prohibition = ActionRule(
        id="GOV-001",
        decision=Decision.BLOCK,
        reason="Deploying a high-risk model is prohibited without conformity assessment.",
        action_types=("deploy_model",),
    )
    engine = PolicyEngine(
        Policy(name="overlay", version="1.0", action_rules=(permissive, prohibition))
    )

    decision = engine.decide_action(
        run_id=RUN, subject_kind="run", subject_id="r1", action_type="deploy_model"
    )

    assert (decision.decision, decision.rule_id) == (Decision.BLOCK, "GOV-001")
    # The rule that was outranked is named, so the decision can be replayed (control A16).
    assert "OVERLAY-DEPLOY" in decision.reason


def test_equal_precedence_action_rules_keep_policy_order() -> None:
    """Precedence decides; policy order only breaks a tie, so a prepended overlay still wins."""
    engine = PolicyEngine(
        Policy(
            name="tie",
            version="1.0",
            action_rules=(
                ActionRule(
                    id="OVERLAY",
                    decision=Decision.WARNING,
                    reason="Overlay warning.",
                    action_types=("train_model",),
                ),
                ActionRule(
                    id="BASE",
                    decision=Decision.WARNING,
                    reason="Base warning.",
                    action_types=("train_model",),
                ),
            ),
        )
    )

    decision = engine.decide_action(
        run_id=RUN, subject_kind="run", subject_id="r1", action_type="train_model"
    )

    assert (decision.decision, decision.rule_id) == (Decision.WARNING, "OVERLAY")
    assert "BASE" in decision.reason


def _capability_rule(rule_id: str, decision: Decision, reason: str) -> CapabilityRule:
    """A capability rule matching any tool tagged ``training``, so overlaps are deliberate."""
    return CapabilityRule(id=rule_id, decision=decision, reason=reason, risk_tags=("training",))


TRAINING = ToolCapability(id="train_model", risk_tags=("training",))


def test_a_permissive_capability_rule_never_shadows_a_stricter_later_one() -> None:
    """Overlapping capability rules resolve by precedence, not by policy order.

    `decide_capability` took the first match once no BLOCK matched, so a PASS rule listed ahead of
    a REQUIRE_HUMAN_REVIEW rule decided for it — and the recorded decision named only the
    permissive rule, leaving no trace that the stricter one matched at all. A guard that cannot
    show what it discarded is not auditable, and a policy that de-escalates on rule order is not
    fail-safe.
    """
    engine = PolicyEngine(
        Policy(
            name="overlap",
            version="1.0",
            capability_rules=(
                _capability_rule("P-001", Decision.PASS, "Local training tool."),
                _capability_rule("P-002", Decision.REQUIRE_HUMAN_REVIEW, "Training needs review."),
            ),
        )
    )

    decision = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=TRAINING, risk=CONFIDENT
    )

    assert (decision.decision, decision.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "P-002")
    # Not the fail-safe escalation doing the work: CONFIDENT is a complete, confident profile.
    assert "Fail-safe escalation" not in decision.reason
    # The shadowed match is on the record; a decision that silently discards a matching rule
    # cannot be replayed against the policy that produced it (control A16).
    assert "P-001" in decision.reason


def test_a_blocking_capability_rule_wins_from_any_position() -> None:
    """BLOCK is simply the highest precedence; it no longer needs its own special case."""
    engine = PolicyEngine(
        Policy(
            name="overlap",
            version="1.0",
            capability_rules=(
                _capability_rule("P-001", Decision.PASS, "Local training tool."),
                _capability_rule("P-002", Decision.REQUIRE_HUMAN_REVIEW, "Training needs review."),
                _capability_rule("P-003", Decision.BLOCK, "Training is prohibited here."),
            ),
        )
    )

    decision = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=TRAINING, risk=CONFIDENT
    )

    assert (decision.decision, decision.rule_id) == (Decision.BLOCK, "P-003")
    assert "P-001" in decision.reason
    assert "P-002" in decision.reason


def test_capability_rules_of_equal_precedence_break_the_tie_by_policy_order() -> None:
    """The same tie-break `decide_findings` uses: overlay rules are evaluated before base ones."""
    engine = PolicyEngine(
        Policy(
            name="overlap",
            version="1.0",
            capability_rules=(
                _capability_rule("P-001", Decision.REQUIRE_HUMAN_REVIEW, "Overlay rule."),
                _capability_rule("P-002", Decision.REQUIRE_HUMAN_REVIEW, "Base rule."),
            ),
        )
    )

    decision = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=TRAINING, risk=CONFIDENT
    )

    assert decision.rule_id == "P-001"


LOCAL = ToolCapability(id="profile_dataset")

# Every phrase `uncertainty_reasons` can contribute, so a parametrized case can assert that
# its own trigger fired *and* that no other one did.
FAIL_SAFE_REASONS: tuple[str, ...] = (
    "unknown risk level",
    "unknown activity category",
    "incomplete risk information",
    "classification confidence below the policy threshold",
    "classification flagged for human review",
)


def _base_engine() -> PolicyEngine:
    """The shipped base policy alone, so no overlay rule can decide instead of GOV-005."""
    return PolicyEngine(load_default_policy("base"))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        pytest.param({"risk_level": "unknown"}, FAIL_SAFE_REASONS[0], id="unknown-risk-level"),
        pytest.param(
            {"activity_category": "unknown"}, FAIL_SAFE_REASONS[1], id="unknown-activity-category"
        ),
        pytest.param(
            {"missing_information": ("target_column",)},
            FAIL_SAFE_REASONS[2],
            id="incomplete-information",
        ),
        pytest.param({"confidence": 0.5}, FAIL_SAFE_REASONS[3], id="confidence-below-threshold"),
        pytest.param({"needs_human_review": True}, FAIL_SAFE_REASONS[4], id="flagged-for-review"),
    ],
)
def test_each_fail_safe_trigger_escalates_a_permissive_capability_on_its_own(
    override: dict[str, Any], expected: str
) -> None:
    """Each of the five uncertainty triggers escalates GOV-005's PASS by itself.

    The suite only ever exercised two of them together, so `missing_information` and
    `needs_human_review` never ran: a guard nothing executes is a guard nothing proves. Each case
    starts from a complete, confident profile and perturbs exactly one fact, then asserts both the
    escalation and *which* reason produced it — an escalation for the wrong reason would otherwise
    pass as a correct one.
    """
    risk = CONFIDENT.model_copy(update=override)

    decision = _base_engine().decide_capability(
        run_id=RUN, subject_id="c1", capability=LOCAL, risk=risk
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "GOV-005"  # the rule still PASSes; the engine escalated it
    assert f"Fail-safe escalation: {expected}." in decision.reason
    assert [reason for reason in FAIL_SAFE_REASONS if reason in decision.reason] == [expected]


def test_a_capability_no_rule_covers_falls_back_to_the_escalating_default() -> None:
    """An uncovered capability is a request to act, so the fallback asks a human."""
    engine = PolicyEngine(Policy(name="empty", version="1.0"))

    decision = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=LOCAL, risk=CONFIDENT
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "capability_default"
    assert decision.reason == "Capability not covered by the policy: requires approval."


def test_a_permissive_capability_default_is_escalated_by_the_same_fail_safe() -> None:
    """The fallback is not a way around the uncertainty check, however a policy configures it."""
    engine = PolicyEngine(
        Policy(name="permissive", version="1.0", capability_default_decision=Decision.PASS)
    )

    decision = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=LOCAL, risk=RiskProfile()
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "capability_default"
    assert "Fail-safe escalation" in decision.reason


_INHERITED_REVIEW = ExecutionConstraints(requires_human_review=True)


def _one_rule_engine(decision: Decision, constraints: ExecutionConstraints) -> PolicyEngine:
    """An engine whose single capability rule covers everything and carries `constraints`."""
    return PolicyEngine(
        Policy(
            name="inherited",
            version="1.0",
            capability_rules=(
                CapabilityRule(
                    id="ONLY",
                    decision=decision,
                    reason="Rule reason.",
                    execution_constraints=constraints,
                ),
            ),
        )
    )


@pytest.mark.parametrize(
    ("rule_decision", "expected_decision", "expected_reason"),
    [
        pytest.param(
            Decision.PASS,
            Decision.REQUIRE_HUMAN_REVIEW,
            "Rule reason. Execution constraints require human review.",
            id="pass-escalates",
        ),
        pytest.param(
            Decision.WARNING,
            Decision.REQUIRE_HUMAN_REVIEW,
            "Rule reason. Execution constraints require human review.",
            id="warning-escalates",
        ),
        pytest.param(
            Decision.REQUIRE_HUMAN_REVIEW,
            Decision.REQUIRE_HUMAN_REVIEW,
            "Rule reason.",
            id="review-keeps-its-own-reason",
        ),
        pytest.param(Decision.BLOCK, Decision.BLOCK, "Rule reason.", id="block-stays-block"),
    ],
)
def test_decide_capability_escalates_only_permissive_outcomes_under_inherited_review(
    rule_decision: Decision, expected_decision: Decision, expected_reason: str
) -> None:
    """A run-wide review requirement raises PASS and WARNING once, and never touches BLOCK.

    The suffix is appended exactly once: a rule that already decided REQUIRE_HUMAN_REVIEW keeps
    its own reason rather than collecting a second sentence saying the same thing, and a BLOCK is
    never softened into a question for a human.
    """
    engine = _one_rule_engine(rule_decision, ExecutionConstraints())

    decision = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=CONFIDENT,
        inherited_constraints=_INHERITED_REVIEW,
    )

    assert decision.decision is expected_decision
    assert decision.rule_id == "ONLY"
    assert decision.reason == expected_reason
    assert decision.execution_constraints == _INHERITED_REVIEW


def test_decide_capability_persists_the_merge_of_the_rules_and_the_inherited_constraints() -> None:
    """The recorded constraints are the merge, so the decision says what really bound the call."""
    engine = _one_rule_engine(Decision.PASS, ExecutionConstraints(prohibited_tools=("train",)))

    decision = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=CONFIDENT,
        inherited_constraints=ExecutionConstraints(
            prohibited_tools=("publish",), requires_human_review=True, max_tool_calls=3
        ),
    )

    assert decision.execution_constraints == ExecutionConstraints(
        prohibited_tools=("train", "publish"), requires_human_review=True, max_tool_calls=3
    )


@pytest.mark.parametrize(
    "rule_decision",
    [Decision.PASS, Decision.WARNING, Decision.REQUIRE_HUMAN_REVIEW, Decision.BLOCK],
)
@pytest.mark.parametrize(
    "constraints",
    [ExecutionConstraints(), ExecutionConstraints(prohibited_tools=("train",))],
)
def test_decide_capability_without_inherited_constraints_decides_exactly_as_before(
    rule_decision: Decision, constraints: ExecutionConstraints
) -> None:
    """`inherited_constraints=None` is the untouched pre-existing behaviour, not a new default.

    Passing an empty `ExecutionConstraints()` explicitly must reach the same record, because that
    is what every caller who has no Run-wide constraints to inherit ends up sending.
    """
    engine = _one_rule_engine(rule_decision, constraints)

    omitted = engine.decide_capability(
        run_id=RUN, subject_id="c1", capability=LOCAL, risk=CONFIDENT
    )
    explicit = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=CONFIDENT,
        inherited_constraints=ExecutionConstraints(),
    )

    assert (omitted.decision, omitted.rule_id, omitted.reason) == (
        explicit.decision,
        explicit.rule_id,
        explicit.reason,
    )
    assert omitted.execution_constraints == explicit.execution_constraints == constraints


_ALLOW_LIST_CONFLICT_REASON = (
    " Execution constraints conflict: the tool's allow-list shares no tool with the run-wide "
    "allow-list."
)


@pytest.mark.parametrize(
    ("rule_decision", "expected_reason"),
    [
        pytest.param(Decision.PASS, "Rule reason." + _ALLOW_LIST_CONFLICT_REASON, id="pass"),
        pytest.param(
            Decision.REQUIRE_HUMAN_REVIEW,
            "Rule reason." + _ALLOW_LIST_CONFLICT_REASON,
            id="review",
        ),
        pytest.param(Decision.BLOCK, "Rule reason.", id="block-keeps-its-own-reason"),
    ],
)
def test_decide_capability_blocks_when_the_two_allow_lists_share_no_tool(
    rule_decision: Decision, expected_reason: str
) -> None:
    """Disjoint allow-lists authorise nothing, so no outcome under them may allow or ask.

    `ExecutionConstraints.merged_with` intersects two non-empty allow-lists, and an empty
    intersection is indistinguishable from "this constraint does not narrow anything" -- the merge
    would read back as "every tool is allowed", the opposite of what the two rules said. The
    engine answers the conflict itself instead: no tool satisfies both surfaces, so the decision
    is a BLOCK, and a rule that already blocked keeps the reason it blocked for.
    """
    engine = _one_rule_engine(rule_decision, ExecutionConstraints(allowed_tools=("profile",)))

    decision = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=CONFIDENT,
        inherited_constraints=ExecutionConstraints(allowed_tools=("train",)),
    )

    assert decision.decision is Decision.BLOCK
    assert decision.rule_id == "ONLY"
    assert decision.reason == expected_reason


def test_decide_capability_merges_two_allow_lists_that_still_share_a_tool() -> None:
    """An overlapping intersection is a real narrowing, so the ordinary merge decides."""
    engine = _one_rule_engine(
        Decision.PASS, ExecutionConstraints(allowed_tools=("profile", "train"))
    )

    decision = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=CONFIDENT,
        inherited_constraints=ExecutionConstraints(allowed_tools=("train", "publish")),
    )

    assert decision.decision is Decision.PASS
    assert decision.execution_constraints.allowed_tools == ("train",)


def _approval(decision_id: str, *, approved: bool) -> Approval:
    return Approval(
        id=new_id("approval"),
        run_id=RUN,
        policy_decision_id=decision_id,
        authorization_context_sha256="0" * 64,
        approved=approved,
        approved_by=Actor(
            kind=ActorKind.HUMAN,
            id="alice",
            role="risk-officer",
            authenticated=True,
        ),
    )


def test_allows_execution_refuses_a_block_and_every_approval_that_does_not_match() -> None:
    """A BLOCK is unappealable, and an approval only counts for the decision it names."""
    engine = _base_engine()
    blocked = engine.decide_action(
        run_id=RUN, subject_kind="task", subject_id="t1", action_type="deploy_model"
    )
    review = engine.decide_action(
        run_id=RUN, subject_kind="run", subject_id="run", action_type="final_decision"
    )

    assert blocked.decision is Decision.BLOCK
    # Not even a matching, granted approval lifts a BLOCK: approval resolves a review, never a
    # refusal.
    assert allows_execution(blocked) is False
    assert allows_execution(blocked, _approval(blocked.id, approved=True)) is False

    assert review.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert allows_execution(review) is False
    assert allows_execution(review, _approval(review.id, approved=False)) is False
    assert allows_execution(review, _approval(blocked.id, approved=True)) is False
    assert allows_execution(review, _approval(review.id, approved=True)) is True


def test_allows_execution_refuses_an_approval_from_a_declared_human() -> None:
    """An Approval object cannot create authority from an unauthenticated human label."""
    engine = _base_engine()
    review = engine.decide_action(
        run_id=RUN, subject_kind="run", subject_id="run", action_type="final_decision"
    )
    approval = _approval(review.id, approved=True).model_copy(
        update={
            "approved_by": Actor(
                kind=ActorKind.HUMAN,
                id="claimed-reviewer",
                role="risk-officer",
                authenticated=False,
            )
        }
    )

    assert allows_execution(review, approval) is False


STRICT_ACTION = ActionRule(
    id="A-1",
    description="Overwriting a named report is prohibited on a high-risk run.",
    action_types=("write_file",),
    risk_levels=("high",),
    payload_equals={"mode": "overwrite"},
    payload_contains={"paths": "reports/model.md"},
    decision=Decision.BLOCK,
    reason="Overwriting the model report destroys evidence.",
)
_MATCHING_PAYLOAD: dict[str, Any] = {"mode": "overwrite", "paths": ["reports/model.md"]}


@pytest.mark.parametrize(
    ("action_type", "risk_level", "payload", "matches"),
    [
        pytest.param("write_file", "high", _MATCHING_PAYLOAD, True, id="every-condition-holds"),
        pytest.param("read_file", "high", _MATCHING_PAYLOAD, False, id="action-type-differs"),
        pytest.param("write_file", "low", _MATCHING_PAYLOAD, False, id="risk-level-differs"),
        pytest.param("write_file", "unknown", _MATCHING_PAYLOAD, False, id="risk-level-unknown"),
        pytest.param(
            "write_file",
            "high",
            {"mode": "append", "paths": ["reports/model.md"]},
            False,
            id="payload-equals-differs",
        ),
        pytest.param(
            "write_file",
            "high",
            {"paths": ["reports/model.md"]},
            False,
            id="payload-equals-key-absent",
        ),
        pytest.param(
            "write_file",
            "high",
            {"mode": "overwrite", "paths": ["reports/other.md"]},
            False,
            id="payload-contains-value-absent",
        ),
        pytest.param(
            "write_file", "high", {"mode": "overwrite"}, False, id="payload-contains-key-absent"
        ),
        pytest.param(
            "write_file",
            "high",
            {"mode": "overwrite", "paths": "reports/model.md"},
            False,
            id="payload-contains-scalar-not-a-collection",
        ),
        pytest.param(
            "write_file",
            "high",
            {"mode": "overwrite", "paths": ("reports/model.md",)},
            True,
            id="payload-contains-tuple",
        ),
        pytest.param(
            "write_file",
            "high",
            {"mode": "overwrite", "paths": {"reports/model.md"}},
            True,
            id="payload-contains-set",
        ),
    ],
)
def test_an_action_rule_matches_only_when_every_declared_condition_holds(
    action_type: str, risk_level: str, payload: dict[str, Any], matches: bool
) -> None:
    """Pins `ActionRule.matches`: each declared group is a conjunct, none of them optional.

    `payload_contains` deliberately refuses a scalar: `"a/b" in "a/b_backup"` would otherwise make
    a string payload match by substring. The cost is that a rule written for a list never fires on
    a scalar payload of the same name, so a rule author must match the shape the action records.
    """
    assert STRICT_ACTION.matches(action_type, payload, risk_level) is matches


def test_resolve_pending_approval_accepts_a_request_recorded_under_the_contract_key(
    engine: PolicyEngine,
) -> None:
    """The request side reads the same accessor, so either recorded name locates the request."""
    log = InMemoryEventLog(RUN)
    decision = Gate(engine, log, approver=None).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )
    replayed = InMemoryEventLog(RUN)
    replayed.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"policy_decision_id": decision.id, "rule_id": decision.rule_id, "summary": "s"},
        subject_id=decision.subject_id,
    )

    approved = Gate(engine, replayed, approver=auto_approve).resolve_pending_approval(decision)

    assert approved is True
    assert [e.type for e in replayed.events()].count(EventType.HUMAN_APPROVAL) == 1


def test_the_gate_mirrors_every_decision_onto_the_run_s_trace(
    engine: PolicyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The score is a projection of `policy.decision`, never a second source of truth.

    Recorded here rather than at a call site because the Gate is the one place every verdict
    passes through, and it is what makes "how often did we BLOCK this week" a Langfuse query
    instead of a script over every `events.jsonl` on disk.
    """
    scored: list[dict[str, Any]] = []
    monkeypatch.setattr("thymira.policies.gate.score_run", lambda **fields: scored.append(fields))
    log = InMemoryEventLog(RUN)

    decision = Gate(engine, log, approver=auto_reject).check_action(
        subject_kind="run", subject_id="run", action_type="final_decision"
    )

    assert scored == [
        {
            "run_id": log.run_id,
            "name": "policy.decision",
            "value": decision.decision.value,
            "data_type": "CATEGORICAL",
            "comment": decision.rule_id,
        }
    ]
    assert log.events()[0].type is EventType.POLICY_DECISION


def test_a_gate_whose_trace_backend_is_broken_still_decides(engine: PolicyEngine) -> None:
    """Telemetry never fails a Run, and an authorization least of all.

    The isolation lives at one choke point inside `thymira.observability`, not at each call site,
    so this drives the real path rather than patching the helper away: `object()` is a client that
    answers nothing, which is what a misconfigured or half-built backend looks like from here.
    """
    observability.configure(client=object())
    try:
        decision = Gate(engine, InMemoryEventLog(RUN), approver=auto_reject).check_action(
            subject_kind="run", subject_id="run", action_type="final_decision"
        )
    finally:
        observability.reset()

    assert decision.rule_id


# ---------------------------------------------------------------------------------------------
# The classification's uncertainty is answered once, at execution.start, never per call after.

REVIEWED = CONFIDENT.model_copy(
    update={
        "confidence": 0.2,
        "missing_information": ("human_oversight",),
        "needs_human_review": True,
        "reviewed_decision_id": new_id("decision"),
    }
)
"""An uncertain profile a human already answered at the execution-start review."""


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        pytest.param({"risk_level": "unknown"}, FAIL_SAFE_REASONS[0], id="unknown-risk-level"),
        pytest.param(
            {"activity_category": "unknown"}, FAIL_SAFE_REASONS[1], id="unknown-activity-category"
        ),
        pytest.param(
            {"missing_information": ("target_column",)},
            FAIL_SAFE_REASONS[2],
            id="incomplete-information",
        ),
        pytest.param({"confidence": 0.5}, FAIL_SAFE_REASONS[3], id="confidence-below-threshold"),
        pytest.param({"needs_human_review": True}, FAIL_SAFE_REASONS[4], id="flagged-for-review"),
    ],
)
def test_decide_execution_escalates_a_permissive_start_on_each_uncertainty_trigger(
    override: dict[str, Any], expected: str
) -> None:
    """The start review carries the same fail-safe phrase the per-call path does."""
    risk = CONFIDENT.model_copy(update=override)

    decision = _base_engine().decide_execution(run_id=RUN, risk=risk, capabilities=(LOCAL,))

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "GOV-007"
    assert f"Fail-safe escalation: {expected}." in decision.reason
    assert [reason for reason in FAIL_SAFE_REASONS if reason in decision.reason] == [expected]


def test_decide_execution_keeps_a_confident_start_permissive() -> None:
    decision = _base_engine().decide_execution(run_id=RUN, risk=CONFIDENT, capabilities=(LOCAL,))

    assert (decision.decision, decision.rule_id) == (Decision.PASS, "GOV-007")
    assert "Fail-safe escalation" not in decision.reason


def test_decide_execution_does_not_re_escalate_a_reviewed_profile() -> None:
    decision = _base_engine().decide_execution(run_id=RUN, risk=REVIEWED, capabilities=(LOCAL,))

    assert (decision.decision, decision.rule_id) == (Decision.PASS, "GOV-007")


def test_a_prohibited_start_blocks_ahead_of_the_uncertainty_escalation() -> None:
    """BLOCK precedence is untouched: a prohibition never becomes a question for a human."""
    policy = Policy(
        name="no-execution",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=Decision.PASS,
                reason="would otherwise start",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="NO-START",
                decision=Decision.PASS,
                reason="the tool is known",
                execution_constraints=ExecutionConstraints(prohibited_actions=("execution.start",)),
            ),
        ),
    )

    decision = PolicyEngine(policy).decide_execution(
        run_id=RUN, risk=RiskProfile(), capabilities=(LOCAL,)
    )

    assert decision.decision is Decision.BLOCK
    assert "Fail-safe escalation" not in decision.reason


def test_uncertainty_escalates_the_start_ahead_of_a_run_wide_review_constraint() -> None:
    """The two review grounds never stack; the constraint itself is still recorded."""
    engine = _one_rule_engine(Decision.PASS, _INHERITED_REVIEW)
    engine = PolicyEngine(
        engine.policy.model_copy(
            update={
                "action_rules": (
                    ActionRule(
                        id="START",
                        action_types=("execution.start",),
                        decision=Decision.PASS,
                        reason="Start.",
                    ),
                )
            }
        )
    )

    decision = engine.decide_execution(run_id=RUN, risk=RiskProfile(), capabilities=(LOCAL,))

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert "Fail-safe escalation" in decision.reason
    assert "Execution constraints require human review" not in decision.reason
    assert decision.execution_constraints.requires_human_review is True


def test_a_reviewed_profile_lets_a_permissive_capability_rule_stand() -> None:
    decision = _base_engine().decide_capability(
        run_id=RUN, subject_id="c1", capability=LOCAL, risk=REVIEWED
    )

    assert (decision.decision, decision.rule_id) == (Decision.PASS, "GOV-005")
    assert "Fail-safe escalation" not in decision.reason


def test_a_reviewed_profile_still_reviews_under_a_run_wide_review_constraint() -> None:
    decision = _one_rule_engine(Decision.PASS, ExecutionConstraints()).decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=LOCAL,
        risk=REVIEWED,
        inherited_constraints=_INHERITED_REVIEW,
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.reason == "Rule reason. Execution constraints require human review."


def test_a_reviewed_profile_still_reviews_local_side_effects_under_gov_008() -> None:
    decision = _base_engine().decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=ToolCapability(id="write_file", side_effects=("workspace_write",)),
        risk=REVIEWED,
    )

    assert (decision.decision, decision.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "GOV-008")


def test_a_reviewed_profile_still_reviews_sensitive_training_under_cr_001(
    engine: PolicyEngine,
) -> None:
    decision = engine.decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=ToolCapability(id="train", risk_tags=("model_training",)),
        risk=REVIEWED.model_copy(update={"risk_factors": ("sensitive_attributes",)}),
    )

    assert (decision.decision, decision.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "CR-001")


def test_a_reviewed_profile_never_softens_a_block() -> None:
    decision = _base_engine().decide_capability(
        run_id=RUN,
        subject_id="c1",
        capability=ToolCapability(id="send_email", external_effects=("network",)),
        risk=REVIEWED,
    )

    assert (decision.decision, decision.rule_id) == (Decision.BLOCK, "GOV-003")
