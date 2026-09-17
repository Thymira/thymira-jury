"""RoutedModel: a PydanticAI Model backed by thymira's own LLMProvider and router.

Closes ADR-0001 dec.4's open integration point (see the resolution note there): agents depend on
`routed_model`, never on LiteLLM directly, so the routing/provider mechanism can change without
touching a call site. Built on `pydantic_ai.models.function.FunctionModel` — a `Model`
implementation driven by a plain async function — rather than a raw LiteLLM proxy or a from-scratch
`Model` subclass, so PydanticAI owns the streaming/event-iterator machinery and this module owns
only routing, the `model.selected` event, and translating `LLMResponse` into PydanticAI's types.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from thymira.agents.llm.base import (
    LLMCallError,
    LLMConfigurationError,
    LLMMessage,
    LLMStructuredOutputError,
    LLMToolCall,
    LLMToolDefinition,
)
from thymira.agents.llm.litellm_provider import LiteLLMProvider
from thymira.agents.llm.proxy import resolve_proxy_endpoint
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role, choose
from thymira.agents.prompt_provenance import PromptProvenanceContext, record_prompt
from thymira.agents.request_ledger import RequestLedger, instrument_provider
from thymira.agents.route_policy import enforce_model_route
from thymira.events import current_surface, scrub_credentials, scrub_credentials_value
from thymira.observability import generation, is_enabled, update_current_generation
from thymira.schemas import Actor, EventType

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from pydantic import BaseModel
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.prompts import AssembledPrompt
    from thymira.agents.usage import ConcurrentRunUsage, RunUsage, RunUsageReservation
    from thymira.events import EventLog
    from thymira.observability import Handle
    from thymira.schemas import ModelRoutePolicy
    from thymira.state import ArtifactStore


def _effective_instructions(messages: list[ModelMessage]) -> str:
    """Return the current request's ``ModelRequest.instructions`` (PydanticAI's ``instructions=``).

    PydanticAI attaches the rendered instructions to `ModelRequest.instructions` (a ``str | None``,
    confirmed against pydantic_ai 2.33.0) and emits no part for them. Every ``ModelRequest`` in a
    run carries the same rendered instructions, so the most recent one is taken and emitted once,
    rather than repeated per request across a multi-turn tool history. Returns ``""`` when no
    request set instructions (a caller using ``system_prompt=`` only, or neither).
    """
    for message in reversed(messages):
        if isinstance(message, ModelRequest) and message.instructions is not None:
            return message.instructions
    return ""


def _system_text(messages: list[ModelMessage]) -> str:
    """Combine PydanticAI's two system-level channels into the provider's single ``system`` string.

    PydanticAI carries system-level content two ways: ``system_prompt=`` becomes a
    `SystemPromptPart`, while ``instructions=`` becomes `ModelRequest.instructions` and emits no
    part. Every call site in this repo builds its agent with ``instructions=``, so honouring only
    `SystemPromptPart` dropped the whole behavioural contract of every `AgentSpec`. Both channels
    are collected here.

    When a caller supplies BOTH, both are kept — each is genuine system content the model must see,
    and dropping either would lose part of the contract — with the `SystemPromptPart` text first and
    the ``instructions`` string last, newline-joined. Returns ``""`` when neither is present.
    """
    system_parts: list[str] = [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]
    if instructions := _effective_instructions(messages):
        system_parts.append(instructions)
    return "\n".join(system_parts)


def _extract_prompt_and_system(messages: list[ModelMessage]) -> tuple[str, str]:
    """Flatten PydanticAI's message history into ``(prompt, system)``.

    THY-02's scope is one routed request, not multi-turn history management — THY-03's
    `AgentRunner` owns the conversation shape. Concatenating user text and combining the two
    system-level channels (:func:`_system_text`) is enough for a single-turn call.
    """
    prompt_parts = [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]
    return scrub_credentials("\n".join(prompt_parts)), scrub_credentials(_system_text(messages))


def _request_part_for_provider(part: object) -> LLMMessage | None:
    """Translate one PydanticAI request part.

    `SystemPromptPart` is deliberately not mapped here: all system-level content — both
    ``system_prompt=`` and ``instructions=`` — is folded once into a single leading system message
    by :func:`_messages_for_provider`, so translating it here too would emit it twice.
    """
    if isinstance(part, UserPromptPart) and isinstance(part.content, str):
        return LLMMessage(role="user", content=scrub_credentials(part.content))
    if isinstance(part, ToolReturnPart):
        return LLMMessage(
            role="tool",
            content=scrub_credentials(str(part.content)),
            tool_call_id=part.tool_call_id,
        )
    if isinstance(part, RetryPromptPart):
        content = scrub_credentials(part.model_response())
        if part.tool_name is not None:
            return LLMMessage(
                role="tool",
                content=content,
                tool_call_id=part.tool_call_id,
            )
        return LLMMessage(role="user", content=content)
    return None


def _response_for_provider(message: ModelResponse) -> LLMMessage | None:
    """Translate one PydanticAI response message."""
    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            text_parts.append(scrub_credentials(part.content))
        elif isinstance(part, ToolCallPart):
            raw_args = part.args
            try:
                arguments = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMCallError("PydanticAI produced invalid tool-call arguments") from exc
            if not isinstance(arguments, dict):
                raise LLMCallError("PydanticAI produced non-object tool-call arguments")
            tool_calls.append(
                LLMToolCall(
                    id=part.tool_call_id,
                    name=part.tool_name,
                    arguments=scrub_credentials_value(arguments),
                )
            )
    if not text_parts and not tool_calls:
        return None
    return LLMMessage(role="assistant", content="\n".join(text_parts), tool_calls=tuple(tool_calls))


def _messages_for_provider(messages: list[ModelMessage]) -> tuple[LLMMessage, ...]:
    """Translate PydanticAI history into the provider-neutral conversation contract.

    The combined system-level content (:func:`_system_text`) is emitted once as a single leading
    system message; every request part after it is a user, tool-return or retry turn.
    """
    translated: list[LLMMessage] = []
    if system := _system_text(messages):
        translated.append(LLMMessage(role="system", content=scrub_credentials(system)))
    for message in messages:
        if isinstance(message, ModelRequest):
            translated.extend(
                part
                for raw_part in message.parts
                if (part := _request_part_for_provider(raw_part)) is not None
            )
            continue
        if response := _response_for_provider(message):
            translated.append(response)
    return tuple(translated)


def _tool_definitions(info: AgentInfo) -> tuple[LLMToolDefinition, ...]:
    """Translate the tools PydanticAI exposed on this request."""
    return tuple(
        LLMToolDefinition(
            name=definition.name,
            description=definition.description,
            parameters_json_schema=definition.parameters_json_schema,
        )
        for definition in [*info.function_tools, *info.output_tools]
    )


def _before_provider_call(
    guard: Callable[[Sequence[LLMMessage], Sequence[LLMToolDefinition]], None] | None,
    messages: list[ModelMessage],
    info: AgentInfo,
) -> None:
    """Run the optional guard against the exact provider-facing request.

    PydanticAI may add a deferred tool result or a retry part while it is driving an agent. A
    guard invoked at this seam therefore sees the request that would actually cross the provider
    boundary, rather than the prompt that was assembled before the agent started.
    """
    if guard is not None:
        guard(
            _messages_for_provider(messages),
            _tool_definitions(info) if info.function_tools else (),
        )


def _trace_input(messages: list[ModelMessage], info: AgentInfo) -> Any:
    """Render this request the way Langfuse parses an OpenAI chat request, or nothing when off.

    The shape is load-bearing, not cosmetic. Langfuse derives its **Available Tools** and **Tool
    Calls** views by parsing known request/response formats — its own OpenAI integration records
    `{"tools": [...], "messages": [...]}` when tools are offered and a bare message list when they
    are not (`langfuse/openai.py::_extract_chat_prompt`). Recording the same counts in custom
    metadata instead leaves those views empty, which is exactly what happened here.

    The output tool is deliberately left out of `tools`: PydanticAI exposes the structured result
    as a tool of its own, and counting it as available would report every agent as having one more
    tool than it can actually use.

    Built only while tracing is enabled: flattening the history costs real work on a path every
    model call takes.
    """
    if not is_enabled():
        return None
    rendered = [
        {"role": message.role, "content": message.content}
        for message in _messages_for_provider(messages)
    ]
    if not info.function_tools:
        return rendered
    return {"tools": _openai_tools(info), "messages": rendered}


def _openai_tools(info: AgentInfo) -> list[dict[str, Any]]:
    """The offered tools in the shape an OpenAI request carries them."""
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters_json_schema,
            },
        }
        for definition in info.function_tools
    ]


def _openai_tool_calls(calls: Sequence[LLMToolCall]) -> list[dict[str, Any]]:
    """The model's calls in the shape an OpenAI response carries them.

    `arguments` is a JSON *string*, as OpenAI sends it — Langfuse's parser reads this shape, and
    handing it a dict would leave the Tool Calls view empty for the same reason custom metadata
    did.
    """
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            },
        }
        for call in calls
    ]


_ANSWERED: dict[str, Any] = {"answered_directly": True}
"""What the two tool-less paths record: they cannot call a tool, so they always answered."""


def _model_parameters(provider: LLMProvider) -> dict[str, Any] | None:
    """The per-call settings Langfuse keeps in a field of its own, separate from `metadata`.

    Only what the provider will actually apply: `THYMIRA_REASONING_EFFORT` is configured globally
    but dropped for a model that has no such parameter, and reporting it there anyway would read
    as a setting that shaped an answer it never reached.
    """
    effort = getattr(provider, "reasoning_effort", None)
    if effort is None or not getattr(provider, "applies_reasoning_effort", False):
        return None
    return {"reasoning_effort": effort}


def _record_generation(
    response: LLMResponse, output: object, extra: dict[str, Any] | None = None
) -> None:
    """Put the answering model, its tokens and its cost on the open generation.

    The trace records the model that *answered* (`LLMResponse.model`), not the router's synthetic
    `routed:{role}:{task}` binding: PydanticAI's `FunctionModel` overwrites the response's model
    name with that binding on the way out, so this is the only point where the real id is still in
    hand.

    The cached and reasoning counts travel separately from the totals that contain them. The
    budget ledger is charged the inclusive totals, because that is what the provider bills; the
    trace stores exclusive buckets, because that is what Langfuse prices.
    """
    update_current_generation(
        model=response.model,
        provider=response.provider,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cached_input_tokens=response.cached_input_tokens,
        reasoning_output_tokens=response.reasoning_output_tokens,
        cost_usd=response.cost_usd,
        output=output,
        extra={
            **{
                key: value
                for key, value in response.metadata.items()
                if key.startswith("reasoning_effort")
            },
            **(extra or {}),
        },
    )


def _tool_turn_response(
    active_provider: LLMProvider,
    messages: list[ModelMessage],
    info: AgentInfo,
    output_schema: type[BaseModel] | None,
    usage: RunUsage | None,
    *,
    concurrent_usage: ConcurrentRunUsage | None,
    observation: Handle,
    record_model_usage: Callable[[LLMResponse], None] | None,
) -> ModelResponse:
    """Request a tool-capable turn and translate it back to PydanticAI parts."""
    reservation = concurrent_usage.reserve() if concurrent_usage is not None else None
    try:
        parallel_tool_calls = (
            info.model_settings.get("parallel_tool_calls")
            if info.model_settings is not None
            else None
        )
        response = active_provider.complete_turn(
            _messages_for_provider(messages),
            tools=_tool_definitions(info),
            parallel_tool_calls=parallel_tool_calls,
        )
    except LLMCallError as exc:
        if reservation is not None:
            reservation.cancel()
        observation.update(failed=True, reason=str(exc))
        raise ModelRetry(str(exc)) from exc
    except BaseException:
        if reservation is not None:
            reservation.cancel()
        raise
    _settle_model_response(response, usage, reservation, record_model_usage)
    output_tool_names = {definition.name for definition in info.output_tools}
    # PydanticAI exposes the structured result as a tool of its own, so counting every call the
    # model made would report a turn that simply answered as a turn that used a tool -- and
    # overstate how often agents reach for their tool surface.
    called = [call for call in response.tool_calls if call.name not in output_tool_names]
    _record_generation(
        response,
        {"role": "assistant", "tool_calls": _openai_tool_calls(called)}
        if called
        else response.text or {"role": "assistant", "content": ""},
        {
            # Not the counts -- Langfuse derives those from the shapes above. This is the thing it
            # cannot know: whether the turn ended by producing the structured answer.
            "answered_directly": any(
                call.name in output_tool_names for call in response.tool_calls
            ),
        },
    )
    parts: list[ToolCallPart | TextPart] = []
    for call in response.tool_calls:
        args = call.arguments
        if call.name in output_tool_names and output_schema is not None:
            try:
                args = output_schema.model_validate(args).model_dump(mode="json")
            except ValidationError:
                raise ModelRetry(
                    f"the model returned invalid {output_schema.__name__} output"
                ) from None
        parts.append(ToolCallPart(tool_name=call.name, args=args, tool_call_id=call.id))
    if not parts and response.text:
        parts.append(TextPart(content=response.text))
    if not parts:
        raise ModelRetry("the model returned neither text nor a tool call")
    return ModelResponse(
        parts=parts,
        usage=RequestUsage(
            input_tokens=response.input_tokens, output_tokens=response.output_tokens
        ),
        model_name=response.model,
        provider_name=response.provider,
    )


def _settle_model_response(
    response: LLMResponse,
    usage: RunUsage | None,
    reservation: RunUsageReservation | None,
    record_model_usage: Callable[[LLMResponse], None] | None,
) -> None:
    """Settle shared capacity, then record Core and task-local usage exactly once."""
    if reservation is not None:
        reservation.charge(response)
    if record_model_usage is not None:
        record_model_usage(response)
    if usage is not None:
        usage.charge(response)


def routed_model(  # noqa: PLR0915  # merges routing, provenance, proxy-boundary and runtime-skill evidence
    role: Role,
    task: str,
    event_log: EventLog,
    *,
    requested_tier: ModelTier | None = None,
    provider: LLMProvider | None = None,
    actor: Actor | None = None,
    output_schema: type[BaseModel] | None = None,
    assembled: AssembledPrompt | None = None,
    runtime_skill_evidence: Mapping[str, object] | None = None,
    artifact_store: ArtifactStore | None = None,
    agent_id: str | None = None,
    usage: RunUsage | None = None,
    concurrent_usage: ConcurrentRunUsage | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    before_provider_call: Callable[[Sequence[LLMMessage], Sequence[LLMToolDefinition]], None]
    | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    request_ledger: RequestLedger | None = None,
    request_owner_id: str | None = None,
    bound_choice: ModelChoice | None = None,
    proxy_endpoint: str | None = None,
    declared_safe_endpoints: Collection[str] = (),
    route_policy: ModelRoutePolicy | None = None,
) -> FunctionModel:
    """Build a PydanticAI `Model` that routes each request through thymira's own provider.

    Args:
        role: Who is calling — an orchestrator or a sub-agent (:func:`routing.choose`).
        task: The kind of work, a key of `routing.DEFAULT_TIER_BY_TASK`.
        event_log: Where the `model.selected` event lands, appended before the call.
        requested_tier: A tier the caller proposes; role floors still apply.
        provider: Overrides the router-resolved `LiteLLMProvider` — tests inject a
            `ScriptedProvider` here so no network call happens. Production omits it.
        actor: The event actor; defaults to `Actor.system()`.
        output_schema: When set without callable tools, the provider uses
            `LLMProvider.complete_structured`; with callable tools, the schema is exposed as
            PydanticAI's output tool (`AgentInfo.output_tools`) in `complete_turn`. Both paths
            validate locally before returning the output tool call. An invalid or failed
            completion is raised as `ModelRetry` so PydanticAI's retry loop — bounded by the
            `Agent(retries=...)` the caller sets from `spec.max_turns` — drives the next attempt,
            rather than aborting the run.
        assembled: The step's `AssembledPrompt` (THY-05). Given together with `artifact_store`
            and `agent_id`, every call records prompt provenance (THY-06) instead of the plain
            `model.selected` append below.
        runtime_skill_evidence: Runtime skill selection evidence for a MIRA instruction builder
            that does not use `AssembledPrompt`; it is appended to the canonical `model.selected`
            event and bound to the request-ledger record before provider invocation.
        artifact_store: Where the credential-scrubbed prompt is persisted as a LOG artifact. See
            `assembled`.
        agent_id: The prefixed `agent_*` id recorded as the artifact's `produced_by`. See
            `assembled`.
        usage: The run's shared `RunUsage` accumulator (THY-07). When set, every call charges its
            real `LLMResponse` cost/tokens into it before returning, raising
            `UsageLimitExceededError` (propagated, never swallowed here) the moment a configured
            ceiling is crossed — the call that just finished still happened, but no further one
            does.
        concurrent_usage: Optional run-wide reservation coordinator for genuinely concurrent
            provider calls. It reserves request capacity before dispatch and charges its
            authoritative accumulator when the response arrives.
        before_model_request: Optional owner-bound hook invoked immediately before every provider
            request, including PydanticAI retries and tool turns. Returned text is appended to the
            request as model-visible user content after the caller's existing message history.
        before_model_selection: Optional Core budget check performed before routing.
        before_model_call: Optional Core authorization check performed before provider invocation.
        before_provider_call: Optional guard at the actual provider boundary. It receives the
            provider-neutral messages and tool definitions PydanticAI is about to dispatch.
        record_model_usage: Optional Core hook that records measured provider usage.
        request_ledger: Optional durable ledger for the effective provider request and response.
        request_owner_id: The durable owner of the model-visible facts. When omitted, a stable
            binding label derived from ``role`` and ``task`` is used.
        bound_choice: A persisted choice for a parked continuation. When set, the resume never
            re-runs the router and every provider call uses this exact choice.
        proxy_endpoint: An explicit endpoint for the LiteLLM gateway. It is refused when an
            injected provider is supplied, because that provider cannot be assumed to honour it.
        declared_safe_endpoints: Runtime-owned endpoint allowlist for ``proxy_endpoint``.
        route_policy: Immutable session allowlist checked after selection and before the provider.
    """
    resolved_actor = actor or Actor.system()
    effective_request_ledger = request_ledger
    if effective_request_ledger is None and (
        provider is None or callable(getattr(provider, "attach_request_ledger", None))
    ):
        # Production routing owns the LiteLLM boundary. Tests inject a provider double without an
        # attach seam, so their event expectations remain focused on the provider double rather
        # than an implicit production concern.
        effective_request_ledger = RequestLedger(event_log)

    async def _call(  # noqa: PLR0912, PLR0915  # tracing, hook, evidence and authorization paths
        messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        if provider is not None and proxy_endpoint is not None:
            resolve_proxy_endpoint(proxy_endpoint, declared_safe_endpoints=declared_safe_endpoints)
            raise LLMConfigurationError(
                "an explicit LLM proxy endpoint cannot be used with an injected provider"
            )
        if before_model_request is not None:
            additions = tuple(before_model_request())
            if additions:
                messages.append(
                    ModelRequest(parts=[UserPromptPart(content=content) for content in additions])
                )
        request_prompt, request_system = _extract_prompt_and_system(messages)
        request_surface_seqs = tuple(event.seq for event in current_surface(event_log.events()))
        if before_model_selection is not None:
            before_model_selection()
        if usage is not None and concurrent_usage is None:
            usage.ensure_request_available()
        choice = (
            bound_choice
            if bound_choice is not None
            else choose(role, task, requested_tier=requested_tier)
        )
        prompt_sha256: str | None = None
        selection_evidence = dict(runtime_skill_evidence or {})
        if assembled is not None:
            selection_evidence.update(assembled.runtime_skill_event_payload())
        if (
            effective_request_ledger is None
            and assembled is not None
            and artifact_store is not None
            and agent_id is not None
        ):
            # The returned artifact is the audit-grade correlation key: `model.selected` already
            # carries its sha256, and putting the same value on the generation is what lets a
            # reader go from a span in the trace back to the exact prompt that was sent.
            prompt_sha256 = record_prompt(
                PromptProvenanceContext(
                    artifact_store=artifact_store,
                    event_log=event_log,
                    actor=resolved_actor,
                    agent_id=agent_id,
                ),
                assembled,
                choice,
                rendered_system=request_system,
                rendered_user=request_prompt,
                surface_seqs=request_surface_seqs,
            ).sha256
            selection_event = event_log.events()[-1]
        else:
            selection_payload: dict[str, object] = {**choice.event_payload()}
            selection_payload.update(selection_evidence)
            # Parallel THY delegations use a child log for prompt isolation while their shared
            # request ledger remains the canonical durable writer. Append runtime selections to
            # that same log before the gateway records the request; replaying a child event later
            # would mint a different event id/hash and invalidate the binding.
            selection_log = (
                effective_request_ledger.event_log
                if effective_request_ledger is not None
                and selection_evidence
                and effective_request_ledger.event_log is not event_log
                else event_log
            )
            selection_event = selection_log.append(
                EventType.MODEL_SELECTED,
                resolved_actor,
                selection_payload,
                subject_id=agent_id,
            )
        if selection_event.type is not EventType.MODEL_SELECTED:
            raise LLMCallError("model selection evidence was not appended before provider call")
        # Every provider seam, including injected offline doubles, carries the bound snapshot.
        enforce_model_route(
            choice,
            route_policy,
            event_log=event_log,
            actor=resolved_actor,
            subject_id=agent_id,
        )
        if before_model_call is not None:
            before_model_call(choice)
        owner_id = request_owner_id or f"model-binding:{role.value}:{task}"
        if provider is None and choice.model == "<unconfigured>":
            raise LLMConfigurationError(
                "the persisted model choice is unavailable; an explicit new decision is required"
            )
        active_provider = provider or LiteLLMProvider(
            model=None if choice.model == "<unconfigured>" else choice.model,
            request_ledger=effective_request_ledger,
            request_owner_id=owner_id if effective_request_ledger is not None else None,
            proxy_endpoint=proxy_endpoint,
            declared_safe_endpoints=declared_safe_endpoints,
        )
        if effective_request_ledger is not None and provider is not None:
            active_provider = instrument_provider(
                active_provider, effective_request_ledger, owner_id=owner_id
            )
        bind_selection = getattr(active_provider, "bind_runtime_skill_selection", None)
        if callable(bind_selection):
            bind_selection(selection_event, selection_evidence)
        elif effective_request_ledger is not None and selection_evidence:
            raise LLMCallError(
                "the configured request ledger provider cannot bind runtime skill selection"
            )
        with generation(
            role=role.value,
            task=task,
            input_=_trace_input(messages, _info),
            model=choice.model,
            metadata={
                "tier_requested": choice.tier_requested.value,
                "tier_applied": choice.tier_applied.value,
                "routing_reason": choice.reason,
                **({} if prompt_sha256 is None else {"prompt_sha256": prompt_sha256}),
            },
            model_parameters=_model_parameters(active_provider),
        ) as observation:
            _before_provider_call(before_provider_call, messages, _info)
            if _info.function_tools:
                return _tool_turn_response(
                    active_provider,
                    messages,
                    _info,
                    output_schema,
                    usage,
                    concurrent_usage=concurrent_usage,
                    observation=observation,
                    record_model_usage=record_model_usage,
                )
            prompt, system = request_prompt, request_system
            if output_schema is not None:
                reservation = concurrent_usage.reserve() if concurrent_usage is not None else None
                try:
                    validated, response = active_provider.complete_structured(
                        prompt, schema=output_schema, system=system
                    )
                except LLMStructuredOutputError as exc:
                    observation.update(failed=True, reason=str(exc))
                    # Local schema validation happens after a provider has already answered and
                    # billed the request. Charge that received response before PydanticAI retries;
                    # pre-call structured-output failures carry no response and remain uncharged.
                    if exc.response is not None:
                        _settle_model_response(exc.response, usage, reservation, record_model_usage)
                    elif reservation is not None:
                        reservation.cancel()
                    # A provider's validation cause may contain its raw response (Pydantic's
                    # ``input_value`` is one example). Preserve the safe boundary message while
                    # suppressing the untrusted exception chain from observable tracebacks.
                    raise ModelRetry(str(exc)) from None
                except LLMCallError as exc:
                    if reservation is not None:
                        reservation.cancel()
                    observation.update(failed=True, reason=str(exc))
                    raise ModelRetry(str(exc)) from exc
                except BaseException:
                    if reservation is not None:
                        reservation.cancel()
                    raise
                _settle_model_response(response, usage, reservation, record_model_usage)
                _record_generation(response, validated.model_dump(mode="json"), _ANSWERED)
                tool_name = _info.output_tools[0].name
                return ModelResponse(
                    parts=[
                        ToolCallPart(tool_name=tool_name, args=validated.model_dump(mode="json"))
                    ],
                    usage=RequestUsage(
                        input_tokens=response.input_tokens, output_tokens=response.output_tokens
                    ),
                    model_name=response.model,
                    provider_name=response.provider,
                )
            reservation = concurrent_usage.reserve() if concurrent_usage is not None else None
            try:
                response = active_provider.complete(prompt, system=system)
            except LLMCallError as exc:
                if reservation is not None:
                    reservation.cancel()
                # Re-raised unchanged: this path has never converted a failure into a retry, and
                # the mark exists only so the failed call is visible in the trace.
                observation.update(failed=True, reason=str(exc))
                raise
            except BaseException:
                if reservation is not None:
                    reservation.cancel()
                raise
            _settle_model_response(response, usage, reservation, record_model_usage)
            _record_generation(response, response.text, _ANSWERED)
            return ModelResponse(
                parts=[TextPart(content=response.text)],
                usage=RequestUsage(
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens
                ),
                model_name=response.model,
                provider_name=response.provider,
            )

    return FunctionModel(_call, model_name=f"routed:{role.value}:{task}")


__all__ = ["routed_model"]
