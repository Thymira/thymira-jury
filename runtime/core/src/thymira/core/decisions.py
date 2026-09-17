"""The HITL decision channel: analytical Q&A that never becomes an authorization.

This is the decision channel of ADR-0002 (line 49) and roadmap task ``RA-CORE-09``. An agent that
reaches an *analytical* decision it cannot make alone -- "which imputation strategy?", "drop or keep
this outlier?" -- raises a :class:`DecisionRequest`. A :class:`DecisionResolver` answers it and
traces both the question and the answer as ``agent.message`` events on the run's log, in one of
three modes:

- :attr:`DecisionMode.AUTOMATIC` -- code answers the question deterministically; no human, no model.
- :attr:`DecisionMode.HUMAN_CONFIRM` -- a human answers the question directly and code records it.
- :attr:`DecisionMode.LLM_PROPOSES_HUMAN_CONFIRMS` (FINAL) -- a model proposes an answer and a
  human confirms or overrides it; the human's answer is the resolution.

The channel is deliberately kept apart from the Policy Engine and the ``Gate``. A
:class:`DecisionResolution` is an *analytical answer*, never an authorization: it is not a
:class:`thymira.schemas.Decision` (the audit-disposition verdict), and this module never emits one.
The architecture invariant is "LLM proposes, code authorizes"; an analytical Q&A is neither the
proposal that authorizes nor the code that does. Authorization stays entirely with the
``policy.decision`` -> ``human.approval_requested`` -> ``human.approval`` sequence the ``Gate``
records -- this module imports neither.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import Field

from thymira.schemas import (
    Actor,
    ActorKind,
    EventSurface,
    EventType,
    Id,
    ThymiraModel,
    new_id,
)

if TYPE_CHECKING:
    from thymira.events import EventLog

_DECISION_CHANNEL = "decision"
"""The ``channel`` marker every decision ``agent.message`` carries.

It keeps these events distinct from a risk-classification ``agent.message`` (which the MIRA A15
control keys off ``payload["agent"] == "risk-classifier"``); the decision channel never sets that
key, so an analytical Q&A can never be mistaken for a risk classification.
"""


class DecisionMode(StrEnum):
    """How a :class:`DecisionResolver` reaches an analytical answer.

    The mode is recorded on every :class:`DecisionResolution` and its ``agent.message`` so an
    auditor can see whether -- and how -- a human was in the loop. None of the modes produces an
    authorization; the channel answers questions, it does not grant permission.
    """

    AUTOMATIC = "automatic"
    HUMAN_CONFIRM = "human_confirm"
    LLM_PROPOSES_HUMAN_CONFIRMS = "llm_proposes_human_confirms"


@dataclass(frozen=True, slots=True)
class AnalyticalAnswer:
    """An answer a resolver is given -- a chosen value and an optional, auditable rationale.

    ``rationale`` is never chain-of-thought: it is a short justification an auditor can read, not a
    model's private reasoning, and it is redacted with the rest of the payload before it is traced.

    Attributes:
        answer: The chosen answer to the analytical question.
        rationale: A short, non-reasoning justification, or ``None``.
    """

    answer: str
    rationale: str | None = None


class DecisionRequest(ThymiraModel):
    """An analytical decision an agent cannot make alone -- a question, not an authorization ask.

    The request is mode-agnostic: the same question can be answered automatically, by a human, or by
    a model-then-human, depending on which resolver handles it.
    """

    id: Id
    run_id: Id
    question: str = Field(min_length=1, description="the analytical question posed")
    asked_by: str = Field(
        min_length=1, description="id of the agent that reached the analytical decision"
    )
    options: tuple[str, ...] = Field(
        default=(), description="enumerated choices, when the decision is bounded"
    )
    context: dict[str, Any] = Field(
        default_factory=dict, description="supporting facts, redacted before they are traced"
    )


class DecisionResolution(ThymiraModel):
    """The analytical answer to a :class:`DecisionRequest`.

    This is deliberately *not* a :class:`thymira.schemas.Decision`. An analytical answer is never an
    authorization verdict, and no field here carries one: the decision channel resolves questions
    and the Policy Engine authorizes actions, and the two never cross (``RA-CORE-09``).
    """

    id: Id
    request_id: Id
    run_id: Id
    mode: DecisionMode
    answer: str = Field(min_length=1, description="the chosen answer")
    answered_by: Actor = Field(
        description="who answered: the system for automatic, or the human who confirmed"
    )
    rationale: str | None = Field(
        default=None, description="a short, non-reasoning justification, or None"
    )
    proposed_answer: str | None = Field(
        default=None,
        description="the model's proposal in llm_proposes_human_confirms mode, before confirmation",
    )


@runtime_checkable
class DecisionResolver(Protocol):
    """Answers a :class:`DecisionRequest`, tracing the Q&A -- and never authorizing anything.

    Every resolver is constructed with the run's :class:`~thymira.events.EventLog`, so ``resolve``
    takes only the request. The returned :class:`DecisionResolution` is an analytical answer, never
    a :class:`thymira.schemas.Decision`.
    """

    mode: DecisionMode

    def resolve(self, request: DecisionRequest) -> DecisionResolution:
        """Answer ``request``, trace the question and the answer, and return the resolution."""
        ...


AutomaticDecider = Callable[[DecisionRequest], AnalyticalAnswer]
"""Deterministic answer function for :class:`AutomaticResolver` -- code, never a model."""

HumanResponder = Callable[[DecisionRequest], AnalyticalAnswer]
"""A human's direct answer to a request, injected into :class:`HumanConfirmResolver`."""

Proposer = Callable[[DecisionRequest], AnalyticalAnswer]
"""A model's proposed answer for :class:`LlmProposesHumanConfirmsResolver`."""

HumanConfirmer = Callable[[DecisionRequest, AnalyticalAnswer], AnalyticalAnswer]
"""A human confirming or overriding a proposal; returns the answer the human stands behind."""


class AutomaticResolver:
    """Answers deterministically in code -- no human, no model -- and traces the Q&A.

    The injected ``decide`` callable computes the answer from the request alone; the resolution is
    recorded with the system as the answerer.
    """

    mode: DecisionMode = DecisionMode.AUTOMATIC

    def __init__(self, event_log: EventLog, decide: AutomaticDecider) -> None:
        """Bind the resolver to a run's event log and its deterministic answer function.

        Args:
            event_log: The run's log; the question and answer are traced here.
            decide: A pure function that computes the answer from the request.
        """
        self._log = event_log
        self._decide = decide

    def resolve(self, request: DecisionRequest) -> DecisionResolution:
        """Compute the answer, trace the question and answer, and return the resolution."""
        _require_same_run(self._log, request)
        _trace_question(self._log, request)
        answer = self._decide(request)
        resolution = DecisionResolution(
            id=new_id("decision"),
            request_id=request.id,
            run_id=request.run_id,
            mode=self.mode,
            answer=answer.answer,
            answered_by=Actor.system(),
            rationale=answer.rationale,
        )
        _trace_answer(self._log, resolution)
        return resolution


class HumanConfirmResolver:
    """Records a human's direct answer as the resolution -- the human decides, code only records.

    The injected ``respond`` callable obtains the human's answer (a console prompt, a web form, or a
    scripted answer in tests); the human is recorded as the answerer.
    """

    mode: DecisionMode = DecisionMode.HUMAN_CONFIRM

    def __init__(self, event_log: EventLog, respond: HumanResponder, *, human: Actor) -> None:
        """Bind the resolver to a run's event log, the human answerer, and how to reach them.

        Args:
            event_log: The run's log; the question and answer are traced here.
            respond: How the human's answer is obtained for a request.
            human: The human actor recorded as the answerer.

        Raises:
            ValueError: If ``human`` is not a :attr:`~thymira.schemas.ActorKind.HUMAN` actor.
        """
        if human.kind is not ActorKind.HUMAN:
            raise ValueError("human_confirm requires a human actor as the answerer")
        self._log = event_log
        self._respond = respond
        self._human = human

    def resolve(self, request: DecisionRequest) -> DecisionResolution:
        """Obtain the human's answer, trace the Q&A, and return the resolution."""
        _require_same_run(self._log, request)
        _trace_question(self._log, request)
        answer = self._respond(request)
        resolution = DecisionResolution(
            id=new_id("decision"),
            request_id=request.id,
            run_id=request.run_id,
            mode=self.mode,
            answer=answer.answer,
            answered_by=self._human,
            rationale=answer.rationale,
        )
        _trace_answer(self._log, resolution)
        return resolution


class LlmProposesHumanConfirmsResolver:
    """FINAL: a model proposes, a human confirms or overrides; the human's answer is the resolution.

    The proposal is recorded on the answer event (``proposed_answer``) so an auditor sees both what
    the model offered and what the human decided, but only the human's answer is authoritative. The
    proposal never becomes the resolution on its own -- the model proposes, the human answers.
    """

    mode: DecisionMode = DecisionMode.LLM_PROPOSES_HUMAN_CONFIRMS

    def __init__(
        self,
        event_log: EventLog,
        propose: Proposer,
        confirm: HumanConfirmer,
        *,
        human: Actor,
    ) -> None:
        """Bind the resolver to a run's event log, the model proposer, and the human confirmer.

        Args:
            event_log: The run's log; the question and answer are traced here.
            propose: How the model's proposed answer is obtained.
            confirm: How the human confirms or overrides the proposal.
            human: The human actor recorded as the answerer.

        Raises:
            ValueError: If ``human`` is not a :attr:`~thymira.schemas.ActorKind.HUMAN` actor.
        """
        if human.kind is not ActorKind.HUMAN:
            raise ValueError("llm_proposes_human_confirms requires a human actor as the answerer")
        self._log = event_log
        self._propose = propose
        self._confirm = confirm
        self._human = human

    def resolve(self, request: DecisionRequest) -> DecisionResolution:
        """Get the proposal, obtain the human's confirmation, trace the Q&A, and return it."""
        _require_same_run(self._log, request)
        _trace_question(self._log, request)
        proposal = self._propose(request)
        confirmation = self._confirm(request, proposal)
        resolution = DecisionResolution(
            id=new_id("decision"),
            request_id=request.id,
            run_id=request.run_id,
            mode=self.mode,
            answer=confirmation.answer,
            answered_by=self._human,
            rationale=confirmation.rationale,
            proposed_answer=proposal.answer,
        )
        _trace_answer(self._log, resolution)
        return resolution


def _require_same_run(event_log: EventLog, request: DecisionRequest) -> None:
    """Reject a request whose run does not match the log being traced to.

    Raises:
        ValueError: If ``request.run_id`` differs from ``event_log.run_id``.
    """
    if request.run_id != event_log.run_id:
        raise ValueError(
            f"decision request targets run {request.run_id!r}, "
            f"not the event log's {event_log.run_id!r}"
        )


def _trace_question(event_log: EventLog, request: DecisionRequest) -> None:
    """Append the question as one ``agent.message`` attributed to the asking agent."""
    event_log.append(
        EventType.AGENT_MESSAGE,
        Actor(kind=ActorKind.AGENT, id=request.asked_by),
        {
            "channel": _DECISION_CHANNEL,
            "phase": "question",
            "decision_request_id": request.id,
            "asked_by": request.asked_by,
            "question": request.question,
            "options": list(request.options),
            "context": dict(request.context),
            "text": _question_text(request),
        },
        subject_id=request.id,
        surface=EventSurface.MODEL_VISIBLE,
    )


def _trace_answer(event_log: EventLog, resolution: DecisionResolution) -> None:
    """Append the answer as one ``agent.message`` attributed to whoever answered."""
    payload: dict[str, Any] = {
        "channel": _DECISION_CHANNEL,
        "phase": "answer",
        "decision_request_id": resolution.request_id,
        "decision_resolution_id": resolution.id,
        "mode": resolution.mode.value,
        "answer": resolution.answer,
        "text": _answer_text(resolution),
    }
    if resolution.rationale is not None:
        payload["rationale"] = resolution.rationale
    if resolution.proposed_answer is not None:
        payload["proposed_answer"] = resolution.proposed_answer
    event_log.append(
        EventType.AGENT_MESSAGE,
        resolution.answered_by,
        payload,
        subject_id=resolution.request_id,
        surface=EventSurface.MODEL_VISIBLE,
    )


def _question_text(request: DecisionRequest) -> str:
    """Render the one-line question text a model sees on the surface."""
    if request.options:
        options = "; ".join(request.options)
        return f"{request.asked_by} asks: {request.question} (options: {options})"
    return f"{request.asked_by} asks: {request.question}"


def _answer_text(resolution: DecisionResolution) -> str:
    """Render the one-line answer text a model sees on the surface."""
    return f"decision [{resolution.mode.value}] answered: {resolution.answer}"


__all__ = [
    "AnalyticalAnswer",
    "AutomaticDecider",
    "AutomaticResolver",
    "DecisionMode",
    "DecisionRequest",
    "DecisionResolution",
    "DecisionResolver",
    "HumanConfirmResolver",
    "HumanConfirmer",
    "HumanResponder",
    "LlmProposesHumanConfirmsResolver",
    "Proposer",
]
