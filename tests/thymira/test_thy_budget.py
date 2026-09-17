"""Token budgeting / context-window management: compact before an overflowing call (THY-26)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents import PromptBuilder, load_agent_specs
from thymira.agents.llm.base import LLMResponse
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.schemas import Actor, EventSurface, EventType, ModelRoutePolicy, Task, new_id
from thymira.thy import (
    CompactionContext,
    CompactionSummary,
    ContextBudget,
    MeasurementBaseline,
    TokenMeasurement,
    estimate_prompt_tokens,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from thymira.agents import AgentCatalog
    from thymira.schemas import Event

_SPEC_YAML = """\
name: coding
role: agent
task_kinds: [code]
max_turns: 3
max_depth: 1
system_prompt_ref: prompts/coding.md
output_schema_ref: tests.thymira.fixtures_agent_output:DummyOutput
"""

_CHOICE = ModelChoice(
    role=Role.THY,
    task="summarize",
    tier_requested=ModelTier.FAST,
    tier_applied=ModelTier.FAST,
    model="test-model",
    reason="test",
)
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


def _catalog(tmp_path: Path) -> AgentCatalog:
    (tmp_path / "coding.yaml").write_text(_SPEC_YAML, encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coding.md").write_text("You are the coding agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _task(objective: str) -> Task:
    return Task(
        id=new_id("task"), run_id=new_id("run"), agent_id=new_id("agent"), objective=objective
    )


def _log_with_messages(texts: Sequence[str]) -> tuple[InMemoryEventLog, list[Event]]:
    log = InMemoryEventLog(new_id("run"))
    events = [
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": text},
            surface=EventSurface.MODEL_VISIBLE,
        )
        for text in texts
    ]
    return log, events


def test_a_prompt_over_the_window_is_compacted_before_the_call_and_then_fits(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("coding")
    builder = PromptBuilder(catalog)
    log, _ = _log_with_messages(
        [
            "analysis " * 40,
            "analysis " * 40,
            "analysis " * 40,
            "analysis " * 40,
            "analysis " * 40,
            "recent",
        ]
    )
    task = _task("finish the analysis")
    # The window has to hold what is actually sent: the rendered history reaches the model inside
    # one untrusted-data frame whose wrapper alone costs ~98 estimated tokens, so a window under
    # that floor is unsatisfiable for any surface. 200 leaves room for the frame and still forces
    # compaction -- and it only fits once the compaction target subtracts the frame.
    budget = ContextBudget(window=200, model="test-model")
    assert budget.exceeds(builder.build(spec, task, log.events()))
    ctx = CompactionContext(
        provider=ScriptedProvider(
            [CompactionSummary(summary="Earlier steps explored and modelled the data.")]
        ),
        choice=_CHOICE,
        event_log=log,
        actor=Actor.system(),
        route_policy=TEST_ROUTE_POLICY,
    )

    outcome = budget.fit(builder, spec, task, log.events(), ctx)

    # Compaction ran, and it is already on the log before any model call would be made.
    assert outcome.compacted is not None
    assert outcome.compacted.type is EventType.CONTEXT_COMPACTED
    assert any(event.type is EventType.CONTEXT_COMPACTED for event in log.events())
    # The reassembled prompt now fits the window.
    assert not budget.exceeds(outcome.assembled)
    assert outcome.measurement.tokens <= budget.limit
    assert outcome.measurement.baseline is MeasurementBaseline.ESTIMATED


def test_a_prompt_within_the_window_is_left_untouched(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("coding")
    builder = PromptBuilder(catalog)
    log, _ = _log_with_messages(["small a", "small b"])
    task = _task("carry on")
    budget = ContextBudget(window=10_000, model="test-model")
    ctx = CompactionContext(
        provider=ScriptedProvider([CompactionSummary(summary="unused")]),
        choice=_CHOICE,
        event_log=log,
        actor=Actor.system(),
        route_policy=TEST_ROUTE_POLICY,
    )

    outcome = budget.fit(builder, spec, task, log.events(), ctx)

    assert outcome.compacted is None
    assert len(log.events()) == 2
    assert not any(event.type is EventType.CONTEXT_COMPACTED for event in log.events())


def test_a_measurement_declares_whether_it_is_usage_or_estimated(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("coding")
    builder = PromptBuilder(catalog)
    assembled = builder.build(spec, _task("profile the data"), events=[])

    estimated = estimate_prompt_tokens(assembled, "test-model")

    assert estimated.baseline is MeasurementBaseline.ESTIMATED
    assert estimated.model == "test-model"
    assert estimated.tokens >= 0

    response = LLMResponse(text="ok", provider="test", model="m", input_tokens=42, output_tokens=5)
    measured = TokenMeasurement.from_usage(response)

    assert measured.baseline is MeasurementBaseline.USAGE
    assert measured.tokens == 42
