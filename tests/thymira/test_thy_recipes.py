"""Run recipes as data: a recipe drives a run's agents, phases and budget (THY-28).

A recipe selects a two-experiment phase plan and a per-run token budget; `run_recipe` then drives a
run that uses exactly the recipe's agents and phases and enforces that budget -- a delegated call
that crosses it raises. An invalid recipe (a plan naming an unselected agent) is rejected at load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.agents.llm.base import BaseModelT, LLMCallError, LLMResponse
from thymira.agents.usage import UsageLimitExceededError
from thymira.events import InMemoryEventLog
from thymira.schemas import EventType, ModelRoutePolicy, Run, new_id
from thymira.thy import ThyInput, full_agent_catalog, load_recipe, run_recipe
from thymira.thy.agents.experiment import ExperimentResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.agents.llm.base import LLMMessage, LLMToolDefinition

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct run_recipe provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_RESULT = ExperimentResult(
    parameters={"lr": "0.1"}, metrics={"accuracy": 0.9}, tracker_run_id="mlrun-1"
)


class _TokenProvider:
    """Return a fixed `ExperimentResult` per call, reporting `tokens` prompt tokens each time."""

    provider_name = "test"
    model = "scripted"

    def __init__(self, output: BaseModel, *, tokens: int) -> None:
        self._output = output
        self._tokens = tokens

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModelT], system: str = ""
    ) -> tuple[BaseModelT, LLMResponse]:
        del prompt, system
        validated = schema.model_validate(self._output.model_dump())
        response = LLMResponse(
            text=validated.model_dump_json(),
            provider="test",
            model="scripted",
            input_tokens=self._tokens,
        )
        return validated, response

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        raise LLMCallError("complete is not used in these tests")

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        del messages, tools, parallel_tool_calls
        raise LLMCallError("complete_turn is not used in these tests")


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def _write_recipe(tmp_path: Path, *, max_tokens: int) -> Path:
    recipes_dir = tmp_path / ".thymira" / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = recipes_dir / "two-experiments.yaml"
    path.write_text(
        f"""\
name: two-experiments
metric: accuracy
agents: [experiment]
budget:
  max_tokens: {max_tokens}
policy_ref: credit-risk@1.0
plan:
  - id: e1
    agent: experiment
    phase: execute
    instruction: train the baseline logistic regression
  - id: e2
    agent: experiment
    phase: execute
    instruction: train the gradient boosting challenger
""",
        encoding="utf-8",
    )
    return path


def test_a_recipe_run_uses_exactly_its_agents_and_phases(tmp_path: Path) -> None:
    recipe = load_recipe(_write_recipe(tmp_path, max_tokens=100_000))
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = _TokenProvider(_RESULT, tokens=100)

    output = run_recipe(
        recipe,
        ThyInput(run=run),
        full_agent_catalog(),
        log,
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
    )

    # Exactly the recipe's two experiment phases ran, and both produced an experiment.
    assert recipe.catalog(full_agent_catalog()).names() == ("experiment",)
    assert recipe.provenance()["agents"] == ["experiment"]
    assert recipe.provenance()["phases"] == ["execute", "execute"]
    assert recipe.provenance()["policy_ref"] == "credit-risk@1.0"
    assert output.error is None
    assert len(output.experiment_ids) == 2
    agents_used = {e.payload["agent"] for e in log.events() if e.type is EventType.AGENT_STARTED}
    assert agents_used == {"experiment"}


def test_a_recipe_enforces_its_token_budget(tmp_path: Path) -> None:
    # 150-token budget, 100 tokens per call: the first experiment fits, the second crosses it.
    recipe = load_recipe(_write_recipe(tmp_path, max_tokens=150))
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = _TokenProvider(_RESULT, tokens=100)

    with pytest.raises(UsageLimitExceededError):
        run_recipe(
            recipe,
            ThyInput(run=run),
            full_agent_catalog(),
            log,
            provider=provider,
            route_policy=TEST_ROUTE_POLICY,
        )


def test_an_invalid_recipe_is_rejected_at_load(tmp_path: Path) -> None:
    recipes_dir = tmp_path / ".thymira" / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = recipes_dir / "bad.yaml"
    path.write_text(
        """\
name: bad
agents: [experiment]
plan:
  - id: c1
    agent: coding
    phase: execute
    instruction: run some code
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not in its selection"):
        load_recipe(path)


def test_an_unknown_recipe_key_is_rejected_at_load(tmp_path: Path) -> None:
    recipes_dir = tmp_path / ".thymira" / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = recipes_dir / "typo.yaml"
    path.write_text(
        """\
name: typo
agents: [experiment]
budgets: {max_tokens: 10}
plan:
  - id: e1
    agent: experiment
    phase: execute
    instruction: train it
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid recipe"):
        load_recipe(path)
