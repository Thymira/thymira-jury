"""The Research agent: AgentSpec + ResearchResult (claims carry citations) (THY-22)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, Field

from thymira.agents import (
    AgentContext,
    AgentRunner,
    LLMToolCall,
    ScriptedProvider,
    build_agent_tools,
)
from thymira.events import InMemoryEventLog
from thymira.policies import (
    Gate,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import EventType, ModelRoutePolicy, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import RESEARCH_AGENT_SPEC, ResearchResult, research_agent_catalog
from thymira.tools import (
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


# The web-search and Knowledge-Base tools are owned by P3/P1 and do not exist yet (THY-22's Seam);
# these in-process doubles stand in for them. They declare no external effect because a double
# reaches nothing -- so the default policy stack authorises them exactly as it does a local tool.
_SEARCH_HITS = (
    {"source_id": "web-1", "title": "Basel III capital rules", "locator": "https://example/basel"},
)
_KB_HITS = (
    {"source_id": "kb-7", "title": "Prior credit-risk review", "locator": "kb://reviews/2025-11"},
)


class _SearchArguments(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=5, gt=0)


class _KbQueryArguments(BaseModel):
    query: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _FakeSearch:
    """A deterministic stand-in for the P1-owned web-search tool."""

    name: str = "search"
    description: str = "Search the web for sources (test double)."
    arguments_model: type[BaseModel] = _SearchArguments
    result_model = LegacyToolValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="search", external_effects=())
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        del invocation, arguments
        text = json.dumps(_SEARCH_HITS, sort_keys=True)
        return ToolResult(success=True, stdout=text, value=LegacyToolValue(text=text))


@dataclass(frozen=True, slots=True)
class _FakeKbQuery:
    """A deterministic stand-in for the P3-owned Knowledge-Base tool."""

    name: str = "kb_query"
    description: str = "Query the Knowledge Base for documents (test double)."
    arguments_model: type[BaseModel] = _KbQueryArguments
    result_model = LegacyToolValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="kb_query", external_effects=())
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        del invocation, arguments
        text = json.dumps(_KB_HITS, sort_keys=True)
        return ToolResult(success=True, stdout=text, value=LegacyToolValue(text=text))


def _tool_context(tmp_path: Path, *, run_id: str) -> ToolContext:
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def _registry() -> ToolRegistry:
    return ToolRegistry((cast("Tool", _FakeSearch()), cast("Tool", _FakeKbQuery())))


def test_research_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = research_agent_catalog()

    assert catalog.get("research-agent") is RESEARCH_AGENT_SPEC
    assert catalog.output_schema("research-agent") is ResearchResult
    assert catalog.system_prompt("research-agent")


def test_research_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)

    tools = build_agent_tools(RESEARCH_AGENT_SPEC, _registry(), context)

    assert {tool.name for tool in tools} == {"search", "kb_query"}


def test_research_agent_returns_claims_each_carrying_a_source(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="search", arguments={"query": "Basel III capital"}),
            LLMToolCall(id="call-2", name="kb_query", arguments={"query": "credit risk review"}),
            LLMToolCall(
                id="call-3",
                name="final_result",
                arguments={
                    "claims": (
                        {
                            "statement": "Basel III raised minimum capital requirements.",
                            "citations": (
                                {
                                    "source_id": "web-1",
                                    "title": "Basel III capital rules",
                                    "locator": "https://example/basel",
                                },
                            ),
                        },
                        {
                            "statement": "The project already reviewed this model's credit risk.",
                            "citations": (
                                {
                                    "source_id": "kb-7",
                                    "title": "Prior credit-risk review",
                                    "locator": "kb://reviews/2025-11",
                                },
                            ),
                        },
                    )
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Summarise the capital rules and any prior review, with sources.",
    )

    result = AgentRunner().run(
        RESEARCH_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=research_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=_registry(),
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    output = cast("ResearchResult", result.output)
    assert len(output.claims) == 2
    # Every claim carries at least one source reference -- the agent's core contract.
    assert all(claim.citations for claim in output.claims)
    assert all(
        citation.source_id and citation.locator
        for claim in output.claims
        for citation in claim.citations
    )
    # No unallowlisted tool was ever used: only search and kb_query appear on the event log.
    tool_events = [
        event.payload["tool"]
        for event in context.event_log.events()
        if event.type in {EventType.TOOL_STARTED, EventType.TOOL_COMPLETED}
    ]
    assert set(tool_events) == {"search", "kb_query"}
