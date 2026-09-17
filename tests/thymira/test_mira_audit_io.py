"""Unit tests for MIRA's audit-agent input, output, and YAML spec contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.events import InMemoryEventLog
from thymira.mira import (
    AuditAgentOutput,
    AuditAgentSpec,
    AuditInput,
    load_default_specs,
    load_specs,
)
from thymira.mira.checks import AuditReport
from thymira.schemas import (
    Actor,
    AuditFinding,
    Event,
    EventType,
    Framework,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path


def _spec(
    *, name: str = "methodology", framework: Framework = Framework.METHODOLOGY
) -> AuditAgentSpec:
    """Build one valid audit-agent declaration."""
    return AuditAgentSpec(
        name=name,
        framework=framework,
        task_kinds=("audit_judgement", "review"),
        tool_allowlist=("search_regulation",),
        tier=ModelTier.STANDARD,
        max_turns=3,
        system_prompt="Review the supplied evidence and return grounded findings.",
    )


def _finding(run_id: str, *, framework: Framework = Framework.METHODOLOGY) -> AuditFinding:
    """Build one valid audit finding for output validation."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="METHOD-001",
        framework=framework,
        title="Validation evidence is incomplete",
        finding="The run does not record validation evidence.",
        severity=Severity.HIGH,
        confidence=0.9,
    )


def _events(run_id: str) -> tuple[Event, ...]:
    """Build explicit artifact, experiment, and framework evidence for one Run."""
    log = InMemoryEventLog(run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    log.append(
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {"name": "metrics.json", "sha256": "a" * 64},
        subject_id=new_id("artifact"),
    )
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {"experiment": {"id": new_id("experiment")}},
    )
    finding = _finding(run_id)
    log.append(
        EventType.AUDIT_FINDING,
        Actor.system(),
        {"finding": finding.model_dump(mode="json")},
        subject_id=finding.id,
    )
    return tuple(log.events())


def _write_spec(path: Path, *, name: str, max_turns: int = 3) -> None:
    """Write one test YAML declaration without relying on production serialization."""
    path.write_text(
        "\n".join(
            (
                f"name: {name}",
                "framework: METHODOLOGY",
                "task_kinds: [audit_judgement, review]",
                "tool_allowlist: [search_regulation]",
                "tier: STANDARD",
                f"max_turns: {max_turns}",
                "system_prompt: Review the supplied evidence.",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )


def test_audit_agent_spec_round_trips_from_yaml(tmp_path: Path) -> None:
    _write_spec(tmp_path / "methodology.yaml", name="methodology")

    specs = load_specs(tmp_path)

    assert specs == (_spec().model_copy(update={"system_prompt": "Review the supplied evidence."}),)
    assert AuditAgentSpec.model_validate(specs[0].model_dump(mode="json")) == specs[0]


def test_load_specs_uses_deterministic_file_order(tmp_path: Path) -> None:
    _write_spec(tmp_path / "b.yaml", name="first-by-name-only")
    _write_spec(tmp_path / "a.yaml", name="second-by-name-only")

    specs = load_specs(tmp_path)

    assert tuple(spec.name for spec in specs) == ("second-by-name-only", "first-by-name-only")


def test_load_specs_rejects_duplicate_names(tmp_path: Path) -> None:
    _write_spec(tmp_path / "a.yaml", name="duplicate")
    _write_spec(tmp_path / "b.yaml", name="duplicate")

    with pytest.raises(ValueError, match="duplicate audit agent spec name 'duplicate'"):
        load_specs(tmp_path)


def test_load_specs_rejects_non_positive_max_turns(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    _write_spec(path, name="invalid", max_turns=0)

    with pytest.raises(ValueError, match=r"(?s)invalid\.yaml.*max_turns"):
        load_specs(tmp_path)


def test_audit_agent_spec_rejects_required_tool_outside_allowlist() -> None:
    with pytest.raises(ValidationError, match=r"required_tool_calls.*tool_allowlist"):
        AuditAgentSpec(
            name="invalid",
            framework=Framework.METHODOLOGY,
            task_kinds=("audit_judgement",),
            tool_allowlist=("search_regulation",),
            required_tool_calls=("search_regulation", "missing_tool"),
            max_turns=3,
            system_prompt="Review the supplied evidence.",
        )


def test_only_source_grounded_regulatory_specs_require_a_tool_call() -> None:
    required = {
        spec.name: spec.required_tool_calls
        for spec in load_default_specs()
        if spec.required_tool_calls
    }

    assert required == {
        "credit_risk": ("search_regulation",),
        "euaiact": ("search_regulation",),
    }


def test_load_specs_rejects_malformed_yaml_with_source_path(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unterminated\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=r"broken\.yaml: malformed audit agent YAML"):
        load_specs(tmp_path)


def test_audit_input_from_run_derives_only_explicit_event_evidence() -> None:
    run_id = new_id("run")
    events = _events(run_id)
    report = AuditReport(run_id=run_id, status="passed", controls=())

    audit_input = AuditInput.from_run(events, report)

    assert audit_input.run_id == run_id
    assert audit_input.events == events
    assert audit_input.report is report
    assert audit_input.artifact_names == ("metrics.json",)
    experiment = events[2].payload["experiment"]
    assert isinstance(experiment, dict)
    assert audit_input.experiment_ids == (experiment["id"],)
    assert audit_input.frameworks == (Framework.METHODOLOGY,)


def test_audit_input_rejects_events_from_different_runs() -> None:
    events = (*_events(new_id("run")), *_events(new_id("run")))

    with pytest.raises(ValueError, match="events span multiple runs"):
        AuditInput.from_run(events, None)


def test_audit_input_rejects_report_for_another_run() -> None:
    events = _events(new_id("run"))
    report = AuditReport(run_id=new_id("run"), status="passed", controls=())

    with pytest.raises(ValueError, match="does not match events run_id"):
        AuditInput.from_run(events, report)


def test_audit_input_requires_evidence_from_which_to_derive_run_id() -> None:
    with pytest.raises(ValueError, match="cannot derive audit run_id"):
        AuditInput.from_run((), None)


def test_audit_agent_output_builds_from_its_spec() -> None:
    run_id = new_id("run")
    spec = _spec()
    finding = _finding(run_id)
    choice = ModelChoice(
        role=Role.MIRA,
        task="audit_judgement",
        tier_requested=ModelTier.STANDARD,
        tier_applied=ModelTier.STANDARD,
        model="configured-model",
        reason="test routing choice",
    )

    output = AuditAgentOutput.from_spec(spec, findings=(finding,), model_choice=choice)

    assert output.agent_name == spec.name
    assert output.findings == (finding,)
    assert output.model_choice == choice
    assert "framework" not in AuditAgentOutput.model_fields


def test_audit_agent_output_scrubs_finding_free_text_and_keeps_evidence_facts() -> None:
    """Model output is safe at the validated copy boundary while facts remain usable."""
    run_id = new_id("run")
    source = _finding(run_id).model_copy(
        update={
            "title": "Contact alice@example.com",
            "finding": "The observed value was 0.8123456789012345.",
            "recommendation": "Ask alice@example.com to review it.",
        }
    )

    output = AuditAgentOutput.from_spec(_spec(), findings=(source,))

    finding = output.findings[0]
    assert "alice@example.com" not in finding.title
    assert "alice@example.com" not in finding.finding
    assert "0.8123456789012345" in finding.finding
    assert finding.confidence == source.confidence


def test_audit_agent_output_rejects_finding_for_another_framework() -> None:
    spec = _spec(framework=Framework.EU_AI_ACT)
    finding = _finding(new_id("run"), framework=Framework.METHODOLOGY)

    with pytest.raises(ValueError, match="declares framework 'EU_AI_ACT'"):
        AuditAgentOutput.from_spec(spec, findings=(finding,))


def test_audit_contracts_reject_extra_fields() -> None:
    run_id = new_id("run")
    audit_input = AuditInput.from_run((), AuditReport(run_id=run_id, status="passed", controls=()))
    output = AuditAgentOutput.from_spec(_spec(), findings=(_finding(run_id),))
    spec = _spec()

    with pytest.raises(ValidationError, match="extra"):
        AuditInput.model_validate({**audit_input.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError, match="extra"):
        AuditAgentOutput.model_validate({**output.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError, match="extra"):
        AuditAgentSpec.model_validate({**spec.model_dump(), "unexpected": True})
