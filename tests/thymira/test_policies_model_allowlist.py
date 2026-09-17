"""POL-02: model allow-list policy (a substitution is a WARNING).

An allow-list of model ids is a policy rule (ADR-0004). The router chooses a model by code and
records it as a ``model.selected`` event; the Policy Engine reads that choice and holds it against
the policy's model rules. An allowed model passes; a denied, off-allow-list or over-tier model is a
WARNING that names the rule -- surfaced, never silent -- and is recorded through the Gate. The
model_rules field GOV-03 froze is defaulted empty, so it never perturbs a shipped policy's hash.
"""

from __future__ import annotations

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import (
    Gate,
    ModelRule,
    Policy,
    PolicyEngine,
    allows_execution,
    load_default_policy,
    policy_sha256,
)
from thymira.schemas import Decision, EventType, new_id

RUN = new_id("run")

# The pinned base hash from GOV-03 (tests/thymira/test_policies.py). POL-02 adds only deciding
# logic and no Policy field, so the empty-defaulted model_rules must leave this hash unchanged.
# It moves when shipped policy content or serialised policy fields change. The value includes
# GOV-103b from the preceding policy correction and the execution constraints added by task 3;
# POL-02 itself adds no Policy field. The two pins must be updated together -- they are deliberately
# in different modules so that a change cannot be waved through by editing the one test opened.
BASE_POLICY_SHA256 = "67b1b375e948aeeec9db7ffef2540844b21e58b52abb73bae8690a1681584cff"

ALLOWLIST = ModelRule(
    id="MODEL-MIRA",
    description="MIRA runs only on the approved frontier models.",
    decision=Decision.WARNING,
    reason="MIRA ran on a model outside its approved allow-list.",
    roles=("mira",),
    allowed_models=("frontier-a", "frontier-b"),
)


def _engine(*model_rules: ModelRule) -> PolicyEngine:
    return PolicyEngine(Policy(name="models", version="1.0", model_rules=model_rules))


@pytest.fixture
def engine() -> PolicyEngine:
    return _engine(ALLOWLIST)


def test_an_in_allowlist_model_passes(engine: PolicyEngine) -> None:
    decision = engine.decide_model(
        run_id=RUN, subject_id="agent_mira", role="mira", model="frontier-a"
    )

    assert decision.decision is Decision.PASS
    assert decision.rule_id == "MODEL-MIRA"
    assert decision.subject_kind == "task"
    assert decision.policy_sha256 == engine.policy_sha256


def test_an_out_of_allowlist_model_warns_naming_the_rule(engine: PolicyEngine) -> None:
    decision = engine.decide_model(
        run_id=RUN, subject_id="agent_mira", role="mira", model="rogue-model"
    )

    assert decision.decision is Decision.WARNING
    assert decision.rule_id == "MODEL-MIRA"
    assert decision.reason == "MIRA ran on a model outside its approved allow-list."
    assert decision.policy_sha256 == engine.policy_sha256


def test_base_policy_hash_unchanged_by_the_empty_model_rules_field() -> None:
    base = load_default_policy("base")

    assert base.model_rules == ()
    assert policy_sha256(base) == BASE_POLICY_SHA256


def test_a_role_no_rule_covers_is_unconstrained(engine: PolicyEngine) -> None:
    # The allow-list governs MIRA only; a THY selection no rule covers authorizes nothing by
    # itself, so it passes -- the action, capability and tool gates decide effects.
    decision = engine.decide_model(run_id=RUN, subject_id="agent_thy", role="thy", model="anything")

    assert (decision.decision, decision.rule_id) == (Decision.PASS, "model_default")


def test_no_model_rules_configured_passes() -> None:
    engine = _engine()

    decision = engine.decide_model(run_id=RUN, subject_id="a", role="mira", model="x")

    assert (decision.decision, decision.rule_id) == (Decision.PASS, "model_default")


def test_a_denied_model_takes_the_rules_decision() -> None:
    engine = _engine(
        ModelRule(
            id="DENY",
            decision=Decision.WARNING,
            reason="denied model",
            roles=("*",),
            denied_models=("banned",),
        )
    )

    # An empty allow-list plus a deny-list means "anything not denied".
    allowed = engine.decide_model(run_id=RUN, subject_id="a", role="agent", model="ok")
    assert allowed.decision is Decision.PASS
    denied = engine.decide_model(run_id=RUN, subject_id="a", role="agent", model="banned")
    assert (denied.decision, denied.rule_id) == (Decision.WARNING, "DENY")


def test_a_model_over_the_tier_ceiling_warns() -> None:
    engine = _engine(
        ModelRule(
            id="TIER",
            decision=Decision.WARNING,
            reason="tier too high",
            roles=("agent",),
            max_tier="STANDARD",
        )
    )

    ok = engine.decide_model(run_id=RUN, subject_id="a", role="agent", model="m", tier="FAST")
    assert ok.decision is Decision.PASS
    over = engine.decide_model(run_id=RUN, subject_id="a", role="agent", model="m", tier="FRONTIER")
    assert (over.decision, over.rule_id) == (Decision.WARNING, "TIER")


_TIGHT = ModelRule(
    id="TIGHT",
    decision=Decision.WARNING,
    reason="tightened allow-list",
    roles=("mira",),
    allowed_models=("frontier-a",),
)
_LOOSE = ModelRule(
    id="LOOSE",
    decision=Decision.WARNING,
    reason="base allow-list",
    roles=("mira",),
    allowed_models=("frontier-a", "frontier-b"),
)


@pytest.mark.parametrize(
    ("rules", "order"),
    [
        ((_TIGHT, _LOOSE), "overlay first, as Policy.merged_with prepends it"),
        ((_LOOSE, _TIGHT), "overlay last, which used to hand the model a silent PASS"),
    ],
)
def test_the_strictest_covering_rule_wins_whatever_the_order(
    rules: tuple[ModelRule, ...], order: str
) -> None:
    """Rule order does not decide a model selection; the strictest covering outcome does.

    ``decide_model`` returned on the first rule covering the role, so a rule that permitted the
    model shadowed one that refused it and the selection passed with nothing on the record. Only
    the prepended-overlay arrangement happened to be tested, which is exactly the arrangement the
    defect could not be seen in.
    """
    engine = _engine(*rules)

    decision = engine.decide_model(run_id=RUN, subject_id="a", role="mira", model="frontier-b")

    assert (decision.decision, decision.rule_id) == (Decision.WARNING, "TIGHT"), order
    # The permissive rule it outranked is named, not dropped.
    assert "LOOSE" in decision.reason


def test_a_permissive_rule_never_shadows_a_deny_list() -> None:
    """A rule permitting everything must not hand a denied model a PASS by being listed first."""
    permissive = ModelRule(
        id="M-LOOSE", decision=Decision.WARNING, reason="anything goes", roles=("agent",)
    )
    deny = ModelRule(
        id="M-DENY",
        decision=Decision.WARNING,
        reason="that model is denied",
        roles=("agent",),
        denied_models=("rogue",),
    )
    engine = _engine(permissive, deny)

    decision = engine.decide_model(run_id=RUN, subject_id="a", role="agent", model="rogue")

    assert (decision.decision, decision.rule_id) == (Decision.WARNING, "M-DENY")


def test_gate_records_a_substitution_warning(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)
    gate = Gate(engine, log)

    passed = gate.check_model(subject_id="agent_mira", role="mira", model="frontier-a")
    warned = gate.check_model(subject_id="agent_mira", role="mira", model="rogue-model")

    assert passed.decision is Decision.PASS
    assert warned.decision is Decision.WARNING
    # A model choice is evidence, not an authorization: the WARNING is recorded, not blocking.
    assert allows_execution(warned)
    assert [event.type for event in log.events()] == [
        EventType.POLICY_DECISION,
        EventType.POLICY_DECISION,
    ]
    assert log.verify().valid
