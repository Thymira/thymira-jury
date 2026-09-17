"""Tests for the HITL decision channel (RA-CORE-09).

The decision channel answers analytical questions and traces every Q&A as ``agent.message``
events; it is kept strictly separate from Gate authorization and never emits a
``schemas.Decision``.
"""

from __future__ import annotations

import pytest

from thymira.core import (
    AnalyticalAnswer,
    AutomaticResolver,
    DecisionMode,
    DecisionRequest,
    DecisionResolution,
    DecisionResolver,
    HumanConfirmResolver,
    LlmProposesHumanConfirmsResolver,
)
from thymira.events import InMemoryEventLog
from thymira.schemas import Actor, ActorKind, Decision, EventSurface, EventType, new_id


def _request(run_id: str, *, question: str = "Which imputation strategy?") -> DecisionRequest:
    """Build a bounded imputation decision request for ``run_id``."""
    return DecisionRequest(
        id=new_id("decision"),
        run_id=run_id,
        question=question,
        asked_by=new_id("agent"),
        options=("mean", "median", "drop"),
        context={"skew": 2.4},
    )


def _human() -> Actor:
    """A human answerer."""
    return Actor(kind=ActorKind.HUMAN, id="user_analyst", role="data-scientist", authenticated=True)


def _messages(log: InMemoryEventLog) -> list:
    """The ``agent.message`` events on ``log``, in order."""
    return [event for event in log.events() if event.type is EventType.AGENT_MESSAGE]


def test_automatic_resolver_answers_and_traces_the_qa() -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    resolver = AutomaticResolver(
        log,
        lambda _request: AnalyticalAnswer(answer="median", rationale="the feature is right-skewed"),
    )
    request = _request(run_id)

    resolution = resolver.resolve(request)

    assert resolution.answer == "median"
    assert resolution.rationale == "the feature is right-skewed"
    assert resolution.mode is DecisionMode.AUTOMATIC
    assert resolution.request_id == request.id
    assert resolution.answered_by.kind is ActorKind.SYSTEM

    # The question and the answer are both traced as agent.message events.
    question_event, answer_event = _messages(log)
    assert question_event.payload["phase"] == "question"
    assert question_event.payload["channel"] == "decision"
    assert question_event.payload["decision_request_id"] == request.id
    assert question_event.payload["question"] == "Which imputation strategy?"
    assert question_event.actor.id == request.asked_by
    assert question_event.actor.kind is ActorKind.AGENT
    assert answer_event.payload["phase"] == "answer"
    assert answer_event.payload["answer"] == "median"
    assert answer_event.payload["mode"] == DecisionMode.AUTOMATIC.value
    assert answer_event.subject_id == request.id

    # The traced Q&A leaves the hash chain intact.
    assert log.verify().valid


def test_human_confirm_resolver_records_the_human_answer_as_an_event() -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    human = _human()
    resolver = HumanConfirmResolver(
        log,
        lambda _request: AnalyticalAnswer(answer="drop", rationale="confirmed data-entry error"),
        human=human,
    )
    request = _request(run_id, question="Drop or keep this outlier?")

    resolution = resolver.resolve(request)

    assert resolution.answer == "drop"
    assert resolution.mode is DecisionMode.HUMAN_CONFIRM
    assert resolution.answered_by == human

    # The human's answer is recorded as an agent.message attributed to the human.
    answer_event = _messages(log)[-1]
    assert answer_event.actor == human
    assert answer_event.payload["phase"] == "answer"
    assert answer_event.payload["answer"] == "drop"
    assert answer_event.payload["rationale"] == "confirmed data-entry error"
    assert log.verify().valid


def test_llm_proposes_human_confirms_records_proposal_and_human_answer() -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    human = _human()

    def propose(_request: DecisionRequest) -> AnalyticalAnswer:
        return AnalyticalAnswer(answer="mean", rationale="model default")

    def confirm(_request: DecisionRequest, _proposal: AnalyticalAnswer) -> AnalyticalAnswer:
        # The human overrides the model's proposal.
        return AnalyticalAnswer(answer="median", rationale="analyst overrode the proposal")

    resolver = LlmProposesHumanConfirmsResolver(log, propose, confirm, human=human)
    request = _request(run_id)

    resolution = resolver.resolve(request)

    assert resolution.mode is DecisionMode.LLM_PROPOSES_HUMAN_CONFIRMS
    assert resolution.proposed_answer == "mean"
    assert resolution.answer == "median"
    assert resolution.answered_by == human

    answer_event = _messages(log)[-1]
    assert answer_event.actor == human
    assert answer_event.payload["proposed_answer"] == "mean"
    assert answer_event.payload["answer"] == "median"
    assert log.verify().valid


def test_resolution_is_never_a_schemas_decision() -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    resolver = AutomaticResolver(log, lambda _request: AnalyticalAnswer(answer="median"))

    resolution = resolver.resolve(_request(run_id))

    # The resolver output type is never a schemas.Decision (the audit-disposition verdict).
    assert type(resolution) is DecisionResolution
    assert type(resolution) is not Decision
    assert not isinstance(resolution, Decision)
    # No field on the resolution carries an authorization verdict.
    values = [getattr(resolution, name) for name in type(resolution).model_fields]
    assert all(not isinstance(value, Decision) for value in values)
    # The channel never records an authorization event.
    recorded_types = {event.type for event in log.events()}
    assert EventType.POLICY_DECISION not in recorded_types
    assert EventType.HUMAN_APPROVAL not in recorded_types
    assert recorded_types == {EventType.AGENT_MESSAGE}


def test_decision_agent_message_never_looks_like_a_risk_classification() -> None:
    # The A15 MIRA control keys off payload["agent"] == "risk-classifier"; the decision channel
    # must never set that key, so an analytical Q&A cannot be mistaken for a risk classification.
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    resolver = AutomaticResolver(log, lambda _request: AnalyticalAnswer(answer="median"))

    resolver.resolve(_request(run_id))

    for event in _messages(log):
        assert event.payload.get("agent") != "risk-classifier"
        assert event.payload["channel"] == "decision"
        assert event.surface is EventSurface.MODEL_VISIBLE


def test_resolvers_satisfy_the_decision_resolver_protocol() -> None:
    log = InMemoryEventLog(new_id("run"))
    automatic = AutomaticResolver(log, lambda _request: AnalyticalAnswer(answer="median"))
    human = HumanConfirmResolver(
        log, lambda _request: AnalyticalAnswer(answer="drop"), human=_human()
    )

    assert isinstance(automatic, DecisionResolver)
    assert isinstance(human, DecisionResolver)
    assert automatic.mode is DecisionMode.AUTOMATIC
    assert human.mode is DecisionMode.HUMAN_CONFIRM


def test_human_confirm_rejects_a_non_human_answerer() -> None:
    log = InMemoryEventLog(new_id("run"))
    with pytest.raises(ValueError, match="human actor"):
        HumanConfirmResolver(
            log, lambda _request: AnalyticalAnswer(answer="drop"), human=Actor.system()
        )


def test_resolve_rejects_a_request_for_another_run() -> None:
    log = InMemoryEventLog(new_id("run"))
    resolver = AutomaticResolver(log, lambda _request: AnalyticalAnswer(answer="median"))
    other_run_request = _request(new_id("run"))

    with pytest.raises(ValueError, match="not the event log"):
        resolver.resolve(other_run_request)

    # A rejected request traces nothing onto the wrong run's log.
    assert _messages(log) == []
