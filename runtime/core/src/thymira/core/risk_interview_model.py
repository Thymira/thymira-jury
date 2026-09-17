"""Model-assisted extraction, phrasing and sufficiency judgments for the activity interview."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from thymira.agents.llm.base import LLMStructuredOutputError
from thymira.agents.llm.routing import Role, choose
from thymira.agents.request_ledger import RequestLedger, instrument_provider
from thymira.agents.route_policy import enforce_model_route
from thymira.events import scrub_credentials
from thymira.schemas import ActivityProfile, Actor, EventType

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from thymira.agents.llm import LLMProvider
    from thymira.agents.llm.base import LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.events import EventLog
    from thymira.schemas import ModelRoutePolicy


class SufficiencyJudgment(BaseModel):
    """Bounded LLM judgment of whether one activity fact is sufficiently described."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sufficient: bool
    reason: str = Field(min_length=1)


class ExtractedFact(BaseModel):
    """One activity fact the model states with certainty from the available context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    value: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FactExtraction(BaseModel):
    """The facts the model could settle from context; every omitted field is asked to a human."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[ExtractedFact, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskInterviewModelContext:
    """Provider and audit hooks used by one Run's interview model calls."""

    provider: LLMProvider
    event_log: EventLog
    before_model_selection: Callable[[], None] | None = None
    before_model_call: Callable[[ModelChoice], None] | None = None
    record_model_usage: Callable[[LLMResponse], None] | None = None
    request_ledger: RequestLedger | None = None
    route_policy: ModelRoutePolicy | None = None


_EXTRACTION_SYSTEM_PROMPT = """\
You fill a bounded governance intake from the context a project already provides. For each
missing activity fact, state its value only when the context settles it with high certainty: the
fact must be explicitly stated or follow unambiguously from what is written. Never guess, never
infer a fact from domain conventions, and never fill a field the context leaves open; omit it so a
human is asked instead. A list-valued fact is a comma-separated list; write "none" only when the
context explicitly states that nothing applies. Each value must be a concrete statement of the
fact, in the language of the context, and each reason must be short and factual.
"""
_QUESTION_SYSTEM_PROMPT = """\
You write one concise question for a bounded governance intake. Ask only about the named activity
fact. The static question is a fallback reference, not a question to repeat mechanically. Earlier
answers are context only; do not infer facts that they do not state.
"""
_JUDGMENT_SYSTEM_PROMPT = """\
You judge whether a candidate response supplies enough concrete information for the named activity
fact. Return sufficient=true only when the fact is actually stated. If the response is vague,
ambiguous, missing, or merely says that the fact is not needed, return sufficient=false. The fixed
interview field is always required; you cannot mark it as unnecessary. Keep reason concise and
factual, without hidden reasoning.
"""
_FOLLOW_UP_SYSTEM_PROMPT = """\
Write exactly one concise, directed follow-up question for a governance intake. Target the missing
detail named by the sufficiency reason. Do not repeat the static question verbatim and do not claim
that the field is unnecessary.
"""
_MISSING_MARKERS = frozenset(
    {
        "",
        "undeclared",
        "unknown",
        "not declared",
        "not specified",
        "unspecified",
        "not provided",
        "none provided",
        "tbd",
        "n a",
        "na",
        "no declarado",
        "desconocido",
        "sin declarar",
        "sin especificar",
        "not applicable",
        "not available",
        "not known",
        "null",
        "nil",
    }
)


def extract_facts(
    missing: tuple[str, ...],
    questions: Mapping[str, str],
    context: str,
    fields: tuple[str, ...],
    profile: ActivityProfile,
    model_context: RiskInterviewModelContext | None,
) -> tuple[FactExtraction | None, str]:
    """Ask once which missing facts the context settles; ``None`` when no reliable result exists.

    The caller validates every returned fact against the fixed field list before recording it, so
    a model cannot introduce a field or fill one that is not missing.
    """
    if model_context is None:
        return None, "fallback"
    _prepare_model_call(model_context, "extract")
    wanted = "\n".join(f"- {field}: {questions[field]}" for field in missing)
    prompt = (
        "Missing activity-profile fields (state only those the context settles):\n"
        f"{wanted}\n"
        "Already recorded fields:\n"
        f"{_profile_context(profile, fields=fields, exclude='')}\n"
        "Available context:\n<<<\n"
        f"{context}\n"
        ">>>\n"
        "Return only the structured extraction; omit every field you cannot state with certainty."
    )
    try:
        parsed, response = _instrumented_provider(model_context).complete_structured(
            scrub_credentials(prompt),
            schema=FactExtraction,
            system=scrub_credentials(_EXTRACTION_SYSTEM_PROMPT),
        )
        if model_context.record_model_usage is not None:
            model_context.record_model_usage(response)
        extraction = FactExtraction.model_validate(parsed.model_dump())
    except Exception as exc:  # noqa: BLE001  # a failed extraction only means the human is asked
        _record_structured_failure(model_context, exc)
        return None, "fallback"
    return extraction, "model"


def generate_question(
    field: str,
    static_question: str,
    fields: tuple[str, ...],
    profile: ActivityProfile,
    model_context: RiskInterviewModelContext | None,
) -> tuple[str, str]:
    """Generate one field-specific question, retaining the static fallback on provider errors."""
    if model_context is None:
        return static_question, "static"
    _prepare_model_call(model_context, "format")
    prompt = (
        f"Current activity-profile field: {field}\n"
        f"Static fallback question: {static_question}\n"
        "Already answered fields:\n"
        f"{_profile_context(profile, fields=fields, exclude=field)}\n"
        "Write the next question now."
    )
    try:
        response = _instrumented_provider(model_context).complete(
            scrub_credentials(prompt), system=scrub_credentials(_QUESTION_SYSTEM_PROMPT)
        )
        if model_context.record_model_usage is not None:
            model_context.record_model_usage(response)
    except Exception:  # noqa: BLE001  # provider failures must never block the interview
        return static_question, "static_fallback"
    question = response.text.strip()
    return (question, "model") if question else (static_question, "static_fallback")


def judge_sufficiency(
    field: str,
    question: str,
    answer: str,
    fields: tuple[str, ...],
    profile: ActivityProfile,
    model_context: RiskInterviewModelContext | None,
) -> tuple[SufficiencyJudgment | None, str]:
    """Judge one candidate answer, returning ``None`` when no reliable model result exists."""
    if model_context is None:
        return None, "fallback"
    _prepare_model_call(model_context, "extract")
    prompt = (
        f"Required activity-profile field: {field}\n"
        f"Required fact question: {question}\n"
        "Other recorded fields:\n"
        f"{_profile_context(profile, fields=fields, exclude=field)}\n"
        "Candidate response:\n<<<\n"
        f"{answer}\n"
        ">>>\n"
        "Return only the structured sufficiency judgment."
    )
    try:
        parsed, response = _instrumented_provider(model_context).complete_structured(
            scrub_credentials(prompt),
            schema=SufficiencyJudgment,
            system=scrub_credentials(_JUDGMENT_SYSTEM_PROMPT),
        )
        if model_context.record_model_usage is not None:
            model_context.record_model_usage(response)
        judgment = SufficiencyJudgment.model_validate(parsed.model_dump())
    except Exception as exc:  # noqa: BLE001  # an optional evaluator cannot block a live answer
        _record_structured_failure(model_context, exc)
        return None, "fallback"
    return judgment, "model"


def generate_follow_up(
    field: str,
    question: str,
    answer: str,
    reason: str,
    fields: tuple[str, ...],
    profile: ActivityProfile,
    model_context: RiskInterviewModelContext | None,
) -> tuple[str, str]:
    """Write one directed follow-up question for an insufficient human answer."""
    fallback = (
        f"Please give one concrete answer about {field.replace('_', ' ')}, including the "
        "specific people, actions, data, or consequences that apply."
    )
    if model_context is None:
        return fallback, "fallback"
    _prepare_model_call(model_context, "format")
    prompt = (
        f"Required activity-profile field: {field}\n"
        f"Original question: {question}\n"
        f"Human answer:\n<<<\n{answer}\n>>>\n"
        f"Sufficiency reason: {reason}\n"
        "Already answered fields:\n"
        f"{_profile_context(profile, fields=fields, exclude=field)}\n"
        "Write one directed follow-up question."
    )
    try:
        response = _instrumented_provider(model_context).complete(
            scrub_credentials(prompt), system=scrub_credentials(_FOLLOW_UP_SYSTEM_PROMPT)
        )
        if model_context.record_model_usage is not None:
            model_context.record_model_usage(response)
    except Exception:  # noqa: BLE001  # retain a bounded directed fallback on provider errors
        return fallback, "fallback"
    follow_up = response.text.strip()
    return (follow_up, "model") if follow_up else (fallback, "fallback")


def _prepare_model_call(model_context: RiskInterviewModelContext, task: str) -> ModelChoice:
    """Run the same selection and pre-call audit hooks used by risk classification."""
    if model_context.before_model_selection is not None:
        model_context.before_model_selection()
    choice = choose(Role.AGENT, task)
    model_context.event_log.append(
        EventType.MODEL_SELECTED,
        Actor.system(),
        choice.event_payload(),
    )
    enforce_model_route(
        choice,
        model_context.route_policy,
        event_log=model_context.event_log,
        actor=Actor.system(),
    )
    if model_context.before_model_call is not None:
        model_context.before_model_call(choice)
    return choice


def _instrumented_provider(model_context: RiskInterviewModelContext) -> LLMProvider:
    """Return the provider behind one durable interview request boundary."""
    provider = model_context.provider
    detach = getattr(provider, "without_request_ledger", None)
    if callable(detach):
        provider = detach()
    ledger = model_context.request_ledger
    if ledger is None and callable(getattr(provider, "attach_request_ledger", None)):
        ledger = RequestLedger(model_context.event_log)
    if ledger is None:
        return provider
    return instrument_provider(provider, ledger, owner_id="risk-interview")


def _record_structured_failure(model_context: RiskInterviewModelContext, error: Exception) -> None:
    """Charge a provider response rejected by local structured-output validation."""
    if (
        isinstance(error, LLMStructuredOutputError)
        and error.response is not None
        and model_context.record_model_usage is not None
    ):
        model_context.record_model_usage(error.response)


def _profile_context(profile: ActivityProfile, *, fields: tuple[str, ...], exclude: str) -> str:
    """Render already declared profile facts as bounded model prompt context."""
    lines: list[str] = []
    for field in fields:
        if field == exclude:
            continue
        value = getattr(profile, field)
        if not has_declared_value(value):
            continue
        rendered = ", ".join(value) if isinstance(value, tuple) else value
        lines.append(f"- {field}: {rendered or 'none'}")
    return "\n".join(lines) if lines else "- none"


def normalise(value: str) -> str:
    """Fold case, accents, punctuation and whitespace for placeholder comparison."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[\W_]+", " ", unaccented).split())


def is_missing_text(value: str) -> bool:
    """Return whether a textual profile fact is a known absence marker."""
    return normalise(value) in _MISSING_MARKERS


def has_declared_value(value: str | tuple[str, ...]) -> bool:
    """Return whether one scalar or category answer carries declared evidence.

    An empty category tuple is an explicit answer such as ``none``. It is materially different
    from the initial ``("undeclared",)`` placeholder and must not trigger a question.
    """
    if isinstance(value, str):
        return not is_missing_text(value)
    return not value or not all(is_missing_text(item) for item in value)


__all__ = [
    "ExtractedFact",
    "FactExtraction",
    "RiskInterviewModelContext",
    "SufficiencyJudgment",
    "extract_facts",
    "generate_follow_up",
    "generate_question",
    "has_declared_value",
    "is_missing_text",
    "judge_sufficiency",
    "normalise",
]
