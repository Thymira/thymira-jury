"""Minimal, event-backed intake for the activity profile that governs one Run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.agents import RiskFacts, classify_risk
from thymira.core.execution_review import INTERVIEW_LIMIT_RESUME_TARGET
from thymira.core.risk_interview_model import (
    RiskInterviewModelContext,
    SufficiencyJudgment,
    extract_facts,
    generate_follow_up,
    generate_question,
    has_declared_value,
    is_missing_text,
    judge_sufficiency,
    normalise,
)
from thymira.events import canonical_json
from thymira.policies import RiskProfile
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    ActivityProfile,
    Actor,
    ActorKind,
    Decision,
    EventType,
    Evidence,
    RiskAssessment,
    approval_names_decision,
    new_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents.llm import LLMProvider
    from thymira.agents.llm.base import LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.core.control_plane import MiraControlPlane
    from thymira.events import EventLog
    from thymira.schemas import Event, ModelRoutePolicy, Run
    from thymira.state import LocalRunStore


INTERVIEW_FIELDS: tuple[str, ...] = (
    "purpose",
    "affected_population",
    "decision_effect",
    "autonomy",
    "human_oversight",
    "jurisdiction",
    "data_categories",
    "sensitive_attributes",
    "potential_consequences",
)
"""The activity facts required before THY may begin work."""

ANSWERABLE_RISK_FIELDS: tuple[str, ...] = (
    *INTERVIEW_FIELDS,
    "intended_use",
    "decision_role",
    "deployment_context",
)
"""The only names the classifier may keep as missing information: the interview's nine facts
plus the three downstream-context aliases the classifier prompt already presents for them."""

MAX_INTERVIEW_QUESTIONS = 12
"""Bound repeated unanswered facts before escalating the Run to a human review."""

MAX_INTERVIEW_QUESTIONS_PER_FIELD = 3
"""Bound repeated model-rejected human answers for one fact before review."""

_QUESTIONS: dict[str, str] = {
    "purpose": "What is the concrete purpose of this activity?",
    "affected_population": (
        "Which natural persons could be affected? Answer 'none' only when no natural person can "
        "be affected."
    ),
    "decision_effect": (
        "How can this activity influence a decision about a person or organisation?"
    ),
    "autonomy": "What can the system do autonomously, without a human decision?",
    "human_oversight": "What human oversight can review, change, or stop the activity?",
    "jurisdiction": "Which jurisdiction governs this activity?",
    "data_categories": "Which categories of data will be processed? List them, or answer 'none'.",
    "sensitive_attributes": (
        "Which sensitive attributes will be processed? List them, or answer 'none'."
    ),
    "potential_consequences": (
        "What potential consequences could an incorrect result have? List them, or answer 'none'."
    ),
}

_NO_VALUE_MARKERS = frozenset({"none", "no", "ninguno", "ninguna", "ningun"})
_MAX_REVIEW_SUMMARY = "Risk interview reached its question limit; human review is required."
_CONTEXT_DOCUMENT_REF = ".thymira/context.md"
_PROMPT_REF = "run.prompt"


class RiskInterviewError(ValueError):
    """Base error for an invalid activity-profile interview action."""


class RiskInterviewNotPendingError(RiskInterviewError):
    """Raised when a submitted answer does not target a current question."""


@dataclass(frozen=True, slots=True)
class PendingRiskQuestion:
    """One event-backed question a caller may answer for the current profile."""

    event_id: str
    field: str
    question: str
    profile_id: str
    profile_version: int
    question_number: int


@dataclass(frozen=True, slots=True)
class RiskInterviewStatus:
    """The current profile and exactly one pending question, when one exists."""

    profile: ActivityProfile
    pending_question: PendingRiskQuestion | None
    requires_human_review: bool


def _closes_review(event: Event) -> bool:
    """Whether an approval event retires the bounded interview review request."""
    if not isinstance(event.payload.get("approved"), bool):
        return False
    if event.actor.kind is ActorKind.SYSTEM:
        return True
    return (
        event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
    )


class RiskInterviewService:
    """Create, revise, and classify one immutable Run activity profile from its event log."""

    def __init__(
        self,
        store: LocalRunStore,
        control_plane: MiraControlPlane,
        *,
        max_questions: int = MAX_INTERVIEW_QUESTIONS,
        model_context_factory: Callable[[str], RiskInterviewModelContext | None] | None = None,
    ) -> None:
        if max_questions < 1:
            raise ValueError("max_questions must be at least 1")
        self._store = store
        self._control_plane = control_plane
        self._max_questions = max_questions
        self._model_context_factory = model_context_factory

    def begin(
        self,
        run: Run,
        initial_profile: ActivityProfile,
        *,
        context_document: str | None = None,
        provider: LLMProvider | None = None,
        event_log: EventLog | None = None,
        before_model_selection: Callable[[], None] | None = None,
        before_model_call: Callable[[ModelChoice], None] | None = None,
        record_model_usage: Callable[[LLMResponse], None] | None = None,
        request_ledger: RequestLedger | None = None,
        route_policy: ModelRoutePolicy | None = None,
    ) -> RiskInterviewStatus:
        """Persist the profile, fill it from known context, and ask one missing fact if needed.

        The fixed interview field list remains authoritative. Before the first question, one
        bounded model call may state the facts that the Run prompt and the project context
        document settle with certainty; every other field is asked to a human. A failed or
        absent model leaves every field for the ordinary interview.
        """
        profile = self._profile(run, initial_profile)
        model_context = self._resolve_model_context(
            run,
            provider=provider,
            event_log=event_log,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            request_ledger=request_ledger,
            route_policy=route_policy,
        )
        if profile.version == 1 and self._question_count(run.id) == 0:
            profile = self._apply_context(run, profile, context_document, model_context)
        missing = _missing_fields(profile)
        if not missing:
            return RiskInterviewStatus(profile, None, False)

        pending = self._pending_question(run.id)
        if pending is not None:
            self._ensure_waiting_for_information(run.id, pending)
            return RiskInterviewStatus(profile, pending, False)
        field = missing[0]
        if (
            self._question_count(run.id) >= self._max_questions
            or _field_question_count(self._store.events(run.id), field)
            >= MAX_INTERVIEW_QUESTIONS_PER_FIELD
        ):
            self._escalate_question_limit(run.id)
            return RiskInterviewStatus(profile, None, True)

        question, question_source = generate_question(
            field,
            _QUESTIONS[field],
            INTERVIEW_FIELDS,
            profile,
            model_context,
        )
        pending = self._request_question(
            run,
            profile,
            field,
            question,
            question_source=question_source,
        )
        self._ensure_waiting_for_information(run.id, pending)
        return RiskInterviewStatus(profile, pending, False)

    def status(self, run: Run, initial_profile: ActivityProfile) -> RiskInterviewStatus:
        """Return the persisted profile and its question without mutating the Run."""
        profile = self.current_profile(run, initial_profile)
        missing = _missing_fields(profile)
        pending = self._pending_question(run.id)
        field_limit_reached = (
            bool(missing)
            and _field_question_count(self._store.events(run.id), missing[0])
            >= MAX_INTERVIEW_QUESTIONS_PER_FIELD
        )
        return RiskInterviewStatus(
            profile,
            pending,
            pending is None
            and bool(missing)
            and (self._question_count(run.id) >= self._max_questions or field_limit_reached)
            and not self._limit_review_answered(run.id),
        )

    def has_pending_question(self, run_id: str) -> bool:
        """Return whether an unanswered fixed-field question exists for this Run.

        `resume` uses this to tell the two producers of a `wait_reason: information` park apart:
        this interview's own question (only `answer` may clear it) from MIRA preflight's
        request for context on an uncertain inherent-risk judgement, which carries no question
        event at all and so can never satisfy this check -- `resume` may re-attempt it instead.
        """
        return self._pending_question(run_id) is not None

    def current_profile(self, run: Run, initial_profile: ActivityProfile) -> ActivityProfile:
        """Return the latest profile or the supplied initial profile without recording either."""
        if initial_profile.run_id != run.id:
            raise ValueError("initial activity profile must belong to the Run")
        return self._recorded_profile(run.id) or initial_profile

    def answer(
        self,
        run: Run,
        *,
        answer: str,
        actor: Actor,
        provider: LLMProvider | None = None,
        event_log: EventLog | None = None,
        before_model_selection: Callable[[], None] | None = None,
        before_model_call: Callable[[ModelChoice], None] | None = None,
        record_model_usage: Callable[[LLMResponse], None] | None = None,
        request_ledger: RequestLedger | None = None,
        route_policy: ModelRoutePolicy | None = None,
    ) -> ActivityProfile:
        """Judge and persist one answer, returning the current immutable profile version."""
        value = answer.strip()
        if not value:
            raise ValueError("an interview answer must not be empty")
        profile = self._recorded_profile(run.id)
        if profile is None:
            raise RiskInterviewNotPendingError(
                f"run {run.id!r} has no persisted activity profile to update"
            )
        question = self._pending_question(run.id)
        if question is None:
            message = f"run {run.id!r} has no pending risk-interview question"
            raise RiskInterviewNotPendingError(message)
        if question.profile_id != profile.id or question.profile_version != profile.version:
            raise RiskInterviewNotPendingError(
                f"run {run.id!r} question does not match its current activity profile"
            )

        model_context = self._resolve_model_context(
            run,
            provider=provider,
            event_log=event_log,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            request_ledger=request_ledger,
            route_policy=route_policy,
        )
        judgment, judgment_source = judge_sufficiency(
            question.field,
            question.question,
            value,
            INTERVIEW_FIELDS,
            profile,
            model_context,
        )
        if judgment is None:
            # Existing callers without a provider retain the original answer semantics. A human
            # answer must never become impossible merely because the optional evaluator is down.
            judgment = SufficiencyJudgment(
                sufficient=True,
                reason="No interview evaluator was available; the live human answer was retained.",
            )
            judgment_source = "fallback"

        if judgment.sufficient:
            profile_id = new_id("profile")
            answer_event = self._append_answer(
                run,
                field=question.field,
                question=question.question,
                question_event_id=question.event_id,
                answer=value,
                actor=actor,
                profile_id=profile_id,
                profile_version=profile.version + 1,
                answer_source="live_human",
                source_ref=None,
                judgment=judgment,
                judgment_source=judgment_source,
            )
            updated = _updated_profile(profile, question.field, value, profile_id, answer_event)
            self._record_profile(updated)
            return updated

        answer_event = self._append_answer(
            run,
            field=question.field,
            question=question.question,
            question_event_id=question.event_id,
            answer=value,
            actor=actor,
            profile_id=profile.id,
            profile_version=profile.version,
            answer_source="live_human",
            source_ref=None,
            judgment=judgment,
            judgment_source=judgment_source,
        )
        # The answered event closes the current question even though it does not revise the
        # profile. The follow-up question becomes the sole pending question.
        if (
            self._question_count(run.id) >= self._max_questions
            or _field_question_count(self._store.events(run.id), question.field)
            >= MAX_INTERVIEW_QUESTIONS_PER_FIELD
        ):
            return profile
        follow_up, question_source = generate_follow_up(
            question.field,
            question.question,
            value,
            judgment.reason,
            INTERVIEW_FIELDS,
            profile,
            model_context,
        )
        pending = self._request_question(
            run,
            profile,
            question.field,
            follow_up,
            question_source=question_source,
            follow_up_to_answer_event_id=answer_event.event_id,
        )
        self._ensure_waiting_for_information(run.id, pending)
        return profile

    def _resolve_model_context(
        self,
        run: Run,
        *,
        provider: LLMProvider | None,
        event_log: EventLog | None,
        before_model_selection: Callable[[], None] | None,
        before_model_call: Callable[[ModelChoice], None] | None,
        record_model_usage: Callable[[LLMResponse], None] | None,
        request_ledger: RequestLedger | None,
        route_policy: ModelRoutePolicy | None,
    ) -> RiskInterviewModelContext | None:
        """Resolve explicit model handles, or the composition-root handles for one Run."""
        default = (
            self._model_context_factory(run.id) if self._model_context_factory is not None else None
        )
        resolved_provider = provider or (default.provider if default is not None else None)
        resolved_event_log = event_log or (default.event_log if default is not None else None)
        if resolved_provider is None or resolved_event_log is None:
            return None
        return RiskInterviewModelContext(
            provider=resolved_provider,
            event_log=resolved_event_log,
            before_model_selection=(
                before_model_selection
                if before_model_selection is not None
                else (default.before_model_selection if default is not None else None)
            ),
            before_model_call=(
                before_model_call
                if before_model_call is not None
                else (default.before_model_call if default is not None else None)
            ),
            record_model_usage=(
                record_model_usage
                if record_model_usage is not None
                else (default.record_model_usage if default is not None else None)
            ),
            request_ledger=(
                request_ledger
                if request_ledger is not None
                else (default.request_ledger if default is not None else None)
            ),
            route_policy=(
                route_policy
                if route_policy is not None
                else (default.route_policy if default is not None else run.model_route_policy)
            ),
        )

    def _apply_context(
        self,
        run: Run,
        profile: ActivityProfile,
        context_document: str | None,
        model_context: RiskInterviewModelContext | None,
    ) -> ActivityProfile:
        """Record each missing fact the model states with certainty from the known context.

        Code, not the model, decides what may be recorded: only a currently missing fixed field,
        once, with a value that is not an absence placeholder.
        """
        missing = _missing_fields(profile)
        if not missing:
            return profile
        sources = [_PROMPT_REF]
        context = f"Run request:\n{run.prompt.strip()}"
        if context_document and context_document.strip():
            sources.append(_CONTEXT_DOCUMENT_REF)
            context += f"\n\nProject context ({_CONTEXT_DOCUMENT_REF}):\n{context_document.strip()}"
        extraction, source = extract_facts(
            missing, _QUESTIONS, context, INTERVIEW_FIELDS, profile, model_context
        )
        if extraction is None:
            return profile
        stated = {
            fact.field: fact
            for fact in reversed(extraction.facts)
            if fact.field in missing and not is_missing_text(fact.value)
        }
        for field in missing:
            fact = stated.get(field)
            if fact is None:
                continue
            profile_id = new_id("profile")
            answer_event = self._append_answer(
                run,
                field=field,
                question=_QUESTIONS[field],
                question_event_id=None,
                answer=fact.value,
                actor=Actor.system(),
                profile_id=profile_id,
                profile_version=profile.version + 1,
                answer_source="project_context",
                source_ref=", ".join(sources),
                judgment=SufficiencyJudgment(sufficient=True, reason=fact.reason),
                judgment_source=source,
            )
            profile = _updated_profile(profile, field, fact.value, profile_id, answer_event)
            self._record_profile(profile)
        return profile

    def _request_question(
        self,
        run: Run,
        profile: ActivityProfile,
        field: str,
        question: str,
        *,
        question_source: str,
        follow_up_to_answer_event_id: str | None = None,
    ) -> PendingRiskQuestion:
        """Append one policy-gated question event for a missing fixed field."""
        question_number = self._question_count(run.id) + 1
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=run.id,
            requester=Actor.system(),
            action_kind=ActionKind.REQUEST_INFORMATION,
            subject_kind="run",
            subject_id=run.id,
            purpose=question,
            payload={
                "field": field,
                "activity_profile_id": profile.id,
                "activity_profile_version": profile.version,
            },
            idempotency_key=(
                f"risk-interview:{run.id}:{profile.version}:{field}"
                if follow_up_to_answer_event_id is None
                else (
                    f"risk-interview:{run.id}:{profile.version}:{field}:follow-up:"
                    f"{follow_up_to_answer_event_id}"
                )
            ),
        )
        payload: dict[str, object] = {
            "field": field,
            "question": question,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "question_number": question_number,
            "question_source": question_source,
            "intent": intent.model_dump(mode="json"),
        }
        if follow_up_to_answer_event_id is not None:
            payload["follow_up_to_answer_event_id"] = follow_up_to_answer_event_id
        event = self._store.append(
            run.id,
            EventType.ACTIVITY_PROFILE_QUESTIONED,
            Actor.system(),
            payload,
            expected_version=self._store.version(run.id),
            subject_id=profile.id,
            producer="thymira.core",
            producer_version="0.1",
        )
        return _question_from_event(event)

    def _append_answer(
        self,
        run: Run,
        *,
        field: str,
        question: str,
        question_event_id: str | None,
        answer: str,
        actor: Actor,
        profile_id: str,
        profile_version: int,
        answer_source: str,
        source_ref: str | None,
        judgment: SufficiencyJudgment,
        judgment_source: str,
    ) -> Event:
        """Append one answer fact, whether it revises the profile or requests clarification."""
        return self._store.append(
            run.id,
            EventType.ACTIVITY_PROFILE_ANSWERED,
            actor,
            {
                "field": field,
                "question": question,
                "question_event_id": question_event_id,
                "answer": answer,
                "answered_by": actor.model_dump(mode="json"),
                "answer_source": answer_source,
                "source_ref": source_ref,
                "sufficiency": judgment.model_dump(mode="json"),
                "sufficiency_source": judgment_source,
                "profile_id": profile_id,
                "profile_version": profile_version,
            },
            expected_version=self._store.version(run.id),
            subject_id=profile_id,
            producer="thymira.core",
            producer_version="0.1",
        )

    def latest_assessment(self, run_id: str, profile: ActivityProfile) -> RiskAssessment | None:
        """Return MIRA's persisted inherent-risk assessment for exactly this profile version."""
        for event in reversed(self._store.events(run_id)):
            if event.type is not EventType.RISK_ASSESSMENT_RECORDED:
                continue
            try:
                assessment = RiskAssessment.model_validate_json(
                    canonical_json(event.payload["risk_assessment"])
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"run {run_id}: invalid risk_assessment.recorded event {event.event_id}"
                ) from exc
            if (
                assessment.activity_profile_id == profile.id
                and assessment.activity_profile_version == profile.version
            ):
                return assessment
        return None

    def classify(
        self,
        run: Run,
        initial_profile: ActivityProfile,
        event_log: EventLog,
        provider: LLMProvider,
        *,
        before_model_selection: Callable[[], None] | None = None,
        before_model_call: Callable[[ModelChoice], None] | None = None,
        record_model_usage: Callable[[LLMResponse], None] | None = None,
        route_policy: ModelRoutePolicy | None = None,
        request_ledger: RequestLedger | None = None,
    ) -> RiskProfile:
        """Classify the current profile once, floored by its recorded MIRA assessment."""
        profile = self._profile(run, initial_profile)
        existing = self._classification(run.id, profile)
        if existing is not None:
            return existing
        assessment = self.latest_assessment(run.id, profile)
        risk = classify_risk(
            _risk_facts(profile),
            provider,
            event_log,
            run_risk_assessment=assessment,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
            request_ledger=request_ledger,
        )
        event_log.append(
            EventType.RISK_CLASSIFIED,
            Actor.system(),
            {
                "risk_profile": risk.model_dump(mode="json"),
                "activity_profile_id": profile.id,
                "activity_profile_version": profile.version,
                "mira_risk_assessment_id": assessment.id if assessment is not None else None,
            },
            subject_id=profile.id,
            producer="thymira.core",
            producer_version="0.1",
        )
        return risk

    def _profile(self, run: Run, initial_profile: ActivityProfile) -> ActivityProfile:
        """Load the latest persisted version or record the supplied initial immutable profile."""
        if initial_profile.run_id != run.id:
            raise ValueError("initial activity profile must belong to the Run")
        profile = self._recorded_profile(run.id)
        if profile is not None:
            return profile
        self._record_profile(initial_profile)
        return initial_profile

    def _recorded_profile(self, run_id: str) -> ActivityProfile | None:
        """Return the latest validated activity-profile record without creating one."""
        for event in reversed(self._store.events(run_id)):
            if event.type is not EventType.ACTIVITY_PROFILE_RECORDED:
                continue
            try:
                profile = ActivityProfile.model_validate_json(
                    canonical_json(event.payload["activity_profile"])
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"run {run_id}: invalid activity_profile.recorded event {event.event_id}"
                ) from exc
            if profile.run_id != run_id:
                raise ValueError(
                    f"run {run_id}: activity profile {profile.id} belongs to another Run"
                )
            return profile
        return None

    def _record_profile(self, profile: ActivityProfile) -> None:
        """Append the exact immutable profile that subsequent evidence refers to."""
        assert profile.run_id is not None  # noqa: S101  # validated by ActivityProfile.
        self._store.append(
            profile.run_id,
            EventType.ACTIVITY_PROFILE_RECORDED,
            Actor.system(),
            {"activity_profile": profile.model_dump(mode="json")},
            expected_version=self._store.version(profile.run_id),
            subject_id=profile.id,
            producer="thymira.core",
            producer_version="0.1",
        )

    def _pending_question(self, run_id: str) -> PendingRiskQuestion | None:
        """Fold question and answer events into the last unanswered question."""
        answered = {
            event.payload.get("question_event_id")
            for event in self._store.events(run_id)
            if event.type is EventType.ACTIVITY_PROFILE_ANSWERED
        }
        for event in reversed(self._store.events(run_id)):
            if (
                event.type is EventType.ACTIVITY_PROFILE_QUESTIONED
                and event.event_id not in answered
            ):
                return _question_from_event(event)
        return None

    def _question_count(self, run_id: str) -> int:
        """Return the durable number of questions already asked for one Run."""
        return sum(
            event.type is EventType.ACTIVITY_PROFILE_QUESTIONED
            for event in self._store.events(run_id)
        )

    def _ensure_waiting_for_information(self, run_id: str, question: PendingRiskQuestion) -> None:
        """Ask the policy-gated control plane to park the Run for this question."""
        current = self._control_plane.controller.current_state(run_id)
        if current.condition.value == "waiting" and current.wait_reason is not None:
            if current.wait_reason.value == "information":
                return
            raise RiskInterviewError(f"run {run_id!r} is waiting for {current.wait_reason.value}")
        event = next(
            (item for item in self._store.events(run_id) if item.event_id == question.event_id),
            None,
        )
        if event is None:
            raise RiskInterviewError(f"run {run_id!r} question evidence is missing")
        try:
            intent = ActionIntent.model_validate(event.payload["intent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskInterviewError(f"run {run_id!r} question intent is invalid") from exc
        result = self._control_plane.handle(intent)
        if result.state is None or result.state.wait_reason is None:
            raise RiskInterviewError("the information request was not authorized to park the Run")
        if result.state.wait_reason.value != "information":
            raise RiskInterviewError("the information request parked the Run for the wrong reason")

    def _escalate_question_limit(self, run_id: str) -> None:
        """Request human review once the bounded interview cannot establish the required facts.

        The park names :data:`INTERVIEW_LIMIT_RESUME_TARGET` so the approval re-enters the
        interview body. Found live in `run_5a9e9662d6ce4527bc1c250bf6b5b9fd`: a target-less park
        made `RunService.resolve_approval` record `resume_from: approval`, which
        `InlineDispatcher._resume_node` maps to the Gate -- and the Gate's outgoing edge demands
        a policy decision this Run never reached, so the approved Run died with
        `runtime graph Gate node did not produce a policy decision` and the answer was spent for
        nothing.
        """
        if self._limit_review_resolved(run_id):
            return
        if self._limit_review_pending(run_id):
            return
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=run_id,
            requester=Actor.system(),
            action_kind=ActionKind.REQUEST_APPROVAL,
            subject_kind="run",
            subject_id=run_id,
            purpose=_MAX_REVIEW_SUMMARY,
            payload={
                "max_questions": self._max_questions,
                "max_questions_per_field": MAX_INTERVIEW_QUESTIONS_PER_FIELD,
                "reason": "missing_activity_profile",
            },
            idempotency_key=f"risk-interview-limit:{run_id}:{self._max_questions}",
        )
        result = self._control_plane.handle(intent)
        if result.decision.decision is not Decision.REQUIRE_HUMAN_REVIEW:
            raise RiskInterviewError("question-limit escalation did not require human review")
        if result.state is None:
            self._control_plane.controller.park_for_review(
                run_id,
                result.decision,
                resume_from=INTERVIEW_LIMIT_RESUME_TARGET,
            )

    def _limit_review_pending(self, run_id: str) -> bool:
        """Return whether this interview already requested but has not received the limit review."""
        requested = self._limit_review_decision_id(run_id)
        if requested is None:
            return False
        return not self._limit_review_answered(run_id)

    def _limit_review_answered(self, run_id: str) -> bool:
        """Return whether a human answer retired the bounded interview review request."""
        requested = self._limit_review_decision_id(run_id)
        if requested is None:
            return False
        return any(
            event.type is EventType.HUMAN_APPROVAL
            and approval_names_decision(event.payload, requested)
            and _closes_review(event)
            for event in self._store.events(run_id)
        )

    def _limit_review_resolved(self, run_id: str) -> bool:
        """Return whether a human has answered the bounded interview's review request."""
        requested = self._limit_review_decision_id(run_id)
        if requested is None:
            return False
        return any(
            event.type is EventType.HUMAN_APPROVAL
            and approval_names_decision(event.payload, requested)
            and event.payload.get("approved") is True
            and ("automatic" not in event.payload or event.payload["automatic"] is False)
            and event.actor.kind is ActorKind.HUMAN
            and event.actor.authenticated
            for event in self._store.events(run_id)
        )

    def _limit_review_decision_id(self, run_id: str) -> str | None:
        """Find the decision id created for the one bounded-interview review request."""
        for event in reversed(self._store.events(run_id)):
            if event.type is not EventType.HUMAN_APPROVAL_REQUESTED:
                continue
            if event.payload.get("summary") != _MAX_REVIEW_SUMMARY:
                continue
            decision_id = event.payload.get("decision_id")
            return decision_id if isinstance(decision_id, str) else None
        return None

    def _classification(self, run_id: str, profile: ActivityProfile) -> RiskProfile | None:
        """Return an already-recorded classification for the exact immutable profile."""
        for event in reversed(self._store.events(run_id)):
            if event.type is not EventType.RISK_CLASSIFIED:
                continue
            if (
                event.payload.get("activity_profile_id") != profile.id
                or event.payload.get("activity_profile_version") != profile.version
            ):
                continue
            try:
                return RiskProfile.model_validate(event.payload["risk_profile"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"run {run_id}: invalid risk.classified event {event.event_id}"
                ) from exc
        return None


def _missing_fields(profile: ActivityProfile) -> tuple[str, ...]:
    """Return only required fields without evidence, never inferring a replacement fact."""
    missing: list[str] = []
    for field in INTERVIEW_FIELDS:
        value = getattr(profile, field)
        if not has_declared_value(value):
            missing.append(field)
    return tuple(missing)


def _field_question_count(events: Sequence[Event], field: str) -> int:
    """Return how many durable interview questions targeted one fixed field."""
    return sum(
        event.type is EventType.ACTIVITY_PROFILE_QUESTIONED and event.payload.get("field") == field
        for event in events
    )


def _question_from_event(event: Event) -> PendingRiskQuestion:
    """Validate one persisted question event before exposing it to a caller."""
    payload = event.payload
    field = payload.get("field")
    question = payload.get("question")
    profile_id = payload.get("profile_id")
    profile_version = payload.get("profile_version")
    question_number = payload.get("question_number")
    if (
        not isinstance(field, str)
        or field not in INTERVIEW_FIELDS
        or not isinstance(question, str)
        or not question
        or not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(profile_version, int)
        or isinstance(profile_version, bool)
        or profile_version < 1
        or not isinstance(question_number, int)
        or isinstance(question_number, bool)
        or question_number < 1
    ):
        raise RiskInterviewError(f"risk-interview question event {event.event_id} is invalid")
    return PendingRiskQuestion(
        event_id=event.event_id,
        field=field,
        question=question,
        profile_id=profile_id,
        profile_version=profile_version,
        question_number=question_number,
    )


def _updated_profile(
    profile: ActivityProfile,
    field: str,
    answer: str,
    profile_id: str,
    answer_event: Event,
) -> ActivityProfile:
    """Return the next immutable profile version anchored to the answer event."""
    if field not in INTERVIEW_FIELDS:
        raise RiskInterviewError(f"unknown activity-profile field {field!r}")
    value: str | tuple[str, ...]
    if field in {"data_categories", "sensitive_attributes", "potential_consequences"}:
        value = _split_values(answer)
    else:
        value = answer
    evidence = Evidence(kind="event", ref=f"seq:{answer_event.seq}", sha256=answer_event.hash)
    data = profile.model_dump(mode="python")
    data.update(
        {
            "id": profile_id,
            "version": profile.version + 1,
            "supersedes_id": profile.id,
            "evidence_refs": (*profile.evidence_refs, evidence),
            field: value,
        }
    )
    return ActivityProfile.model_validate(data)


def _split_values(answer: str) -> tuple[str, ...]:
    """Preserve a declared absence or split a plain list without inventing categories."""
    if normalise(answer) in _NO_VALUE_MARKERS:
        return ()
    return tuple(part.strip() for part in re.split(r"[,;\n]", answer) if part.strip())


def _risk_facts(profile: ActivityProfile) -> RiskFacts:
    """Extract only declared activity-profile facts for the deterministic classifier layer."""
    population = normalise(profile.affected_population)
    no_person_impact = population in _NO_VALUE_MARKERS
    has_sensitive_attributes = bool(profile.sensitive_attributes) and any(
        normalise(value) not in _NO_VALUE_MARKERS for value in profile.sensitive_attributes
    )
    present_context = tuple(
        name
        for name, value in (
            ("intended_use", profile.purpose),
            ("decision_role", profile.decision_effect),
            ("deployment_context", profile.autonomy),
            ("affected_population", profile.affected_population),
            ("human_oversight", profile.human_oversight),
            ("potential_consequences", profile.potential_consequences),
        )
        if has_declared_value(value)
    )
    return RiskFacts(
        objective=profile.purpose,
        has_sensitive_attributes=has_sensitive_attributes,
        affects_natural_persons=not no_person_impact,
        missing_required_information=_missing_fields(profile),
        present_context_fields=present_context,
        answerable_fields=ANSWERABLE_RISK_FIELDS,
        declared_context=tuple(
            (name, value)
            for name, value in (
                ("decision_effect", profile.decision_effect),
                ("autonomy", profile.autonomy),
                ("human_oversight", profile.human_oversight),
            )
            if has_declared_value(value)
        ),
    )


__all__ = [
    "INTERVIEW_FIELDS",
    "MAX_INTERVIEW_QUESTIONS",
    "MAX_INTERVIEW_QUESTIONS_PER_FIELD",
    "PendingRiskQuestion",
    "RiskInterviewError",
    "RiskInterviewModelContext",
    "RiskInterviewNotPendingError",
    "RiskInterviewService",
    "RiskInterviewStatus",
    "SufficiencyJudgment",
]
