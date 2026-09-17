"""RoutedModel: PydanticAI Model backed by thymira's own LLMProvider + router (THY-02)."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    LLMConfigurationError,
    LLMResponse,
    LLMStructuredOutputError,
    LLMToolCall,
    RunUsage,
    UsageLimitExceededError,
    UsageLimits,
)
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.model_binding import routed_model
from thymira.core import UsageLedger
from thymira.events import InMemoryEventLog
from thymira.policies import BudgetRule, Gate, ModelRule, Policy, PolicyEngine
from thymira.schemas import Decision, EventType, ModelRoutePolicy, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.agents import LLMMessage
    from thymira.agents.llm import BaseModelT

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(
    ("test-model", "rogue-model"), authority="code-owned-tests"
)


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give direct provider seams a code-owned route snapshot and deterministic model choice."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _log() -> InMemoryEventLog:
    return InMemoryEventLog(new_id("run"))


class _MeteredInvalidThenValidProvider(ScriptedProvider):
    """Return one metered parse failure and then one valid structured response."""

    def __init__(self) -> None:
        super().__init__([])

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        self.calls.append({"prompt": prompt, "system": system, "tools": []})
        if len(self.calls) == 1:
            response = LLMResponse(
                text="not-json-private-marker",
                provider=self.provider_name,
                model=self.model,
                input_tokens=7,
                output_tokens=3,
                cost_usd=0.04,
            )
            raise LLMStructuredOutputError(
                "the response does not satisfy the expected schema", response=response
            )
        validated = schema.model_validate({"row_count": 1, "columns": ["x"]})
        return validated, LLMResponse(
            text=validated.model_dump_json(),
            provider=self.provider_name,
            model=self.model,
            input_tokens=11,
            output_tokens=5,
            cost_usd=0.06,
        )


class _UnsafeStructuredCauseProvider(ScriptedProvider):
    """Expose a deliberately unsafe cause to exercise the routed provider boundary."""

    def __init__(self, private_marker: str) -> None:
        super().__init__([])
        self._private_marker = private_marker

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        self.calls.append({"prompt": prompt, "system": system, "tools": []})
        unsafe_cause = ValueError(self._private_marker)
        raise LLMStructuredOutputError(
            f"the response does not satisfy {schema.__name__}"
        ) from unsafe_cause


def test_one_agent_request_appends_exactly_one_model_selected_event() -> None:
    log = _log()
    provider = ScriptedProvider(["hi"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    Agent(model=model).run_sync("say hi")

    selected = [e for e in log.events() if e.type == EventType.MODEL_SELECTED]
    assert len(selected) == 1


def test_tier_applied_respects_the_agent_floor() -> None:
    log = _log()
    provider = ScriptedProvider(["hi"])
    # "select_tool" defaults to FAST, which is exactly the AGENT floor -- not raised.
    model = routed_model(
        Role.AGENT, "select_tool", log, provider=provider, route_policy=TEST_ROUTE_POLICY
    )

    Agent(model=model).run_sync("pick a tool")

    payload = next(e for e in log.events() if e.type == EventType.MODEL_SELECTED).payload
    assert payload["tier_applied"] == ModelTier.FAST.value


def test_a_requested_tier_below_the_floor_is_raised() -> None:
    log = _log()
    provider = ScriptedProvider(["hi"])
    # MIRA's floor is STANDARD; requesting FAST must still be raised to STANDARD.
    model = routed_model(
        Role.MIRA,
        "select_tool",
        log,
        provider=provider,
        requested_tier=ModelTier.FAST,
        route_policy=TEST_ROUTE_POLICY,
    )

    Agent(model=model).run_sync("judge this")

    payload = next(e for e in log.events() if e.type == EventType.MODEL_SELECTED).payload
    assert payload["tier_applied"] == ModelTier.STANDARD.value
    assert "floor" in payload["reason"]


def test_no_network_call_the_scripted_provider_answers_directly() -> None:
    log = _log()
    provider = ScriptedProvider(["hello from scripted"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    result = Agent(model=model).run_sync("say hi")

    assert result.output == "hello from scripted"
    assert provider.calls == [{"prompt": "say hi", "system": "", "tools": []}]


def test_provider_never_receives_a_known_environment_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "known-provider-value"  # noqa: S105  # test fixture credential
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    log = _log()
    provider = ScriptedProvider(["hi"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    Agent(model=model, instructions=f"Use {secret} only internally").run_sync(
        f"request contains {secret}"
    )

    assert secret not in str(provider.calls)
    assert all("[REDACTED:CREDENTIAL]" in str(call) for call in provider.calls)


def test_explicit_proxy_cannot_be_silently_bypassed_by_an_injected_provider() -> None:
    endpoint = "https://gateway.example.test/v1"
    provider = ScriptedProvider(["must not run"])
    model = routed_model(
        Role.AGENT,
        "code",
        _log(),
        provider=provider,
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
    )

    with pytest.raises(LLMConfigurationError, match="injected provider"):
        Agent(model=model).run_sync("must refuse")

    assert provider.calls == []


def test_output_carries_exactly_one_request() -> None:
    log = _log()
    provider = ScriptedProvider(["hi"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    result = Agent(model=model).run_sync("say hi")

    # PydanticAI estimates its own token counts from the message content when a model reports
    # none (the ScriptedProvider does not); `requests` is the count this binding controls
    # directly, one per model.selected event.
    assert result.usage.requests == 1


def test_instructions_reach_the_provider_as_system_on_the_no_tool_path() -> None:
    # Every call site builds its PydanticAgent with instructions=, not system_prompt=; that text
    # must reach the provider as `system`, not be silently dropped.
    log = _log()
    provider = ScriptedProvider(["hi"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    Agent(model=model, instructions="You are the data agent.").run_sync("say hi")

    assert provider.calls == [
        {"prompt": "say hi", "system": "You are the data agent.", "tools": []}
    ]


def test_instructions_reach_the_provider_as_system_on_the_structured_path() -> None:
    # The structured-output branch (no callable tools, output_schema set) must carry `system` too.
    log = _log()
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    model = routed_model(
        Role.AGENT,
        "analyze",
        log,
        provider=provider,
        output_schema=DataProfileOutput,
        route_policy=TEST_ROUTE_POLICY,
    )

    Agent(
        model=model, output_type=DataProfileOutput, instructions="You are the data agent."
    ).run_sync("profile it")

    assert provider.calls == [
        {"prompt": "profile it", "system": "You are the data agent.", "tools": []}
    ]


def test_instructions_reach_the_provider_as_system_on_the_tool_path() -> None:
    # The tool path translates the whole history; instructions ride on every ModelRequest but must
    # reach the provider as `system` exactly once per turn, not be dropped or duplicated.
    log = _log()
    provider = ScriptedProvider(
        [LLMToolCall(id="call-1", name="echo", arguments={"value": "hi"}), "done"]
    )

    def echo(**_: object) -> str:
        return "echoed"

    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )

    Agent(model=model, tools=[tool], instructions="You are the coding agent.").run_sync("use echo")

    assert [call["system"] for call in provider.calls] == [
        "You are the coding agent.",
        "You are the coding agent.",
    ]


def test_system_prompt_and_instructions_are_combined_with_the_system_prompt_first() -> None:
    # When a caller supplies BOTH system_prompt= and instructions=, both are system-level content
    # the model must see; the documented order is the system-prompt text first, instructions last.
    log = _log()
    provider = ScriptedProvider(["hi"])
    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    Agent(model=model, system_prompt="SYS", instructions="INSTR").run_sync("say hi")

    assert provider.calls == [{"prompt": "say hi", "system": "SYS\nINSTR", "tools": []}]


def test_a_routed_model_executes_a_provider_requested_tool_before_finishing() -> None:
    log = _log()
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="echo", arguments={"value": "hi"}),
            "done",
        ]
    )
    seen: list[dict[str, object]] = []

    def echo(**arguments: object) -> str:
        seen.append(arguments)
        return "echoed"

    model = routed_model(Role.AGENT, "code", log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )

    result = Agent(model=model, tools=[tool]).run_sync("use echo")

    assert result.output == "done"
    assert seen == [{"value": "hi"}]
    assert len(provider.calls) == 2


def test_tool_retry_preserves_the_original_tool_call_identity_for_the_provider() -> None:
    """An unavailable tool must be answered as that tool call, not as a new user turn."""
    observed: list[LLMMessage] = []

    def echo(**_: object) -> str:
        return "echoed"

    def finish_after_retry(messages: Sequence[LLMMessage]) -> str:
        observed.extend(messages)
        return "done"

    provider = ScriptedProvider(
        [
            LLMToolCall(id="missing-call", name="edit_file", arguments={}),
            finish_after_retry,
        ]
    )
    model = routed_model(
        Role.AGENT,
        "code",
        _log(),
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
    )
    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={"type": "object"},
    )

    result = Agent(model=model, tools=[tool], retries=1).run_sync("edit a file")

    assert result.output == "done"
    retry = observed[-1]
    assert retry.role == "tool"
    assert retry.tool_call_id == "missing-call"
    assert "Unknown tool name" in retry.content
    assert "Fix the errors and try again" in retry.content


def test_provider_boundary_guard_observes_actual_request_before_gateway_dispatch() -> None:
    """A guard at the FunctionModel seam can reject a request before the provider is called."""
    log = _log()
    provider = ScriptedProvider(["must not run"])
    observed: dict[str, object] = {}

    def guard(messages: object, tools: object) -> None:
        observed["messages"] = messages
        observed["tools"] = tools
        raise RuntimeError("request rejected by test guard")

    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        before_provider_call=guard,
        route_policy=TEST_ROUTE_POLICY,
    )

    with pytest.raises(RuntimeError, match="request rejected"):
        Agent(model=model, instructions="current instructions").run_sync("current context")

    assert provider.calls == []
    assert observed["messages"]
    assert observed["tools"] == ()


def test_owner_request_hook_runs_for_every_provider_request_including_tool_turns() -> None:
    log = _log()
    provider = ScriptedProvider([LLMToolCall(id="call-1", name="echo", arguments={}), "done"])
    seen: list[int] = []

    def before_request() -> tuple[str, ...]:
        seen.append(len(seen) + 1)
        return (f"live-steer-{seen[-1]}",)

    def echo(**_: object) -> str:
        return "echoed"

    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        before_model_request=before_request,
        route_policy=TEST_ROUTE_POLICY,
    )
    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={"type": "object"},
    )

    Agent(model=model, tools=[tool]).run_sync("use echo")

    assert seen == [1, 2]
    assert "live-steer-1" in provider.calls[0]["prompt"]
    assert "live-steer-2" in provider.calls[1]["prompt"]


def test_owner_request_hook_runs_again_on_a_structured_retry() -> None:
    log = _log()
    provider = ScriptedProvider(["not valid json", DataProfileOutput(row_count=1, columns=("x",))])
    seen: list[int] = []

    def before_request() -> tuple[str, ...]:
        seen.append(len(seen) + 1)
        return (f"live-steer-{seen[-1]}",)

    model = routed_model(
        Role.AGENT,
        "analyze",
        log,
        provider=provider,
        output_schema=DataProfileOutput,
        before_model_request=before_request,
        route_policy=TEST_ROUTE_POLICY,
    )

    Agent(model=model, output_type=DataProfileOutput, retries=1).run_sync("profile it")

    assert seen == [1, 2]
    assert "live-steer-1" in provider.calls[0]["prompt"]
    assert "live-steer-2" in provider.calls[1]["prompt"]


def test_structured_parse_failure_consumes_request_limit_before_retry() -> None:
    log = _log()
    provider = _MeteredInvalidThenValidProvider()
    usage = RunUsage(limits=UsageLimits(max_requests=1))
    recorded: list[LLMResponse] = []
    model = routed_model(
        Role.AGENT,
        "analyze",
        log,
        provider=provider,
        output_schema=DataProfileOutput,
        usage=usage,
        record_model_usage=recorded.append,
        route_policy=TEST_ROUTE_POLICY,
    )

    with pytest.raises(UsageLimitExceededError, match="max_requests"):
        Agent(model=model, output_type=DataProfileOutput, retries=1).run_sync("profile it")

    assert len(provider.calls) == 1
    assert usage.requests == 1
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3
    assert usage.cost_usd == pytest.approx(0.04)
    assert recorded == [
        LLMResponse(
            text="not-json-private-marker",
            provider="test",
            model="scripted",
            input_tokens=7,
            output_tokens=3,
            cost_usd=0.04,
        )
    ]


def test_structured_parse_failure_and_retry_are_both_charged() -> None:
    provider = _MeteredInvalidThenValidProvider()
    usage = RunUsage()
    recorded: list[LLMResponse] = []
    model = routed_model(
        Role.AGENT,
        "analyze",
        _log(),
        provider=provider,
        output_schema=DataProfileOutput,
        usage=usage,
        record_model_usage=recorded.append,
        route_policy=TEST_ROUTE_POLICY,
    )

    result = Agent(model=model, output_type=DataProfileOutput, retries=1).run_sync("profile it")

    assert result.output == DataProfileOutput(row_count=1, columns=("x",))
    assert len(provider.calls) == 2
    assert usage.requests == 2
    assert usage.input_tokens == 18
    assert usage.output_tokens == 8
    assert usage.cost_usd == pytest.approx(0.1)
    assert [response.text for response in recorded] == [
        "not-json-private-marker",
        '{"row_count":1,"columns":["x"]}',
    ]


def test_structured_retry_traceback_suppresses_an_unsafe_provider_cause() -> None:
    private_marker = "routed-structured-private-marker"
    provider = _UnsafeStructuredCauseProvider(private_marker)
    model = routed_model(
        Role.AGENT,
        "analyze",
        _log(),
        provider=provider,
        output_schema=DataProfileOutput,
        route_policy=TEST_ROUTE_POLICY,
    )

    with pytest.raises(
        UnexpectedModelBehavior, match="Exceeded maximum output retries"
    ) as exc_info:
        Agent(model=model, output_type=DataProfileOutput, retries=0).run_sync("profile it")

    rendered_traceback = "".join(traceback.format_exception(exc_info.value))
    assert private_marker not in rendered_traceback
    assert "the response does not satisfy DataProfileOutput" in rendered_traceback


def test_output_tool_validation_traceback_does_not_expose_arguments() -> None:
    private_marker = "output-tool-private-marker"
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-output",
                name="final_result",
                arguments={"row_count": private_marker, "columns": ["x"]},
            )
        ]
    )

    def echo(**_: object) -> str:
        return "echoed"

    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={"type": "object"},
    )
    model = routed_model(
        Role.AGENT,
        "analyze",
        _log(),
        provider=provider,
        output_schema=DataProfileOutput,
        route_policy=TEST_ROUTE_POLICY,
    )

    with pytest.raises(
        UnexpectedModelBehavior, match="Exceeded maximum output retries"
    ) as exc_info:
        Agent(
            model=model,
            output_type=DataProfileOutput,
            tools=[tool],
            retries=0,
        ).run_sync("profile it")

    rendered_traceback = "".join(traceback.format_exception(exc_info.value))
    assert private_marker not in rendered_traceback
    assert "the model returned invalid DataProfileOutput output" in rendered_traceback


def test_shared_max_requests_is_checked_before_a_follow_up_tool_turn() -> None:
    log = _log()
    provider = ScriptedProvider([LLMToolCall(id="call-1", name="echo", arguments={}), "done"])
    usage = RunUsage(limits=UsageLimits(max_requests=1))

    def echo(**_: object) -> str:
        return "echoed"

    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        usage=usage,
        route_policy=TEST_ROUTE_POLICY,
    )
    tool = Tool.from_schema(
        echo,
        name="echo",
        description="Echo a value.",
        json_schema={"type": "object"},
    )

    with pytest.raises(UsageLimitExceededError, match="max_requests"):
        Agent(model=model, tools=[tool]).run_sync("use echo")

    assert len(provider.calls) == 1
    assert len([event for event in log.events() if event.type is EventType.MODEL_SELECTED]) == 1


def test_gate_budget_blocks_before_a_new_provider_call() -> None:
    """A crossed Core budget is recorded and stops the model before the provider is reached."""
    log = _log()
    provider = ScriptedProvider(["must not run"])
    ledger = UsageLedger()
    ledger.charge(requests=1, tokens=11, cost_usd=0.0)
    policy = Policy(
        name="budget",
        version="1.0",
        budget_rules=(
            BudgetRule(
                id="HARD-TOKENS",
                decision=Decision.BLOCK,
                reason="token budget exhausted",
                max_tokens=10,
            ),
        ),
    )
    gate = Gate(PolicyEngine(policy), log)

    def budget_guard() -> None:
        decision = gate.check_budget(ledger.snapshot(), summary="model budget")
        if decision.decision is Decision.BLOCK:
            raise RuntimeError(decision.reason)

    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
        before_model_selection=budget_guard,
    )

    with pytest.raises(RuntimeError, match="token budget exhausted"):
        Agent(model=model).run_sync("do not call the model")

    assert provider.calls == []
    assert [event.type for event in log.events()] == [EventType.POLICY_DECISION]


def test_gate_blocks_an_unauthorised_model_before_provider_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model rule is checked after routing but before an unauthorized provider call."""
    log = _log()
    provider = ScriptedProvider(["must not run"])
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "rogue-model")
    policy = Policy(
        name="models",
        version="1.0",
        model_rules=(
            ModelRule(
                id="ALLOW-SAFE",
                decision=Decision.BLOCK,
                reason="model is not allow-listed",
                roles=(Role.AGENT.value,),
                allowed_models=("safe-model",),
            ),
        ),
    )
    gate = Gate(PolicyEngine(policy), log)

    def model_guard(choice: ModelChoice) -> None:
        decision = gate.check_model(
            subject_id="agent_test",
            role=choice.role.value,
            model=choice.model,
            tier=choice.tier_applied.value,
            summary="agent model",
        )
        if decision.decision is Decision.BLOCK:
            raise RuntimeError(decision.reason)

    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
        before_model_call=model_guard,
    )

    with pytest.raises(RuntimeError, match="model is not allow-listed"):
        Agent(model=model).run_sync("do not invoke rogue model")

    assert provider.calls == []
    events = log.events()
    assert [event.type for event in events] == [
        EventType.MODEL_SELECTED,
        EventType.POLICY_DECISION,
    ]
    assert events[-1].payload["decision"] == Decision.BLOCK.value
