"""Unit tests for the shipped Credit Risk audit agent (AUD-CREDIT-RISK).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent` with a ``ScriptedProvider`` and
a seeded :class:`LocalRegulationStore` reached through the unchanged ``search_regulation`` tool. The
scenario the roadmap fixes: a model trained with a protected attribute yields a ``CREDIT_RISK``
finding of severity >= HIGH carrying an external Evidence citation sourced from the knowledge base,
and a model that holds the protected attribute out of its features yields none.
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
from thymira.mira.kb import (
    LocalRegulationStore,
    RegulationChunk,
    RegulationSearchResult,
    load_default_regulation_store,
)
from thymira.mira.kb.ingest import write_jsonl
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


_PROTECTED_ATTRIBUTE_TEXT = (
    "Consumer-credit fairness rules forbid a creditor from using a protected attribute -- such as "
    "age, sex, race, or marital status -- as a basis for a credit decision, and require that a "
    "protected attribute be held out of the model's features and used only to measure disparity, "
    "with each adverse action explained by the specific principal reasons the decision rested on."
)


def _credit_risk_spec() -> AuditAgentSpec:
    """Return the one shipped audit agent declared for the CREDIT_RISK framework."""
    specs = [spec for spec in load_default_specs() if spec.framework is Framework.CREDIT_RISK]
    assert len(specs) == 1, "exactly one shipped CREDIT_RISK audit agent is expected"
    return specs[0]


def _seed_credit_risk_store(tmp_path: Path) -> LocalRegulationStore:
    """Seed a local regulation store with one hash-verified CREDIT_RISK chunk to cite."""
    chunk = RegulationChunk.from_text(
        source_id="credit-fairness-protected-attributes",
        framework=Framework.CREDIT_RISK,
        location="Consumer credit fairness, protected-attribute handling and adverse action",
        text=_PROTECTED_ATTRIBUTE_TEXT,
    )
    path = tmp_path / "regulation.jsonl"
    write_jsonl((chunk,), path)
    return LocalRegulationStore(path)


def _report(run_id: str) -> AuditReport:
    """Return a minimal deterministic report with no findings for one Run."""
    return AuditReport(run_id=run_id, status="passed", controls=(), findings=())


def _input_for_descriptive_run(log: InMemoryEventLog) -> AuditInput:
    """Record a descriptive plot-only Run and return its audit input."""
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "Generated numeric histograms and a PDF; no model or decision was produced."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    return AuditInput(
        run_id=log.run_id,
        events=tuple(log.events()),
        report=_report(log.run_id),
    )


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
        risk_profile=RiskProfile(
            risk_level="high", activity_category="credit-scoring", confidence=1.0
        ),
    )


def _protected_attribute_input(log: InMemoryEventLog) -> tuple[AuditInput, int]:
    """Record a Run that trained on a protected attribute and return the input and its seq."""
    event = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {
            "text": (
                "Trained the credit model on features including the protected attribute 'age'; "
                "no subgroup disparity breakdown was produced."
            )
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    return audit_input, event.seq


def _protected_attribute_finding(
    *, seq: int, external_ref: str, external_sha256: str
) -> dict[str, object]:
    """Return one scripted HIGH credit-risk finding grounded on the Run event and the KB chunk."""
    return {
        "findings": [
            {
                "control_id": "CREDIT-PROTECTED-ATTRIBUTE",
                "title": "Protected attribute used as a model feature",
                "finding": (
                    f"The Run trained the credit model on the protected attribute 'age' as a "
                    f"feature (event seq:{seq}), which consumer-credit fairness rules forbid."
                ),
                "severity": "HIGH",
                "confidence": 0.9,
                "evidence": [
                    {"kind": "event", "ref": f"seq:{seq}"},
                    {"kind": "external", "ref": external_ref, "sha256": external_sha256},
                ],
                "recommendation": (
                    "Hold the protected attribute out of the features and use it only to measure "
                    "subgroup disparity."
                ),
            }
        ]
    }


def _run_protected_attribute_scenario(
    tmp_path: Path,
) -> tuple[AuditAgentOutput, RegulationSearchResult, InMemoryEventLog]:
    """Run the credit-risk agent over a protected-attribute Run via the real KB tool."""
    store = _seed_credit_risk_store(tmp_path)
    chunk = store.search("protected attribute", framework=Framework.CREDIT_RISK, k=1)[0]
    external_ref = f"{chunk.source_id}/{chunk.location}"

    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _protected_attribute_input(log)
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool = SearchRegulation(store)
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={
                    "query": "protected attribute credit decision",
                    "framework": "CREDIT_RISK",
                    "k": 3,
                },
            ),
            LLMToolCall(
                id="call-output",
                name="final_result",
                arguments=_protected_attribute_finding(
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
    output = run_audit_agent(_credit_risk_spec(), audit_input, context)
    return output, chunk, log


def test_load_default_specs_ships_the_credit_risk_agent() -> None:
    spec = _credit_risk_spec()

    assert spec.name == "credit_risk"
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.tool_allowlist == ("search_regulation",)
    assert spec.required_tool_calls == ("search_regulation",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_credit_risk_prompt_forbids_asserting_authorization() -> None:
    prompt = _credit_risk_spec().system_prompt

    assert "evidence, not decisions" in prompt
    assert "Policy Engine" in prompt
    assert 'framework="CREDIT_RISK"' in prompt
    assert 'framework="GDPR"' in prompt


@pytest.mark.parametrize(
    ("query", "expected_source_id"),
    [
        ("creditworthiness credit score", "eu-ai-act-2024-annex-iii-5b-creditworthiness"),
        (
            "automated processing human intervention",
            "consumer-credit-directive-2023-art-18-automated",
        ),
        ("special categories social networks", "consumer-credit-directive-2023-art-18-data"),
    ],
)
def test_credit_risk_prompt_names_searchable_packaged_queries(
    query: str,
    expected_source_id: str,
) -> None:
    """Every suggested lookup must have a matching packaged CREDIT_RISK source."""
    prompt = _credit_risk_spec().system_prompt

    matches = load_default_regulation_store().search(
        query,
        framework=Framework.CREDIT_RISK,
        k=5,
    )

    assert matches
    assert all(match.framework is Framework.CREDIT_RISK for match in matches)
    assert expected_source_id in {match.source_id for match in matches}
    assert f'"{query}"' in prompt


def test_shipped_credit_risk_agent_completes_with_packaged_scope_evidence(
    tmp_path: Path,
) -> None:
    """The real shipped spec, store, tool and runner complete the mandatory lookup seam."""
    log = InMemoryEventLog(new_id("run"))
    agent_id = new_id("agent")
    task_id = new_id("task")
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-premature", name="final_result", arguments={"findings": []}),
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={
                    "query": "creditworthiness credit score",
                    "framework": "CREDIT_RISK",
                    "k": 5,
                },
            ),
            LLMToolCall(id="call-output", name="final_result", arguments={"findings": []}),
        ]
    )
    tool = SearchRegulation(load_default_regulation_store())
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

    output = run_audit_agent(_credit_risk_spec(), _input_for_descriptive_run(log), context)

    assert output.findings == ()
    completed = [event for event in log.events() if event.type is EventType.TOOL_COMPLETED]
    assert len(completed) == 1
    assert completed[0].payload["status"] == "COMPLETED"
    value = completed[0].payload["value"]["value"]
    assert value["result_count"] > 0
    assert {match["framework"] for match in value["matches"]} == {"CREDIT_RISK"}
    agent_completion = log.events()[-1]
    assert agent_completion.type is EventType.AGENT_COMPLETED
    assert agent_completion.payload["status"] == "COMPLETED"


def test_credit_risk_flags_a_model_trained_on_a_protected_attribute(tmp_path: Path) -> None:
    output, chunk, log = _run_protected_attribute_scenario(tmp_path)

    assert output.agent_name == "credit_risk"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.CREDIT_RISK
    assert finding.severity in {Severity.HIGH, Severity.CRITICAL}
    assert 0.0 <= finding.confidence <= 1.0

    external = [reference for reference in finding.evidence if reference.kind == "external"]
    assert len(external) == 1
    assert chunk.source_id in external[0].ref
    assert external[0].sha256 == chunk.sha256
    assert sha256_text(chunk.fragment) == chunk.sha256

    event_types = [event.type for event in log.events()]
    assert event_types.count(EventType.TOOL_STARTED) == 1
    assert event_types.count(EventType.TOOL_COMPLETED) == 1


def test_credit_risk_run_without_protected_attribute_yields_no_finding(tmp_path: Path) -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {
            "text": (
                "Held the protected attribute out of the features; reported subgroup disparity "
                "and adverse-action reasons for each declined application."
            )
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    agent_id = new_id("agent")
    task_id = new_id("task")
    tool = SearchRegulation(_seed_credit_risk_store(tmp_path))
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={
                    "query": "protected attribute credit decision",
                    "framework": "CREDIT_RISK",
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

    output = run_audit_agent(_credit_risk_spec(), audit_input, context)

    assert output.agent_name == "credit_risk"
    assert output.findings == ()
