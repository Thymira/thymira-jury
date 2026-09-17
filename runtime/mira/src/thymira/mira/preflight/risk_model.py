"""Model-assisted inherent-risk judgement with a closed reviewed taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ConfigDict, Field

from thymira.events import canonical_json, scrub_credentials
from thymira.schemas import ActivityProfile, Actor, EventType, RiskLevel, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.events import EventLog
    from thymira.mira.preflight.models import BaseRiskMethod
    from thymira.schemas import ModelRoutePolicy


RISK_CONFIDENCE_THRESHOLD = 0.75
"""Minimum confidence for accepting a model's reviewed risk-category selection.

This matches the default policy's minimum risk confidence. A lower model result remains an
uncertain fact and must not silently become an executable risk profile.
"""

MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX = "MIRA's model could not classify inherent risk"
"""Stable marker for Core to distinguish model uncertainty from a model-call fallback."""

_PROFILE_FIELDS = (
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


class RiskJudgment(ThymiraModel):
    """The bounded structured output accepted from MIRA's risk-classification model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_level: RiskLevel
    activity_category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: tuple[str, ...] = ()
    justification: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class RiskAssessmentModelContext:
    """Provider and auditable call hooks for one MIRA risk judgement."""

    provider: LLMProvider
    event_log: EventLog
    before_model_selection: Callable[[], None] | None = None
    before_model_call: Callable[[ModelChoice], None] | None = None
    record_model_usage: Callable[[LLMResponse], None] | None = None
    request_ledger: RequestLedger | None = None
    route_policy: ModelRoutePolicy | None = None


_RISK_SYSTEM_PROMPT = """\
You are MIRA's inherent-risk assessor. Judge the complete activity profile against the reviewed
risk taxonomy supplied in the user message. Return exactly one structured judgement.

The taxonomy is closed: choose only an exact risk_level and activity_category pair from the
reviewed rules. Never invent a category or level, and never use mitigations or controls to lower
inherent risk. Do not fill missing facts with assumptions. If the profile is ambiguous or does not
support a confident selection, return the most appropriate reviewed pair only when you can defend
it and set missing_information or lower confidence; otherwise use risk_level=unknown,
activity_category=unknown. Keep the justification concise and factual; do not reveal hidden
reasoning.
"""


def classify_risk(
    profile: ActivityProfile,
    method: BaseRiskMethod,
    model_context: RiskAssessmentModelContext | None,
) -> RiskJudgment | None:
    """Ask MIRA's model for one bounded risk judgement, or return ``None`` on any model failure.

    The caller owns the deterministic fail-closed assessment when this optional enhancement is
    unavailable. Call preparation is inside the same broad fallback boundary as the provider so a
    budget, routing, structured-output, or accounting failure cannot turn into a guessed risk.
    """
    if model_context is None:
        return None
    from thymira.agents.llm.base import (  # noqa: PLC0415  # preserve MIRA's lazy boundary
        LLMStructuredOutputError,
    )
    from thymira.agents.request_ledger import (  # noqa: PLC0415  # preserve MIRA's lazy boundary
        RequestLedger,
        instrument_provider,
    )

    try:
        _prepare_model_call(model_context)
        provider = model_context.provider
        detach = getattr(provider, "without_request_ledger", None)
        if callable(detach):
            provider = detach()
        ledger = model_context.request_ledger
        if ledger is None and callable(getattr(provider, "attach_request_ledger", None)):
            ledger = RequestLedger(model_context.event_log)
        active_provider = (
            instrument_provider(
                provider,
                ledger,
                owner_id="mira-risk-preflight",
            )
            if ledger is not None
            else provider
        )
        parsed, response = active_provider.complete_structured(
            scrub_credentials(_risk_prompt(profile, method)),
            schema=RiskJudgment,
            system=scrub_credentials(_RISK_SYSTEM_PROMPT),
        )
        if model_context.record_model_usage is not None:
            model_context.record_model_usage(response)
        return RiskJudgment.model_validate(parsed.model_dump(mode="python"))
    except Exception as exc:  # noqa: BLE001  # optional model assistance must fail closed
        if (
            isinstance(exc, LLMStructuredOutputError)
            and exc.response is not None
            and model_context.record_model_usage is not None
        ):
            model_context.record_model_usage(exc.response)
        return None


def _prepare_model_call(model_context: RiskAssessmentModelContext) -> ModelChoice:
    """Route and record one MIRA classification call before invoking its provider."""
    from thymira.agents.llm.routing import (  # noqa: PLC0415  # preserve MIRA's lazy boundary
        Role,
        choose,
    )
    from thymira.agents.route_policy import (  # noqa: PLC0415  # preserve MIRA's lazy boundary
        enforce_model_route,
    )

    if model_context.before_model_selection is not None:
        model_context.before_model_selection()
    choice = choose(Role.MIRA, "classify")
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


def _risk_prompt(profile: ActivityProfile, method: BaseRiskMethod) -> str:
    """Render all nine profile facts plus the reviewed rule taxonomy."""
    profile_values = {field: getattr(profile, field) for field in _PROFILE_FIELDS}
    rules = [
        {
            "id": rule.id,
            "category": rule.category,
            "risk_level": rule.risk_level.value,
            "sensitive_attributes_any": list(rule.sensitive_attributes_any),
            "requires_no_sensitive_attributes": rule.requires_no_sensitive_attributes,
        }
        for rule in method.rules
    ]
    return canonical_json(
        {
            "activity_profile": profile_values,
            "reviewed_risk_method": {
                "id": method.id,
                "version": method.version,
                "rules": rules,
            },
            "instruction": (
                "Select one exact reviewed rule pair. Use the complete profile, do not invent "
                "facts, and report uncertainty in missing_information and confidence."
            ),
        }
    )


__all__ = [
    "MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX",
    "RISK_CONFIDENCE_THRESHOLD",
    "RiskAssessmentModelContext",
    "RiskJudgment",
    "classify_risk",
]
