"""Unit tests for the shipped EU AI Act audit agent (AUD-EUAIACT).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent`. The regulatory scenario the
roadmap fixes runs with a ``ScriptedProvider`` and a real, seeded :class:`LocalRegulationStore`
reached through the unchanged ``search_regulation`` tool: given a Run whose final decision was taken
without a recorded ``human.approval``, the agent emits an EU AI Act Article 14 finding carrying an
external Evidence reference sourced from the knowledge base, and a Run with a recorded approval
yields no finding. A regulatory finding is evidence about compliance, never a legal determination
and never an authorization, and one test asserts the shipped prompt says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.events import InMemoryEventLog, sha256_text
from thymira.mira.agents.loader import load_default_specs
from thymira.mira.agents.runner import AuditAgentContext, run_audit_agent
from thymira.mira.audit_io import AuditInput
from thymira.mira.checks import AuditReport
from thymira.mira.kb import LocalRegulationStore, RegulationChunk, RegulationSearchResult
from thymira.mira.kb.ingest import load_seed_corpus, write_jsonl
from thymira.mira.tools import SearchRegulation
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve, load_policy_stack
from thymira.schemas import (
    Actor,
    EventSurface,
    EventType,
    Framework,
    ModelRoutePolicy,
    Severity,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.audit_io import AuditAgentOutput
    from thymira.tools import Tool


TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AuditAgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _euaiact_spec() -> AuditAgentSpec:
    """Return the one shipped audit agent declared for the EU_AI_ACT framework."""
    specs = [spec for spec in load_default_specs() if spec.framework is Framework.EU_AI_ACT]
    assert len(specs) == 1, "exactly one shipped EU_AI_ACT audit agent is expected"
    return specs[0]


def _seed_eu_ai_act_store(tmp_path: Path) -> LocalRegulationStore:
    """Seed a local regulation store from the packaged EU AI Act corpus entries."""
    corpus = load_seed_corpus()
    chunks = tuple(
        RegulationChunk.from_text(
            source_id=entry.source_id,
            framework=entry.framework,
            location=entry.location,
            text=entry.text,
        )
        for entry in corpus.entries
        if entry.framework is Framework.EU_AI_ACT
    )
    assert chunks, "the packaged corpus must ship EU AI Act chunks to cite"
    path = tmp_path / "regulation.jsonl"
    write_jsonl(chunks, path)
    return LocalRegulationStore(path)


def _report(run_id: str) -> AuditReport:
    """Return a minimal deterministic report with no findings for one Run."""
    return AuditReport(run_id=run_id, status="passed", controls=(), findings=())


def _tool_context(
    tmp_path: Path, *, log: InMemoryEventLog, agent_id: str, task_id: str
) -> ToolContext:
    """Build a local, authorised Tool Manager context for one MIRA agent."""
    return ToolContext(
        run_id=log.run_id,
        agent_id=agent_id,
        task_id=task_id,
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", log.run_id),
        risk_profile=RiskProfile(risk_level="limited", activity_category="audit", confidence=1.0),
    )


def _missing_approval_input(log: InMemoryEventLog) -> tuple[AuditInput, int]:
    """Record a final decision with no ``human.approval`` and return the input and its seq."""
    log.append(
        EventType.RUN_STARTED,
        Actor.system(),
        {"text": "Run started for automated credit scoring."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    decision = log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {"decision": "ALLOW", "summary": "Final scoring decision issued.", "final": True},
        surface=EventSurface.MODEL_VISIBLE,
    )
    log.append(
        EventType.RUN_COMPLETED,
        Actor.system(),
        {"text": "Run completed; final decision recorded with no human approval."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    return audit_input, decision.seq


def _art14_finding(*, seq: int, external_ref: str, external_sha256: str) -> dict[str, object]:
    """Return one scripted EU AI Act Article 14 human-oversight finding.

    The finding grounds on the run event that recorded the final decision and on the external
    regulation chunk the ``search_regulation`` tool returned from the seeded knowledge base.
    """
    return {
        "findings": [
            {
                "control_id": "EU-AI-ACT-ART-14",
                "title": "Human oversight gap: final decision without a recorded approval",
                "finding": (
                    f"The Run recorded a final decision (event seq:{seq}) with no human.approval, "
                    "so no natural person exercised the oversight Article 14 requires."
                ),
                "severity": "HIGH",
                "confidence": 0.88,
                "evidence": [
                    {"kind": "event", "ref": f"seq:{seq}"},
                    {"kind": "external", "ref": external_ref, "sha256": external_sha256},
                ],
                "recommendation": (
                    "Route consequential decisions through a recorded human.approval before the "
                    "Run completes."
                ),
            }
        ]
    }


def _run_missing_approval_scenario(
    tmp_path: Path,
) -> tuple[AuditAgentOutput, LocalRegulationStore, RegulationSearchResult, InMemoryEventLog]:
    """Run the EU AI Act agent over a final-decision-without-approval Run via the real KB tool."""
    store = _seed_eu_ai_act_store(tmp_path)
    chunk = store.search("human oversight", framework=Framework.EU_AI_ACT, k=1)[0]
    assert chunk.source_id == "eu-ai-act-2024-art-14"
    external_ref = f"{chunk.source_id}/{chunk.location}"

    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _missing_approval_input(log)
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool = SearchRegulation(store)
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={
                    "query": "human oversight article 14",
                    "framework": "EU_AI_ACT",
                    "k": 3,
                },
            ),
            LLMToolCall(
                id="call-output",
                name="final_result",
                arguments=_art14_finding(
                    seq=seq, external_ref=external_ref, external_sha256=chunk.sha256
                ),
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
        tool_context=_tool_context(tmp_path, log=log, agent_id=agent_id, task_id=task_id),
    )
    output = run_audit_agent(_euaiact_spec(), audit_input, context)
    return output, store, chunk, log


def test_load_default_specs_ships_the_eu_ai_act_agent() -> None:
    spec = _euaiact_spec()

    assert spec.name == "euaiact"
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.tool_allowlist == ("search_regulation",)
    assert spec.required_tool_calls == ("search_regulation",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_euaiact_prompt_frames_findings_as_evidence_not_authorization() -> None:
    prompt = _euaiact_spec().system_prompt

    assert "evidence about compliance, not decisions" in prompt
    assert "never a legal determination" in prompt
    assert "never an authorization" in prompt
    assert "Policy Engine" in prompt


def test_euaiact_flags_a_final_decision_without_human_approval(tmp_path: Path) -> None:
    output, _store, chunk, log = _run_missing_approval_scenario(tmp_path)

    assert output.agent_name == "euaiact"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.EU_AI_ACT
    assert finding.control_id == "model:EU-AI-ACT-ART-14"
    assert finding.severity is Severity.HIGH
    assert 0.0 <= finding.confidence <= 1.0

    external = [reference for reference in finding.evidence if reference.kind == "external"]
    assert len(external) == 1
    assert chunk.source_id in external[0].ref

    event_types = [event.type for event in log.events()]
    assert event_types.count(EventType.TOOL_STARTED) == 1
    assert event_types.count(EventType.TOOL_COMPLETED) == 1


def test_euaiact_external_citation_is_sourced_from_the_seeded_kb(tmp_path: Path) -> None:
    output, store, chunk, _log = _run_missing_approval_scenario(tmp_path)

    external = next(
        reference for reference in output.findings[0].evidence if reference.kind == "external"
    )
    assert external.sha256 == chunk.sha256
    assert sha256_text(chunk.fragment) == chunk.sha256
    reloaded = store.search("human oversight", framework=Framework.EU_AI_ACT, k=1)[0]
    assert reloaded.sha256 == external.sha256


def test_euaiact_run_with_recorded_approval_yields_no_finding(tmp_path: Path) -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.POLICY_DECISION,
        Actor.system(),
        {"decision": "REQUIRE_HUMAN_REVIEW", "summary": "Final scoring decision."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {"approved": True, "summary": "Reviewer approved the final decision."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool = SearchRegulation(_seed_eu_ai_act_store(tmp_path))
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={
                    "query": "human oversight article 14",
                    "framework": "EU_AI_ACT",
                    "k": 3,
                },
            ),
            LLMToolCall(id="call-output", name="final_result", arguments={"findings": []}),
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
        tool_context=_tool_context(tmp_path, log=log, agent_id=agent_id, task_id=task_id),
    )

    output = run_audit_agent(_euaiact_spec(), audit_input, context)

    assert output.agent_name == "euaiact"
    assert output.findings == ()
