"""RunUsage / UsageLimits: shared budget charged per delegated call (THY-07).

**Paired suite.** The identical fail-safe rule -- *a configured cost cap plus an UNKNOWN cost is a
breach, not a pass* -- is implemented twice on purpose, and the second copy is
`thymira.core.usage.UsageLedger`, covered by `tests/thymira/test_core_usage.py`. They are not
merged: `thymira.core` sits *above* `thymira.agents` in the layer order, so a shared helper would
have to be imported upward across a member boundary for a three-line rule; and they react
differently in kind -- the ledger *reports* which limits are breached, `RunUsage` *enforces* by
raising `UsageLimitExceededError`. Change the rule in one and you must change it in the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentRunner,
    LLMMessage,
    LLMResponse,
    LLMToolDefinition,
    RunUsage,
    UsageLimitExceededError,
    UsageLimits,
    load_agent_specs,
)
from thymira.events import InMemoryEventLog
from thymira.schemas import EventType, ModelRoutePolicy, Task, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


@dataclass
class _FakeProvider:
    """A `LLMProvider` double returning pre-scripted `LLMResponse`s with an explicit `cost_usd`.

    `ScriptedProvider` builds its own `LLMResponse` internally and never sets `cost_usd`, so it
    cannot exercise "cost is summed from what the provider actually reported" -- this double can.
    """

    items: list[tuple[BaseModel, LLMResponse]]
    provider_name: str = "test"
    model: str = "fake"
    index: int = field(default=0, init=False)

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        del messages, tools, parallel_tool_calls
        _, response = self.items[self.index]
        self.index += 1
        return response

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        _, response = self.items[self.index]
        self.index += 1
        return response

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModel], system: str = ""
    ) -> tuple[BaseModel, LLMResponse]:
        validated, response = self.items[self.index]
        self.index += 1
        return validated, response


def _response(
    *, model: str, cost_usd: float, input_tokens: int = 10, output_tokens: int = 5
) -> LLMResponse:
    return LLMResponse(
        text="{}",
        provider="test",
        model=model,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def test_charging_sums_cost_and_tokens_across_calls_from_different_models() -> None:
    usage = RunUsage()

    usage.charge(_response(model="model-a", cost_usd=0.01, input_tokens=10, output_tokens=5))
    usage.charge(_response(model="model-b", cost_usd=0.02, input_tokens=20, output_tokens=8))

    assert usage.cost_usd == pytest.approx(0.03)
    assert usage.requests == 2
    assert usage.input_tokens == 30
    assert usage.output_tokens == 13
    assert usage.total_tokens == 43


def _unpriced(model: str = "unpriced") -> LLMResponse:
    """A response from a model LiteLLM has no price for -- new, self-hosted or proxied."""
    return LLMResponse(text="{}", provider="test", model=model, input_tokens=10, output_tokens=5)


def test_charging_a_response_with_no_reported_cost_makes_the_total_unknown() -> None:
    """An unpriced call degrades the accumulated cost to unknown, never to a spendable `0.0`.

    Rewritten from `test_charging_a_response_with_no_reported_cost_charges_zero`, which pinned
    the coercion `cost_usd += response.cost_usd or 0.0` -- the defect itself (finding #40-2).
    """
    usage = RunUsage()

    usage.charge(_unpriced())

    assert usage.cost_usd is None
    # Tokens and requests stay measurable: only the *price* is unknown, not the traffic.
    assert usage.requests == 1
    assert usage.total_tokens == 15


def test_a_configured_cost_ceiling_plus_an_unpriced_response_raises() -> None:
    """The regression for finding #40-2: a ceiling that cannot be shown to hold is a breach.

    The priced total (0.01) is three orders of magnitude below the 100.0 cap, so nothing here
    raises on arithmetic -- it raises because the cap can no longer be *evaluated*.
    """
    usage = RunUsage(limits=UsageLimits(max_cost_usd=100.0))
    usage.charge(_response(model="priced", cost_usd=0.01))

    with pytest.raises(UsageLimitExceededError, match="max_cost_usd"):
        usage.charge(_unpriced())

    assert usage.cost_usd is None


def test_an_unpriced_response_without_a_cost_ceiling_does_not_raise() -> None:
    """No configured cap means nothing to fail safe about; other ceilings still apply."""
    usage = RunUsage(limits=UsageLimits(max_requests=10, max_tokens=1000))

    usage.charge(_unpriced())

    assert usage.cost_usd is None
    assert usage.requests == 1


def test_an_unknown_cost_is_never_repriced_by_a_later_known_charge() -> None:
    """Once the total is unknown it stays unknown; a later priced call cannot launder it."""
    usage = RunUsage()
    usage.charge(_unpriced())

    usage.charge(_response(model="m", cost_usd=0.02))

    assert usage.cost_usd is None
    assert usage.requests == 2


def test_known_costs_accumulate_and_stay_under_a_cap_they_do_not_cross() -> None:
    """The cap is crossed only by a *strictly* greater total; exactly at the cap still passes."""
    usage = RunUsage(limits=UsageLimits(max_cost_usd=0.05))

    usage.charge(_response(model="m", cost_usd=0.03))
    usage.charge(_response(model="m", cost_usd=0.02))

    assert usage.cost_usd == pytest.approx(0.05)
    assert usage.requests == 2


def test_merging_an_unpriced_task_total_keeps_the_run_total_unknown() -> None:
    """`add_cost` is the merge THY's parallel fan-in uses (`thymira.thy.nodes.execute`).

    Each fan-out task charges an isolated `RunUsage`; the run's shared accumulator has to inherit
    the unknown, or the ceiling silently comes back to life after the fan-in.
    """
    run_total = RunUsage(limits=UsageLimits(max_cost_usd=1.0))
    run_total.charge(_response(model="m", cost_usd=0.01))

    run_total.add_cost(None)

    assert run_total.cost_usd is None
    with pytest.raises(UsageLimitExceededError, match="max_cost_usd"):
        run_total.charge(_response(model="m", cost_usd=0.0))


def test_crossing_max_cost_usd_raises_and_does_not_add_a_second_charge() -> None:
    usage = RunUsage(limits=UsageLimits(max_cost_usd=0.05))
    usage.charge(_response(model="m", cost_usd=0.03))

    with pytest.raises(UsageLimitExceededError, match="max_cost_usd"):
        usage.charge(_response(model="m", cost_usd=0.03))

    # The first charge stands (the call happened); the exceeding one is not silently dropped
    # either -- requests still counts it, cost still reflects it, only the caller is told to stop.
    assert usage.requests == 2
    assert usage.cost_usd == pytest.approx(0.06)


def test_crossing_max_tool_calls_raises() -> None:
    usage = RunUsage(limits=UsageLimits(max_tool_calls=1))
    usage.charge_tool()

    with pytest.raises(UsageLimitExceededError, match="max_tool_calls"):
        usage.charge_tool()


def test_an_unset_limit_is_never_enforced() -> None:
    usage = RunUsage(limits=UsageLimits())

    for _ in range(5):
        usage.charge(_response(model="m", cost_usd=1000.0))

    assert usage.requests == 5


def _catalog(tmp_path: Path) -> AgentCatalog:
    (tmp_path / "data.yaml").write_text(
        """\
name: data
role: agent
task_kinds: [analyze]
max_turns: 3
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def test_a_run_that_crosses_max_cost_usd_raises_and_makes_no_further_model_call(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it")
    log = InMemoryEventLog(run_id)
    # Exactly one scripted item: if the runner attempted a second model call, it would fail with
    # an out-of-responses error, not UsageLimitExceededError -- so seeing that specific exception
    # here proves no further call was made.
    provider = _FakeProvider(
        items=[(DataProfileOutput(row_count=1, columns=("a",)), _response(model="m", cost_usd=5.0))]
    )
    usage = RunUsage(limits=UsageLimits(max_cost_usd=1.0))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        usage=usage,
    )

    with pytest.raises(UsageLimitExceededError, match="max_cost_usd"):
        AgentRunner().run(spec, task, ctx)

    assert usage.requests == 1
    assert provider.index == 1


def test_an_unpriced_step_reports_a_null_cost_on_the_agent_completed_event(
    tmp_path: Path,
) -> None:
    """What `cost_usd` renders as for a consumer: JSON `null`, never a spendable `0.0`.

    `AgentRunner._usage_payload` publishes the step's cost as the shared budget's delta across
    the step. With the budget's total unknown that delta is unknown too, and `agent.completed`
    says so rather than reporting a step that cost nothing. No cap is configured here, so the
    run itself is allowed to continue -- this pins the *rendering*, not the enforcement.
    """
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it")
    log = InMemoryEventLog(run_id)
    provider = _FakeProvider(
        items=[(DataProfileOutput(row_count=1, columns=("a",)), _unpriced(model="m"))]
    )
    usage = RunUsage()
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        usage=usage,
    )

    AgentRunner().run(spec, task, ctx)

    completed = next(e.payload for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert completed["cost_usd"] is None
    assert usage.cost_usd is None
