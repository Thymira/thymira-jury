"""Unit tests for the explicit MIRA specialist dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.mira.agents.dispatcher import dispatch_audit_agent
from thymira.mira.agents.loader import load_default_specs
from thymira.mira.agents.reg_evidence import CitationIndex
from thymira.mira.agents.runner import AuditAgentContext
from thymira.mira.agents.spec import AuditAgentSpec
from thymira.mira.audit_io import AuditInput
from thymira.mira.checks import AuditReport
from thymira.mira.kb import RegulationChunk
from thymira.policies import Gate, PolicyEngine, RiskProfile, ToolCapability, load_policy_stack
from thymira.schemas import (
    Actor,
    AuditFinding,
    EventType,
    Framework,
    ModelRoutePolicy,
    Severity,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import LegacyToolValue, ToolContext, ToolInvocation, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.tools import Tool


TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AuditAgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _spec(name: str, *, tool_allowlist: tuple[str, ...] = ()) -> AuditAgentSpec:
    """Build a valid internal audit-agent specification for one dispatcher branch."""
    return AuditAgentSpec(
        name=name,
        framework=Framework.INTERNAL,
        task_kinds=("audit_judgement",),
        tool_allowlist=tool_allowlist,
        max_turns=3,
        system_prompt="Return only grounded audit evidence.",
    )


def _context(
    log: InMemoryEventLog,
    provider: ScriptedProvider,
    *,
    registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
) -> AuditAgentContext:
    """Build one shared authorized context for a dispatched agent."""
    return AuditAgentContext(
        route_policy=TEST_ROUTE_POLICY,
        event_log=log,
        actor=Actor.system(),
        agent_id=new_id("agent"),
        task_id=new_id("task"),
        provider=provider,
        tool_registry=registry,
        tool_context=tool_context,
    )


def _audit_input(log: InMemoryEventLog, report: AuditReport | None = None) -> AuditInput:
    """Build an immutable audit input from the log and optional deterministic report."""
    if not log.events() and report is None:
        log.append(EventType.RUN_STARTED, Actor.system(), {})
    return AuditInput.from_run(tuple(log.events()), report)


def _finding(run_id: str) -> AuditFinding:
    """Build one finding for the regulatory-evidence enricher to strengthen."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        agent_id=new_id("agent"),
        control_id="AUDIT-EVIDENCE-001",
        framework=Framework.INTERNAL,
        title="Audit evidence needs a source",
        finding="The audit candidate needs a source-backed citation.",
        severity=Severity.MEDIUM,
        confidence=0.8,
    )


def _default_spec(name: str) -> AuditAgentSpec:
    """Return one shipped spec by name."""
    return next(spec for spec in load_default_specs() if spec.name == name)


def _compaction_input() -> tuple[InMemoryEventLog, AuditInput, int]:
    """Build an input containing one compaction and its shadowed event."""
    log = InMemoryEventLog(new_id("run"))
    shadowed = log.append(EventType.AGENT_MESSAGE, Actor.system(), {"text": "tool failed"})
    compaction = log.append(
        EventType.CONTEXT_COMPACTED,
        Actor.system(),
        {
            "shadowed_seqs": [shadowed.seq],
            "envelope": {"provider": "test", "model": "test", "tier": "STANDARD"},
            "summary": "The tool succeeded.",
        },
    )
    return log, _audit_input(log), compaction.seq


def test_dispatcher_uses_the_regulatory_evidence_implementation() -> None:
    """The regulatory spec enriches an existing finding instead of using generic finding output."""
    log = InMemoryEventLog(new_id("run"))
    finding = _finding(log.run_id)
    chunk = RegulationChunk.from_text(
        source_id="internal-audit-source",
        framework=Framework.INTERNAL,
        location="section-1",
        text="Audit evidence must be traceable.",
    )
    provider = ScriptedProvider(
        [{"citations": [{"finding_id": finding.id, "source_id": chunk.source_id}]}]
    )
    report = AuditReport(run_id=log.run_id, status="passed", controls=(), findings=(finding,))

    output = dispatch_audit_agent(
        _default_spec("reg_evidence"),
        _audit_input(log, report),
        _context(log, provider),
        citation_index=CitationIndex.from_chunks((chunk,)),
    )

    assert output.agent_name == "reg_evidence"
    assert output.findings[0].id == finding.id
    assert output.findings[0].evidence[0].kind == "external"
    assert output.model_choice is not None
    assert [event.type for event in log.events()] == [
        EventType.AGENT_STARTED,
        EventType.MODEL_SELECTED,
        EventType.AGENT_COMPLETED,
    ]


def test_dispatcher_uses_the_compaction_fidelity_implementation() -> None:
    """The compaction spec produces a fidelity finding through its bespoke projection."""
    log, audit_input, compaction_seq = _compaction_input()
    provider = ScriptedProvider(
        [
            {
                "gaps": [
                    {
                        "compaction_seq": compaction_seq,
                        "omitted_seqs": [0],
                        "title": "Summary misrepresents a tool result",
                        "finding": "The summary reports success after a failed tool event.",
                        "severity": "HIGH",
                        "confidence": 0.9,
                    }
                ]
            }
        ]
    )

    output = dispatch_audit_agent(
        _default_spec("compaction_fidelity"), audit_input, _context(log, provider)
    )

    assert output.agent_name == "compaction_fidelity"
    assert output.findings[0].control_id == "COMPACTION-FIDELITY"
    assert output.model_choice is not None
    assert sum(event.type is EventType.AGENT_STARTED for event in log.events()) == 1


def test_dispatcher_falls_back_to_the_generic_runner() -> None:
    """A normal spec still uses the generic structured finding runner."""
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(
        [
            {
                "findings": [
                    {
                        "control_id": "GENERIC-001",
                        "title": "Generic finding",
                        "finding": "The generic runner handled this spec.",
                        "severity": "LOW",
                        "confidence": 0.7,
                    }
                ]
            }
        ]
    )

    output = dispatch_audit_agent(_spec("ordinary"), _audit_input(log), _context(log, provider))

    assert output.agent_name == "ordinary"
    assert [finding.control_id for finding in output.findings] == ["model:GENERIC-001"]
    assert output.findings[0].agent_id is not None


@dataclass(slots=True)
class _ReadOnlyTool:
    """A tool double whose invocation can only be observed through Tool Manager events."""

    name: str = "search_regulation"
    description: str = "Return a local regulation result."
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="search_regulation", external_effects=())
    )
    invocations: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Record the narrowed invocation and return a bounded result."""
        del arguments
        self.invocations.append(invocation)
        text = "Article 1: evidence is traceable."
        return ToolResult(success=True, stdout=text, value=LegacyToolValue(text=text))


def test_specialized_agent_tools_still_use_the_tool_manager(tmp_path: Path) -> None:
    """A specialized PydanticAI call reaches tools only through the shared manager adapter."""
    log, audit_input, _compaction_seq = _compaction_input()
    tool = _ReadOnlyTool()
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool_context = ToolContext(
        run_id=log.run_id,
        agent_id=agent_id,
        task_id=task_id,
        workspace=tmp_path,
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", log.run_id),
        risk_profile=RiskProfile(risk_level="limited", activity_category="audit", confidence=1.0),
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="tool-search",
                name="search_regulation",
                arguments={},
            ),
            LLMToolCall(
                id="tool-output",
                name="final_result",
                arguments={"gaps": []},
            ),
        ]
    )
    context = AuditAgentContext(
        route_policy=TEST_ROUTE_POLICY,
        event_log=log,
        actor=Actor.system(),
        agent_id=agent_id,
        task_id=task_id,
        provider=provider,
        tool_registry=ToolRegistry((cast("Tool", tool),)),
        tool_context=tool_context,
    )
    spec = _default_spec("compaction_fidelity").model_copy(
        update={"tool_allowlist": ("search_regulation",), "max_turns": 2}
    )

    dispatch_audit_agent(spec, audit_input, context)

    assert len(tool.invocations) == 1
    assert not hasattr(tool.invocations[0], "event_log")
    assert sum(event.type is EventType.TOOL_STARTED for event in log.events()) == 1
    assert sum(event.type is EventType.TOOL_COMPLETED for event in log.events()) == 1
    assert (
        sum(
            event.type is EventType.POLICY_DECISION
            and event.payload.get("subject_kind") == "tool_call"
            for event in log.events()
        )
        == 1
    )
