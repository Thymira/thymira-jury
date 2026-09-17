"""Unit and composition tests for MIRA's model-assisted inherent-risk assessment."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from thymira.agents import RequestLedger
from thymira.agents.llm import LLMResponse, ScriptedProvider
from thymira.core import (
    MiraControlPlane,
    MiraSubgraph,
    RunController,
    RuntimeState,
    RunTransitionKind,
    SubgraphDeps,
    UsageLedger,
    initial_runtime_state,
)
from thymira.core.control_plane import RunEventLog
from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import replay_request_ledger
from thymira.mira.preflight import (
    BaseRiskEvaluator,
    BaseRiskMethod,
    RiskAssessmentModelContext,
    RiskRule,
    load_default_pack,
    risk_model,
)
from thymira.policies import Gate, PolicyEngine, load_default_policy
from thymira.schemas import (
    ActivityProfile,
    Actor,
    EventType,
    ModelRoutePolicy,
    ResponseOutcome,
    RiskLevel,
    Run,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRecordRepository, LocalRunStore
from thymira.tools import ToolManager, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind direct MIRA risk calls to one deterministic route."""
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "test-model")


def _run() -> Run:
    """Build one Run suitable for standalone MIRA assessment tests."""
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Assess the activity.",
        model_route_policy=TEST_ROUTE_POLICY,
    )


def _profile(run_id: str) -> ActivityProfile:
    """Build a complete, clear profile with no sensitive attributes declared."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run_id,
        purpose="Prioritise applications for a human reviewer.",
        affected_population="People submitting applications.",
        decision_effect="Changes review order, never the final decision.",
        autonomy="Recommendation only.",
        human_oversight="A reviewer can override every recommendation.",
        jurisdiction="ES",
        data_categories=("application_data",),
        sensitive_attributes=(),
        potential_consequences=("A delayed review.",),
    )


def _method() -> BaseRiskMethod:
    """Build a reviewed taxonomy whose rule cannot match the empty sensitive-attribute set."""
    return BaseRiskMethod(
        id="reviewed-context-risk",
        version="1.0",
        required_profile_facts=("data_categories", "potential_consequences"),
        rules=(
            RiskRule(
                id="RISK-CONTEXT-LOW",
                category="contextual_low_risk",
                risk_level=RiskLevel.LOW,
                sensitive_attributes_any=("context_signal",),
            ),
        ),
    )


def test_model_assesses_the_complete_profile_against_reviewed_pairs() -> None:
    """A valid MIRA judgement can select a reviewed pair when deterministic matching cannot."""
    run = _run()
    profile = _profile(run.id).model_copy(
        update={
            "purpose": "Shared context document.",
            "affected_population": "Shared context document.",
            "decision_effect": "Shared context document.",
            "autonomy": "Shared context document.",
            "human_oversight": "Shared context document.",
        }
    )
    event_log = InMemoryEventLog(run.id)
    provider = ScriptedProvider(
        [
            {
                "risk_level": "low",
                "activity_category": "contextual_low_risk",
                "confidence": 0.91,
                "justification": (
                    "The declared oversight and recommendation-only use fit the reviewed pair."
                ),
            }
        ]
    )
    context = RiskAssessmentModelContext(
        provider=provider, event_log=event_log, route_policy=TEST_ROUTE_POLICY
    )

    assessment = BaseRiskEvaluator(
        _method(),
        Actor.system(),
        model_context=context,
    ).evaluate(profile, assessment_id=new_id("assessment"), assessed_at=NOW)

    assert assessment.risk_level is RiskLevel.LOW
    assert assessment.activity_category == "contextual_low_risk"
    assert assessment.confidence == 0.91
    assert assessment.missing_information == ()
    selected = [event for event in event_log.events() if event.type is EventType.MODEL_SELECTED]
    assert len(selected) == 1
    assert selected[0].payload["role"] == "mira"
    assert selected[0].payload["task"] == "classify"
    assert selected[0].payload["role"] != "agent"
    assert provider.calls[0]["prompt"].count("Shared context document.") == 5
    assert verify_events(event_log.events()).valid


def test_model_failure_preserves_the_previous_deterministic_fallback() -> None:
    """An exhausted scripted provider leaves an unmatched profile UNKNOWN with zero confidence."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    assessment = BaseRiskEvaluator(
        _method(),
        Actor.system(),
        model_context=RiskAssessmentModelContext(
            provider=ScriptedProvider([]),
            event_log=event_log,
            route_policy=TEST_ROUTE_POLICY,
        ),
    ).evaluate(_profile(run.id), assessment_id=new_id("assessment"), assessed_at=NOW)

    assert assessment.risk_level is RiskLevel.UNKNOWN
    assert assessment.activity_category == "unknown"
    assert assessment.confidence == 0.0
    assert assessment.missing_information == ("sensitive_attributes",)
    assert assessment.justification == (
        "No reviewed base-risk rule matches the declared sensitive attributes."
    )


def test_invalid_structured_risk_judgement_is_charged_before_fallback() -> None:
    """An invalid returned judgement still costs one request before deterministic fallback."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    recorded: list[LLMResponse] = []
    assessment = BaseRiskEvaluator(
        _method(),
        Actor.system(),
        model_context=RiskAssessmentModelContext(
            provider=ScriptedProvider([{"risk_level": "low"}]),
            event_log=event_log,
            record_model_usage=recorded.append,
            request_ledger=RequestLedger(event_log),
            route_policy=TEST_ROUTE_POLICY,
        ),
    ).evaluate(_profile(run.id), assessment_id=new_id("assessment"), assessed_at=NOW)

    assert assessment.risk_level is RiskLevel.UNKNOWN
    assert assessment.confidence == 0.0
    assert len(recorded) == 1
    assert recorded[0].metadata["schema"] == "RiskJudgment"
    replay = replay_request_ledger(tuple(event_log.events()))
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert replay.in_flight == ()


def test_mira_risk_prompt_and_system_scrub_credentials_before_provider_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight excludes a known credential before both the provider and durable request log."""
    secret = "sk-aB3dEf0gHi1jKl2mNo3pQr4sT5u"  # noqa: S105  # fixture credential
    marker = "[REDACTED:CREDENTIAL]"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(risk_model, "_RISK_SYSTEM_PROMPT", f"Assess using {secret}")
    run = _run()
    event_log = InMemoryEventLog(run.id)
    provider = ScriptedProvider(
        [
            {
                "risk_level": "low",
                "activity_category": "contextual_low_risk",
                "confidence": 0.91,
                "justification": "The reviewed pair applies.",
            }
        ]
    )
    profile = _profile(run.id).model_copy(update={"purpose": f"Assess {secret}"})

    BaseRiskEvaluator(
        _method(),
        Actor.system(),
        model_context=RiskAssessmentModelContext(
            provider=provider,
            event_log=event_log,
            request_ledger=RequestLedger(event_log),
            route_policy=TEST_ROUTE_POLICY,
        ),
    ).evaluate(profile, assessment_id=new_id("assessment"), assessed_at=NOW)

    assert len(provider.calls) == 1
    assert secret not in str(provider.calls)
    assert marker in provider.calls[0]["prompt"]
    assert provider.calls[0]["system"] == f"Assess using {marker}"
    events = event_log.events()
    assert all(secret not in event.model_dump_json() for event in events)
    ledger_inputs = [
        event
        for event in events
        if event.type
        in {
            EventType.MODEL_INPUT_HEADER_REVISED,
            EventType.MODEL_INPUT_SURFACE_UPDATED,
            EventType.MODEL_REQUEST_RECORDED,
        }
    ]
    assert ledger_inputs
    assert all(marker in event.model_dump_json() for event in ledger_inputs)


@pytest.mark.parametrize(
    ("route_policy", "reason"),
    [
        (None, "route_policy_unavailable"),
        (
            ModelRoutePolicy.from_routes(("other-model",), authority="code-owned-tests"),
            "route_not_allowed",
        ),
    ],
)
def test_mira_risk_route_denial_precedes_gate_and_provider(
    route_policy: ModelRoutePolicy | None,
    reason: str,
) -> None:
    """Preflight falls back without crossing Gate or provider when its route is unauthorized."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    provider = ScriptedProvider(
        [
            {
                "risk_level": "low",
                "activity_category": "contextual_low_risk",
                "confidence": 0.9,
                "justification": "This response must remain unused.",
            }
        ]
    )
    gate_calls: list[str] = []

    result = risk_model.classify_risk(
        _profile(run.id),
        _method(),
        RiskAssessmentModelContext(
            provider=provider,
            event_log=event_log,
            before_model_call=lambda _choice: gate_calls.append("gate"),
            route_policy=route_policy,
        ),
    )

    assert result is None
    assert provider.calls == []
    assert gate_calls == []
    assert [event.type for event in event_log.events()] == [
        EventType.MODEL_SELECTED,
        EventType.MODEL_ROUTE_DENIED,
    ]
    assert event_log.events()[-1].payload["reason"] == reason


def test_low_confidence_model_assessment_requests_human_approval_once(
    tmp_path: Path,
) -> None:
    """MIRA records UNKNOWN and asks for review without an unanswerable retry loop."""
    run = _run()
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    policy = PolicyEngine(load_default_policy())
    event_log = RunEventLog(store, run.id)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(policy, event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts" / run.id, run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    control_plane = MiraControlPlane(store, policy)
    provider = ScriptedProvider(
        [
            {
                "risk_level": "low",
                "activity_category": "declared_no_sensitive_data",
                "confidence": 0.42,
                "missing_information": ["human_oversight"],
                "justification": "The oversight description is ambiguous.",
            }
        ]
    )
    mira = MiraSubgraph(
        run,
        _profile(run.id),
        (load_default_pack("methodology-base"),),
        Actor.system(),
        provider=provider,
        control_plane=control_plane,
        now=lambda: NOW,
    )

    state: RuntimeState = initial_runtime_state(run)
    mira.preflight(state, deps=deps)

    current = control_plane.controller.current_state(run.id).state
    assert current.condition.value == "waiting"
    assert current.wait_reason is not None
    assert current.wait_reason.value == "approval"
    assessment_events = [
        event for event in event_log.events() if event.type is EventType.RISK_ASSESSMENT_RECORDED
    ]
    assert len(assessment_events) == 1
    assessment = assessment_events[0].payload["risk_assessment"]
    assert assessment["risk_level"] == "unknown"
    assert assessment["confidence"] == 0.42
    transitions = [
        event for event in event_log.events() if event.type is EventType.RUN_TRANSITIONED
    ]
    assert not any(
        isinstance(event.payload.get("intent"), dict)
        and event.payload["intent"].get("action_kind") == "request_information"
        for event in transitions
    )
    approval_requests = [
        event for event in event_log.events() if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    ]
    assert len(approval_requests) == 1
    assert (
        approval_requests[0].payload["summary"].startswith("MIRA could not classify inherent risk")
    )
    assert verify_events(event_log.events()).valid
