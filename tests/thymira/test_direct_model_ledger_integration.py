"""Integration tests for direct interview and MIRA-preflight model-request evidence.

Real: Core composition helpers, UsageLedger, event log, MIRA replay and preflight. Faked: only
the provider, through deterministic ``ScriptedProvider`` responses; no network or model key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.native_provider import native_provider as _native_provider
from thymira.agents import RequestLedger, instrument_provider
from thymira.agents.llm import LiteLLMProvider, ScriptedProvider
from thymira.core import (
    MiraSubgraph,
    SubgraphDeps,
    UsageLedger,
    build_risk_interview_model_context,
    default_activity_profile,
    initial_runtime_state,
)
from thymira.core.risk_interview_model import extract_facts, generate_question
from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import replay_request_ledger
from thymira.mira.preflight import load_default_pack
from thymira.policies import Gate, PolicyEngine, load_default_policy
from thymira.schemas import (
    ActivityProfile,
    Actor,
    EventType,
    ModelRoutePolicy,
    ResponseOutcome,
    Run,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRecordRepository
from thymira.tools import ToolManager, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind both direct-call roles to one deterministic allowed route."""
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "test-model")
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "test-model")


def _run() -> Run:
    """Build one Run for direct model-boundary integration tests."""
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Assess this bounded activity.",
        model_route_policy=TEST_ROUTE_POLICY,
    )


def _invalid_native_provider() -> LiteLLMProvider:
    """Build a LiteLLM provider over an offline gateway returning invalid JSON."""
    return _native_provider("not-json")


def _complete_profile(run: Run) -> ActivityProfile:
    """Build the smallest complete profile accepted by risk preflight."""
    return default_activity_profile(run).model_copy(
        update={
            "purpose": "Prioritise applications for a human reviewer.",
            "affected_population": "People submitting applications.",
            "decision_effect": "Changes review order, never the final decision.",
            "autonomy": "Recommendation only.",
            "human_oversight": "A reviewer can override every recommendation.",
            "jurisdiction": "ES",
            "data_categories": ("application_data",),
            "sensitive_attributes": (),
            "potential_consequences": ("A delayed review.",),
        }
    )


def test_interview_parse_failure_charges_usage_and_replays_once() -> None:
    """The production interview context records and charges one rejected response."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    usage_ledger = UsageLedger()
    context = build_risk_interview_model_context(
        event_log,
        Gate(PolicyEngine(load_default_policy()), event_log),
        usage_ledger,
        provider=ScriptedProvider([{"unexpected": []}]),
        route_policy=TEST_ROUTE_POLICY,
    )
    assert context is not None

    extraction, source = extract_facts(
        ("purpose",),
        {"purpose": "What is the concrete purpose?"},
        "No purpose is declared.",
        ("purpose",),
        default_activity_profile(run),
        context,
    )

    replay = replay_request_ledger(tuple(event_log.events()))
    assert extraction is None
    assert source == "fallback"
    assert usage_ledger.snapshot()["requests"] == 1
    assert sum(event.type is EventType.MODEL_REQUEST_RECORDED for event in event_log.events()) == 1
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert replay.in_flight == ()
    assert verify_events(event_log.events()).valid


def test_interview_preinstrumented_scripted_parse_failure_is_not_double_recorded() -> None:
    """An already wrapped ScriptedProvider remains one durable request boundary."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    usage_ledger = UsageLedger()
    request_ledger = RequestLedger(event_log)
    provider = instrument_provider(
        ScriptedProvider([{"unexpected": []}]),
        request_ledger,
        owner_id="risk-interview",
    )
    context = build_risk_interview_model_context(
        event_log,
        Gate(PolicyEngine(load_default_policy()), event_log),
        usage_ledger,
        provider=provider,
        request_ledger=request_ledger,
        route_policy=TEST_ROUTE_POLICY,
    )
    assert context is not None

    extraction, source = extract_facts(
        ("purpose",),
        {"purpose": "What is the concrete purpose?"},
        "No purpose is declared.",
        ("purpose",),
        default_activity_profile(run),
        context,
    )

    replay = replay_request_ledger(tuple(event_log.events()))
    assert extraction is None
    assert source == "fallback"
    assert usage_ledger.snapshot()["requests"] == 1
    assert sum(event.type is EventType.MODEL_REQUEST_RECORDED for event in event_log.events()) == 1
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert replay.in_flight == ()
    assert verify_events(event_log.events()).valid


def test_interview_native_litellm_parse_failure_is_not_double_recorded() -> None:
    """The native gateway ledger remains the sole writer for an invalid LiteLLM response."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    usage_ledger = UsageLedger()
    context = build_risk_interview_model_context(
        event_log,
        Gate(PolicyEngine(load_default_policy()), event_log),
        usage_ledger,
        provider=_invalid_native_provider(),
        request_ledger=RequestLedger(event_log),
        route_policy=TEST_ROUTE_POLICY,
    )
    assert context is not None

    extraction, source = extract_facts(
        ("purpose",),
        {"purpose": "What is the concrete purpose?"},
        "No purpose is declared.",
        ("purpose",),
        default_activity_profile(run),
        context,
    )

    replay = replay_request_ledger(tuple(event_log.events()))
    assert extraction is None
    assert source == "fallback"
    assert usage_ledger.snapshot()["requests"] == 1
    assert usage_ledger.snapshot()["tokens"] == 3
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert replay.in_flight == ()
    assert verify_events(event_log.events()).valid


@pytest.mark.parametrize("provider_kind", ["scripted", "litellm"])
def test_independent_interview_calls_each_start_at_attempt_one(provider_kind: str) -> None:
    """Extraction and question generation are independent, not a fabricated retry chain."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    usage_ledger = UsageLedger()
    provider = (
        ScriptedProvider([{"facts": []}, "What is the concrete purpose?"])
        if provider_kind == "scripted"
        else _native_provider('{"facts": []}', "What is the concrete purpose?")
    )
    context = build_risk_interview_model_context(
        event_log,
        Gate(PolicyEngine(load_default_policy()), event_log),
        usage_ledger,
        provider=provider,
        request_ledger=RequestLedger(event_log),
        route_policy=TEST_ROUTE_POLICY,
    )
    assert context is not None
    profile = default_activity_profile(run)

    extraction, extraction_source = extract_facts(
        ("purpose",),
        {"purpose": "What is the concrete purpose?"},
        "No purpose is declared.",
        ("purpose",),
        profile,
        context,
    )
    question, question_source = generate_question(
        "purpose",
        "What is the concrete purpose?",
        ("purpose",),
        profile,
        context,
    )

    replay = replay_request_ledger(tuple(event_log.events()))
    assert extraction is not None
    assert extraction.facts == ()
    assert extraction_source == "model"
    assert question == "What is the concrete purpose?"
    assert question_source == "model"
    assert usage_ledger.snapshot()["requests"] == 2
    assert len(replay.requests) == 2
    assert len(replay.responses) == 2
    assert [item.request.attempt for item in replay.requests] == [1, 1]
    assert all(item.request.retry_of is None for item in replay.requests)
    assert all(item.outcome is ResponseOutcome.SUCCESS for item in replay.responses)
    assert replay.in_flight == ()
    assert verify_events(event_log.events()).valid


def test_interview_direct_call_does_not_steal_a_native_provider_ledger() -> None:
    """A run-local interview binding leaves a provider's prior ledger binding untouched."""
    original_run = _run()
    interview_run = _run()
    original_log = InMemoryEventLog(original_run.id)
    interview_log = InMemoryEventLog(interview_run.id)
    provider = _native_provider('{"facts": []}', "original-ledger-response")
    provider.attach_request_ledger(RequestLedger(original_log), owner_id="original-owner")
    context = build_risk_interview_model_context(
        interview_log,
        Gate(PolicyEngine(load_default_policy()), interview_log),
        UsageLedger(),
        provider=provider,
        request_ledger=RequestLedger(interview_log),
        route_policy=TEST_ROUTE_POLICY,
    )
    assert context is not None

    extraction, source = extract_facts(
        ("purpose",),
        {"purpose": "What is the concrete purpose?"},
        "No purpose is declared.",
        ("purpose",),
        default_activity_profile(interview_run),
        context,
    )
    response = provider.complete("Write through the provider's original ledger.")

    interview_replay = replay_request_ledger(tuple(interview_log.events()))
    original_replay = replay_request_ledger(tuple(original_log.events()))
    assert extraction is not None
    assert source == "model"
    assert response.text == "original-ledger-response"
    assert len(interview_replay.requests) == len(interview_replay.responses) == 1
    assert len(original_replay.requests) == len(original_replay.responses) == 1
    assert interview_replay.requests[0].request.attempt == 1
    assert interview_replay.requests[0].request.retry_of is None
    assert original_replay.requests[0].request.attempt == 1
    assert original_replay.requests[0].request.retry_of is None
    assert {owner.owner_id for owner in interview_replay.requests[0].request.owners} == {
        "risk-interview"
    }
    assert {owner.owner_id for owner in original_replay.requests[0].request.owners} == {
        "original-owner"
    }
    assert verify_events(interview_log.events()).valid
    assert verify_events(original_log.events()).valid


def test_mira_preflight_parse_failure_uses_the_injected_run_ledgers(tmp_path: Path) -> None:
    """MiraSubgraph wires both Core ledgers into its direct risk-model call."""
    run = _run()
    event_log = InMemoryEventLog(run.id)
    usage_ledger = UsageLedger()
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(PolicyEngine(load_default_policy()), event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=usage_ledger,
        request_ledger=RequestLedger(event_log),
    )
    mira = MiraSubgraph(
        run,
        _complete_profile(run),
        (load_default_pack("methodology-base"),),
        Actor.system(),
        provider=ScriptedProvider([{"risk_level": "low"}]),
    )

    mira.preflight(initial_runtime_state(run), deps=deps)

    replay = replay_request_ledger(tuple(event_log.events()))
    assert usage_ledger.snapshot()["requests"] == 1
    assert sum(event.type is EventType.MODEL_REQUEST_RECORDED for event in event_log.events()) == 1
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert replay.in_flight == ()
    assert verify_events(event_log.events()).valid


def test_mira_preflight_does_not_steal_a_native_provider_ledger(tmp_path: Path) -> None:
    """MIRA's direct risk call leaves a provider's prior ledger binding untouched."""
    original_run = _run()
    preflight_run = _run()
    original_log = InMemoryEventLog(original_run.id)
    preflight_log = InMemoryEventLog(preflight_run.id)
    provider = _native_provider('{"risk_level": "low"}', "original-ledger-response")
    provider.attach_request_ledger(RequestLedger(original_log), owner_id="original-owner")
    deps = SubgraphDeps(
        event_log=preflight_log,
        gate=Gate(PolicyEngine(load_default_policy()), preflight_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", preflight_run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
        request_ledger=RequestLedger(preflight_log),
    )
    mira = MiraSubgraph(
        preflight_run,
        _complete_profile(preflight_run),
        (load_default_pack("methodology-base"),),
        Actor.system(),
        provider=provider,
    )

    mira.preflight(initial_runtime_state(preflight_run), deps=deps)
    response = provider.complete("Write through the provider's original ledger.")

    preflight_replay = replay_request_ledger(tuple(preflight_log.events()))
    original_replay = replay_request_ledger(tuple(original_log.events()))
    assert response.text == "original-ledger-response"
    assert len(preflight_replay.requests) == len(preflight_replay.responses) == 1
    assert len(original_replay.requests) == len(original_replay.responses) == 1
    assert preflight_replay.responses[0].outcome is ResponseOutcome.PARSE_FAILURE
    assert original_replay.responses[0].outcome is ResponseOutcome.SUCCESS
    assert preflight_replay.requests[0].request.attempt == 1
    assert preflight_replay.requests[0].request.retry_of is None
    assert original_replay.requests[0].request.attempt == 1
    assert original_replay.requests[0].request.retry_of is None
    assert {owner.owner_id for owner in preflight_replay.requests[0].request.owners} == {
        "mira-risk-preflight"
    }
    assert {owner.owner_id for owner in original_replay.requests[0].request.owners} == {
        "original-owner"
    }
    assert verify_events(preflight_log.events()).valid
    assert verify_events(original_log.events()).valid
