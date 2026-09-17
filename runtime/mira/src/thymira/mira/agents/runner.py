"""Bounded execution of one MIRA audit-agent declaration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import ModelSettings, RunContext
from pydantic_ai import Tool as PydanticTool
from pydantic_ai.exceptions import ModelRetry, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from thymira.agents.llm.base import LLMProvider, LLMStructuredOutputError
from thymira.agents.llm.routing import ModelChoice, Role
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import frame_untrusted
from thymira.agents.tool_bridge import render_tool_output
from thymira.agents.usage import RunUsage
from thymira.events import canonical_json, redact_value
from thymira.mira.agents.required_tools import (
    latest_applicable_regulation_search,
    required_tool_failures,
    unattempted_required_tools,
)
from thymira.mira.audit_io import AuditAgentOutput, AuditInput
from thymira.mira.evidence import (
    EvidenceContentError,
    EvidenceExcerpt,
    EvidenceLimitError,
    EvidenceReader,
)
from thymira.mira.finding_safety import safe_finding
from thymira.mira.grounding import (
    RunEvidenceIndex,
    ground_findings,
    unbacked_summary,
)
from thymira.schemas import (
    Actor,
    AuditFinding,
    Event,
    EventType,
    Evidence,
    Severity,
    TaskStatus,
    ThymiraModel,
    id_kind,
    new_id,
    utc_now,
)
from thymira.tools import ToolContext, ToolManager, ToolRegistry, input_schema

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents.llm.base import LLMResponse
    from thymira.agents.request_ledger import RequestLedger
    from thymira.agents.runtime_catalog import RuntimeSkillCatalog
    from thymira.events import EventLog
    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.checks import AuditReport
    from thymira.schemas import ModelRoutePolicy
    from thymira.tools import Tool as ToolContract

    type AuditToolContextFactory = Callable[[str, str], ToolContext]


@dataclass(frozen=True, slots=True)
class EvidenceProjectionLimits:
    """Independent character caps for MIRA's model-visible evidence projection."""

    total_chars: int = 12_000
    event_chars: int = 2_000
    agent_message_chars: int = 2_000
    excerpt_chars: int = 2_000
    report_chars: int = 4_000


@dataclass(frozen=True, slots=True)
class AuditAgentContext:
    """The in-process dependencies and identity of one audit-agent invocation."""

    event_log: EventLog
    actor: Actor
    agent_id: str
    task_id: str
    runtime_skill_catalog: RuntimeSkillCatalog | None = None
    runtime_skill_budget: int = 32_000
    runtime_skill_names: tuple[str, ...] = ()
    runtime_skill_request_id: str | None = None
    runtime_skill_dispatch_id: str | None = None
    request_ledger: RequestLedger | None = None
    provider: LLMProvider | None = None
    tool_registry: ToolRegistry | None = None
    tool_context: ToolContext | None = None
    evidence_reader: EvidenceReader | None = None
    projection_limits: EvidenceProjectionLimits = field(default_factory=EvidenceProjectionLimits)
    before_model_selection: Callable[[], None] | None = None
    before_model_call: Callable[[ModelChoice], None] | None = None
    record_model_usage: Callable[[LLMResponse], None] | None = None
    route_policy: ModelRoutePolicy | None = None


class _FindingContent(ThymiraModel):
    """The finding content a model may propose without authoritative identity fields."""

    control_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[Evidence, ...] = ()
    recommendation: str | None = None


class _AuditAgentResponse(ThymiraModel):
    """The complete structured response returned by one audit agent."""

    findings: tuple[_FindingContent, ...] = ()


_MODEL_CONTROL_ID_PREFIX = "model:"
"""Namespace for control ids proposed by an audit model."""

_AUDIT_EVENT_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_TRANSITIONED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_DENIED,
        EventType.POLICY_DECISION,
        EventType.ARTIFACT_CREATED,
    }
)
"""Run-log event types that generic audit agents may inspect directly."""

_MAX_AGENT_MESSAGES = 8
"""Maximum number of the latest narrative messages shown to an audit agent."""

_MAX_ARTIFACT_EXCERPTS = 4
"""Maximum number of report-cited artifact excerpts shown to an audit agent."""


def run_audit_agent(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
) -> AuditAgentOutput:
    """Run one audit agent and return only its normalised candidate findings.

    Raises:
        ValueError: If the context, evidence projection, or hash-pinned excerpt is invalid.
        LLMStructuredOutputError: If structured output cannot be validated within ``max_turns``
            or a required tool call does not complete successfully.
    """
    validate_audit_agent_context(spec, audit_input, context)
    prompt = _build_evidence_projection(audit_input, context)
    instructions = runtime_skill_instructions(spec, context)
    runtime_evidence = runtime_skill_event_payload(context)
    event_start = len(context.event_log.events())
    task_kind = spec.task_kinds[0]
    step_usage = RunUsage()
    model = routed_model(
        Role.MIRA,
        task_kind,
        context.event_log,
        requested_tier=spec.tier,
        provider=context.provider,
        actor=context.actor,
        output_schema=_AuditAgentResponse,
        runtime_skill_evidence=runtime_evidence,
        before_model_selection=context.before_model_selection,
        before_model_call=context.before_model_call,
        record_model_usage=_step_usage_recorder(step_usage, context.record_model_usage),
        route_policy=context.route_policy,
        request_ledger=context.request_ledger,
        request_owner_id=(
            context.runtime_skill_dispatch_id or f"{context.task_id}:{context.agent_id}"
        ),
    )
    tools = build_audit_tools(spec, context)
    agent: PydanticAgent[None, _AuditAgentResponse] = PydanticAgent(
        model=model,
        output_type=_AuditAgentResponse,
        instructions=instructions,
        retries=max(spec.max_turns - 1, 0),
        tools=tools,
        model_settings=ModelSettings(parallel_tool_calls=False),
    )
    required_tool_retry_sent = False

    @agent.output_validator
    def _require_declared_audit_tools(
        run_context: RunContext[None], output: _AuditAgentResponse
    ) -> _AuditAgentResponse:
        """Give one bounded correction when the model skips a required audit tool."""
        nonlocal required_tool_retry_sent
        cycle_events = context.event_log.events()[event_start:]
        unattempted = unattempted_required_tools(
            cycle_events,
            context.task_id,
            spec.required_tool_calls,
        )
        correction_fits = run_context.usage.requests + 2 <= spec.max_turns
        if unattempted and not required_tool_retry_sent and correction_fits:
            required_tool_retry_sent = True
            rendered = ", ".join(unattempted)
            message = (
                "Required audit evidence is missing. Before returning final_result, call each "
                f"of these required audit tools and use its returned evidence: {rendered}."
            )
            raise ModelRetry(message)
        return output

    context.event_log.append(
        EventType.AGENT_STARTED,
        context.actor,
        {
            "agent": spec.name,
            "agent_id": context.agent_id,
            "task_id": context.task_id,
            "framework": spec.framework.value,
        },
        subject_id=context.agent_id,
        producer="thymira.mira",
    )
    try:
        result = agent.run_sync(prompt, usage_limits=UsageLimits(request_limit=spec.max_turns))
    except (UnexpectedModelBehavior, UsageLimitExceeded):
        context.event_log.append(
            EventType.AGENT_COMPLETED,
            context.actor,
            {
                "agent": spec.name,
                "agent_id": context.agent_id,
                "task_id": context.task_id,
                "status": TaskStatus.FAILED.value,
                "max_turns": spec.max_turns,
                **_usage_payload(step_usage),
            },
            subject_id=context.agent_id,
            producer="thymira.mira",
        )
        message = f"MIRA audit agent {spec.name!r} exhausted structured-output validation"
        raise LLMStructuredOutputError(message) from None

    cycle_events = context.event_log.events()[event_start:]
    tool_failures = required_tool_failures(
        cycle_events,
        context.task_id,
        spec.required_tool_calls,
        audit_framework=spec.framework,
    )
    if tool_failures:
        reason = "; ".join(f"{tool}: {error}" if error else tool for tool, error in tool_failures)
        context.event_log.append(
            EventType.AGENT_COMPLETED,
            context.actor,
            {
                "agent": spec.name,
                "agent_id": context.agent_id,
                "task_id": context.task_id,
                "status": TaskStatus.FAILED.value,
                "reason": reason,
                **_usage_payload(step_usage),
            },
            subject_id=context.agent_id,
            producer="thymira.mira",
        )
        message = f"MIRA audit agent {spec.name!r} required audit tool did not complete: {reason}"
        raise LLMStructuredOutputError(message)

    proposed = tuple(
        _normalise_finding(content, spec, audit_input, context)
        for content in result.output.findings
    )
    # The model authored these evidence references; the Run's own log decides which of them are
    # real. An unbacked reference is dropped here, before the finding can carry it into the report,
    # the deduplication key, or the assurance bundle's evidence and traceability indexes. The log
    # is read as it stands now, not as the agent was shown it: the agent's own tool calls are part
    # of the evidence it may cite. External chunks are narrower: only this task's latest resolved,
    # canonical and framework-applicable regulation search may back them.
    regulation_event = latest_applicable_regulation_search(
        cycle_events,
        context.task_id,
        spec.framework,
    )
    regulation_events = () if regulation_event is None else (regulation_event,)
    findings, unbacked = ground_findings(
        proposed,
        RunEvidenceIndex.from_events(
            context.event_log.events(),
            regulation_events=regulation_events,
        ),
    )
    model_choice = _last_model_choice(cycle_events)
    completion: dict[str, Any] = {
        "agent": spec.name,
        "agent_id": context.agent_id,
        "task_id": context.task_id,
        "status": TaskStatus.COMPLETED.value,
        **_usage_payload(step_usage),
    }
    if unbacked:
        completion["unbacked_evidence"] = unbacked_summary(unbacked)
    context.event_log.append(
        EventType.AGENT_COMPLETED,
        context.actor,
        completion,
        subject_id=context.agent_id,
        producer="thymira.mira",
    )
    return AuditAgentOutput.from_spec(spec, findings=findings, model_choice=model_choice)


def runtime_skill_instructions(spec: AuditAgentSpec, context: AuditAgentContext) -> str:
    """Return MIRA instructions with selected runtime skill bodies loaded on demand."""
    catalog = context.runtime_skill_catalog
    if catalog is None:
        return spec.system_prompt
    selected = context.runtime_skill_names or spec.runtime_skill_names
    selection = catalog.select(
        selected,
        max_bytes=context.runtime_skill_budget,
        request_id=context.runtime_skill_request_id,
        dispatch_id=(context.runtime_skill_dispatch_id or f"{context.task_id}:{context.agent_id}"),
    )
    parts = [spec.system_prompt, catalog.index_text()]
    if selection.rendered:
        parts.append(selection.rendered)
    return "\n\n".join(parts)


def runtime_skill_event_payload(context: AuditAgentContext) -> dict[str, object]:
    """Return canonical evidence for the selected MIRA runtime projection."""
    catalog = context.runtime_skill_catalog
    if catalog is None:
        return {}
    return catalog.selection_event_payload()


def validate_audit_agent_context(
    spec: AuditAgentSpec, audit_input: AuditInput, context: AuditAgentContext
) -> None:
    """Reject invalid execution dependencies before any lifecycle event is appended."""
    _require_id_kind(context.agent_id, field_name="agent_id", expected="agent")
    _require_id_kind(context.task_id, field_name="task_id", expected="task")
    if audit_input.run_id != context.event_log.run_id:
        raise ValueError("AuditInput.run_id must match the runner event log run_id")
    if any(event.run_id != audit_input.run_id for event in audit_input.events):
        raise ValueError("AuditInput events must all belong to AuditInput.run_id")
    if not spec.task_kinds:
        raise ValueError("AuditAgentSpec.task_kinds must not be empty")
    limits = context.projection_limits
    if any(
        limit <= 0
        for limit in (
            limits.total_chars,
            limits.event_chars,
            limits.agent_message_chars,
            limits.excerpt_chars,
            limits.report_chars,
        )
    ):
        raise ValueError("evidence projection limits must be positive")
    registry = context.tool_registry
    tool_context = context.tool_context
    if (registry is None) is not (tool_context is None):
        raise ValueError("ToolRegistry and ToolContext must be provided together")
    if tool_context is None:
        return
    if tool_context.run_id != audit_input.run_id:
        raise ValueError("tool context run_id must match the runner input")
    if tool_context.agent_id != context.agent_id:
        raise ValueError("tool context agent_id must match the runner context")
    if tool_context.task_id != context.task_id:
        raise ValueError("tool context task_id must match the runner context")
    if tool_context.event_log is not context.event_log:
        raise ValueError("tool context event_log must match the runner event log")


def _require_id_kind(value: str, *, field_name: str, expected: str) -> None:
    """Reject a malformed or wrongly prefixed execution identity."""
    article = "an" if expected[0] in "aeiou" else "a"
    message = f"{field_name} must be {article} {expected} id"
    try:
        actual = id_kind(value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if actual != expected:
        raise ValueError(message)


def _build_evidence_projection(audit_input: AuditInput, context: AuditAgentContext) -> str:
    """Return a redacted, deterministic projection limited to authorised MIRA evidence."""
    limits = context.projection_limits
    excerpts = _authorised_excerpts(
        audit_input.report, context.evidence_reader, limits.excerpt_chars
    )
    selected_events, agent_messages = _select_evidence_events(audit_input.events)
    projection = {
        "run_id": audit_input.run_id,
        "events": [_event_projection(event, limits.event_chars) for event in selected_events],
        "agent_messages": [
            _event_projection(event, limits.agent_message_chars) for event in agent_messages
        ],
        "audit_report": _report_projection(audit_input.report, limits.report_chars),
        "artifact_names": audit_input.artifact_names,
        "experiment_ids": audit_input.experiment_ids,
        "frameworks": tuple(framework.value for framework in audit_input.frameworks),
        "artifact_excerpts": excerpts,
    }
    rendered = canonical_json(redact_value(projection))
    try:
        return frame_untrusted(rendered, label="mira-evidence", max_chars=limits.total_chars)
    except ValueError:
        # A complete frame is safer than a partial one. With an unrealistically small caller
        # budget, omit the evidence rather than returning a payload whose closing delimiter was
        # truncated and could be mistaken for an instruction boundary.
        return _truncate(
            "MIRA evidence omitted: the configured budget cannot fit a complete safety frame.",
            limits.total_chars,
        )


def _select_evidence_events(events: Sequence[Event]) -> tuple[tuple[Event, ...], tuple[Event, ...]]:
    """Select typed Run evidence and the latest bounded narrative messages."""
    messages = tuple(event for event in events if event.type is EventType.AGENT_MESSAGE)
    latest_messages = messages[-_MAX_AGENT_MESSAGES:]
    selected = tuple(event for event in events if event.type in _AUDIT_EVENT_TYPES)
    return selected, latest_messages


def _report_projection(report: AuditReport | None, max_chars: int) -> str | None:
    """Render the deterministic report under its own cap, like every other bounded section.

    ``canonical_json`` sorts keys, so ``audit_report`` is serialized before ``events``. Left
    uncapped, one oversized report made the single tail truncation in
    :func:`_build_evidence_projection` delete the whole events section — the primary evidence the
    agent reasons over — while ``event_chars`` suggested each event was individually bounded.
    Capping the report here keeps every section of the projection genuinely independent.
    """
    if report is None:
        return None
    return _truncate(canonical_json(redact_value(report.model_dump(mode="json"))), max_chars)


def _event_projection(event: Event, max_chars: int) -> str:
    """Render one selected event without exposing envelope data unrelated to the model."""
    value = {
        "seq": event.seq,
        "type": event.type.value,
        "payload": redact_value(event.payload),
    }
    return _truncate(canonical_json(value), max_chars)


def _authorised_excerpts(
    report: AuditReport | None,
    reader: EvidenceReader | None,
    max_chars: int,
) -> tuple[dict[str, object], ...]:
    """Read only report-cited artifacts whose expected and actual digests agree."""
    if report is None or reader is None:
        return ()
    excerpts: list[dict[str, object]] = []
    for evidence in _artifact_evidence(report)[:_MAX_ARTIFACT_EXCERPTS]:
        try:
            excerpt = reader.read_excerpt(evidence, max_chars=max_chars)
        except (EvidenceLimitError, EvidenceContentError) as exc:
            # A binary or non-UTF-8 cited artifact (a PDF, a joblib model) is bounded
            # unavailable evidence, exactly like an oversized one; reference and integrity
            # errors still fail closed. Before this, one cited PDF ended the whole Run as
            # FAILED instead of an audit finding.
            excerpts.append(
                {
                    "ref": evidence.ref,
                    "sha256": evidence.sha256,
                    "text": None,
                    "truncated": True,
                    "unavailable": str(exc),
                }
            )
            continue
        if excerpt.evidence != evidence:
            raise ValueError("evidence reader returned an excerpt for another evidence reference")
        if excerpt.expected_sha256 != evidence.sha256 or excerpt.actual_sha256 != evidence.sha256:
            raise ValueError("evidence excerpt sha256 does not match the report-pinned digest")
        text = _truncate(redact_value(excerpt.text), max_chars)
        excerpts.append(
            {
                "ref": evidence.ref,
                "sha256": evidence.sha256,
                "text": text,
                "truncated": excerpt.truncated or text != excerpt.text,
            }
        )
    return tuple(excerpts)


def _artifact_evidence(report: AuditReport) -> tuple[Evidence, ...]:
    """Return unique, hash-pinned artifact evidence cited by the deterministic report."""
    evidence = (
        *(reference for control in report.controls for reference in control.evidence),
        *(reference for finding in report.findings for reference in finding.evidence),
    )
    unique: dict[tuple[str, str, str], Evidence] = {}
    for reference in evidence:
        if reference.kind == "artifact" and reference.sha256 is not None:
            unique.setdefault((reference.kind, reference.ref, reference.sha256), reference)
    return tuple(unique.values())


def _truncate(text: str, max_chars: int) -> str:
    """Return a deterministic bounded string, marking every shortened value with an ellipsis."""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1]}…"


def build_audit_tools(
    spec: AuditAgentSpec, context: AuditAgentContext
) -> tuple[PydanticTool[None], ...]:
    """Expose the MIRA registry through the manager with the spec allowlist as a hard guard."""
    if context.tool_registry is None or context.tool_context is None:
        return ()
    manager = ToolManager(context.tool_registry)
    tool_context = replace(
        context.tool_context,
        allowed_tools=frozenset(spec.tool_allowlist),
    )
    return tuple(_build_audit_tool(manager, tool_context, tool) for tool in context.tool_registry)


def _build_audit_tool(
    manager: ToolManager, context: ToolContext, tool: ToolContract
) -> PydanticTool[None]:
    """Adapt one registered tool without recreating its authorisation or execution logic."""
    tool_name = tool.name

    def call(**arguments: Any) -> str:
        """Delegate one model tool request to the Tool Manager."""
        execution = manager.execute(context, tool_name, arguments)
        return frame_untrusted(render_tool_output(execution), label="mira-tool-output")

    return PydanticTool.from_schema(
        call,
        name=tool_name,
        description=tool.description,
        json_schema=input_schema(tool),
    )


def _normalise_finding(
    content: _FindingContent,
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
) -> AuditFinding:
    """Assign all authority-owned finding identity fields in code rather than from model output."""
    return safe_finding(
        AuditFinding(
            id=new_id("finding"),
            run_id=audit_input.run_id,
            agent_id=context.agent_id,
            control_id=f"{_MODEL_CONTROL_ID_PREFIX}{content.control_id}",
            framework=spec.framework,
            title=content.title,
            finding=content.finding,
            severity=content.severity,
            confidence=content.confidence,
            evidence=content.evidence,
            recommendation=content.recommendation,
            created_at=utc_now(),
        )
    )


def _step_usage_recorder(
    step_usage: RunUsage, delegate: Callable[[LLMResponse], None] | None
) -> Callable[[LLMResponse], None]:
    """Charge one invocation's own accumulator, then the caller's real ledger.

    `AuditAgentContext` carries no `RunUsage` of its own (unlike `AgentContext`), so nothing
    before this diffed a per-step cost the way `runner.py`'s `AGENT_COMPLETED` does for THY --
    MIRA's own `AGENT_COMPLETED` always recorded `cost_usd`-shaped evidence as absent, even though
    the real cost was already being charged into the run-wide `UsageLedger` via
    `context.record_model_usage` (`_model_usage_recorder`, unaffected here: `delegate` is still
    called with the exact same `response`, so budget enforcement is unchanged). `step_usage` only
    has one job: hold the numbers long enough for `_usage_payload` to attach them to this
    invocation's own completion/failure events.
    """

    def record(response: LLMResponse) -> None:
        step_usage.charge(response)
        if delegate is not None:
            delegate(response)

    return record


def _usage_payload(step_usage: RunUsage) -> dict[str, Any]:
    """Return this invocation's own token/cost evidence for an `AGENT_COMPLETED` payload.

    Mirrors the shape `runner.py` (`thymira.agents`) already attaches for THY's own agents
    (`input_tokens`/`output_tokens`/`cost_usd`), so the two producers of that event carry the same
    fields. `cost_usd` stays `None` exactly when `RunUsage.charge` already degraded it to unknown
    (no price for the routed model) -- never coerced to `0.0`, for the same reason `RunUsage`'s own
    docstring gives: an unpriced call must not look like a free one.
    """
    return {
        "input_tokens": step_usage.input_tokens,
        "output_tokens": step_usage.output_tokens,
        "cost_usd": step_usage.cost_usd,
    }


def _last_model_choice(events: Sequence[Event]) -> ModelChoice | None:
    """Return the last router selection emitted in this execution cycle, if any."""
    selected = [event for event in events if event.type is EventType.MODEL_SELECTED]
    if not selected:
        return None
    payload = selected[-1].payload
    return ModelChoice.model_validate(
        {field: payload[field] for field in ModelChoice.model_fields if field in payload}
    )


__all__ = [
    "AuditAgentContext",
    "EvidenceExcerpt",
    "EvidenceProjectionLimits",
    "EvidenceReader",
    "build_audit_tools",
    "run_audit_agent",
    "runtime_skill_instructions",
    "validate_audit_agent_context",
]
