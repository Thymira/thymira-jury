"""Acceptance: the demo Run, replayed from a recorded session (F12).

Real: RunService and the production Core -> THY -> deterministic MIRA -> Gate graph, its risk
interview and classification seam, Delegator/AgentRunner, the builtin tools with the local
Python sandbox, a LocalArtifactStore and the authoritative event log under tmp_path, the checked-in
examples/credit-risk project, and data/german_credit.csv copied to the project's
`data/applications.csv`, where `.thymira/config.yaml` declares it, so Inspect registers it exactly
as a real Run would. Faked: only the model (ScriptedProvider).

What is pinned: (1) the exact system prompt and tool schemas sent to the model on the first
request of each agent (sidecars under tests/thymira/acceptance/); (2) the files the Run leaves
behind (workspace.expected.json), which is the independent oracle -- model prose and tool text do
not prove the external effect; (3) the log verifies. MIRA's optional LLM audit-agent fanout is
outside this acceptance boundary; its deterministic preflight and full control audit remain real.
Replay is read-only. Set
THYMIRA_SNAPSHOT=refresh after an intentional change, review the diff, and commit it.

THYMIRA_SANDBOX_BACKEND is pinned to "local" explicitly: this is a slow-lane test that must pass
on CI without Docker, and the pin documents the local development configuration rather than
relying on the production composition root's own container default. A18 verifies the
source-backed profile. After four explicit tool approvals, the local backend refuses the
experiment because it cannot enforce workspace confinement. A19 records that refusal as critical
and the final Gate blocks the Run; A29 independently recomputes that the recorded resolved
specification is present and self-consistent (an honest all-unenforced local spec) and PASSES.
The agent reports no metrics or model, and no successful training evidence is invented. The
explicit development-mode training regression in test_tools_experiment_repro.py retains the
numerical oracle independently of this default path.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.thymira.acceptance.session import (
    assert_or_update,
    load_demo_session,
    snapshot_mode,
    workspace_snapshot,
)
from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.agents import RiskClassification
from thymira.agents.llm import LLMProvider, ScriptedProvider
from thymira.api import RuntimeDeps, build_default_deps
from thymira.core import GraphFactory, build_runtime_graph_factory
from thymira.events import verify_events
from thymira.schemas import Actor, ActorKind, Artifact, EventType, RiskLevel, Run
from thymira.thy.agents import full_agent_catalog
from thymira.tools import ToolManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langgraph.graph.state import CompiledStateGraph

    from thymira.policies import ApprovalRequest
    from thymira.schemas import Event


pytestmark = pytest.mark.slow

ACCEPTANCE_DIR = Path(__file__).resolve().parent / "acceptance"
REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_MODE = snapshot_mode()

_REVIEWER = Actor(kind=ActorKind.HUMAN, id="acceptance-reviewer", authenticated=True)
_INTERVIEW_ANSWERS = {
    "purpose": "Retrospective academic credit-risk model benchmarking and comparison.",
    "affected_population": "Credit applicants represented in the German credit benchmark.",
    "decision_effect": (
        "Retrospective academic decision support; no actual credit decision is made."
    ),
    "autonomy": "The system trains and evaluates a model but cannot approve or deny credit.",
    "human_oversight": "An analyst reviews the evidence and can change or stop the activity.",
    "jurisdiction": "European Union.",
    "data_categories": (
        "Credit application, financial, demographic, age, employment, housing, and account data."
    ),
    "sensitive_attributes": (
        "Age, personal status and sex, and foreign-worker status are present in the shipped data."
    ),
    "potential_consequences": (
        "An incorrect assessment could support an unfair denial of credit if reused operationally."
    ),
}


def _production_deps(tmp_path: Path, workspace: Path, provider: LLMProvider) -> RuntimeDeps:
    """Build one production graph factory from the API composition root's shared handles."""
    assembled: list[RuntimeDeps] = []
    factories: list[GraphFactory] = []

    def graph_factory(run: Run) -> CompiledStateGraph:
        deps = assembled[0]
        assert deps.project_resolution is not None
        if not factories:
            factories.append(
                build_runtime_graph_factory(
                    deps.run_store,
                    gate_factory=deps.gate_factory,
                    artifact_store_factory=deps.artifact_store_factory,
                    tool_manager=ToolManager(deps.tool_registry),
                    tool_registry=deps.tool_registry,
                    mira_tool_registry=deps.mira_tool_registry,
                    record_repository=deps.record_repository,
                    checkpoint_repository=deps.checkpoint_repository,
                    provider=provider,
                    project_dir=workspace,
                    project_config=deps.project_resolution.config,
                    catalog=full_agent_catalog(),
                    specs=(),
                    risk_interview=deps.risk_interview,
                    plan_repository=deps.board_repository,
                    dag_repository=deps.dag_repository,
                )
            )
        return factories[0](run)

    assembled.append(
        build_default_deps(
            tmp_path / "runtime",
            workspace=workspace,
            graph_factory=graph_factory,
            provider=provider,
            principal_resolver=TEST_CREDENTIAL,
        )
    )
    return assembled[0]


def _approve_pending_review(deps: RuntimeDeps, run_id: str) -> None:
    """Record an authenticated, nonautomatic human approval and resume the parked Run."""

    def approve(_request: ApprovalRequest) -> bool:
        return True

    deps.run_service.resolve_approval(
        run_id,
        gate=deps.gate_factory(run_id, approver=approve, human=_REVIEWER),
        actor=_REVIEWER,
        note="Acceptance reviewer inspected the recorded evidence.",
    )


def _wait_reason(deps: RuntimeDeps, run_id: str) -> str | None:
    """Return the authoritative controller wait reason from the latest transition."""
    transition = next(
        event
        for event in reversed(deps.run_store.events(run_id))
        if event.type is EventType.RUN_TRANSITIONED
    )
    if transition.payload["condition"] != "waiting":
        return None
    return str(transition.payload["wait_reason"])


def _assert_composed_evidence(events: Sequence[Event]) -> None:
    """Verify the intake, human-review, and deterministic MIRA evidence of the full Run."""
    questions = [e for e in events if e.type is EventType.ACTIVITY_PROFILE_QUESTIONED]
    answers = [e for e in events if e.type is EventType.ACTIVITY_PROFILE_ANSWERED]
    assert [e.payload["field"] for e in questions] == list(_INTERVIEW_ANSWERS)
    assert [e.payload["field"] for e in answers] == list(_INTERVIEW_ANSWERS)
    assert len([e for e in events if e.type is EventType.RISK_ASSESSMENT_RECORDED]) == 1
    classified = next(e for e in events if e.type is EventType.RISK_CLASSIFIED)
    assert classified.payload["activity_profile_version"] == 10
    assert classified.payload["risk_profile"]["needs_human_review"] is True
    first_tool = next(e for e in events if e.type is EventType.TOOL_STARTED)
    assert classified.seq < first_tool.seq
    approvals = [e for e in events if e.type is EventType.HUMAN_APPROVAL]
    assert len(approvals) == 3
    assert all(e.actor == _REVIEWER and e.payload["automatic"] is False for e in approvals)
    audit = next(e for e in reversed(events) if e.type is EventType.AUDIT_COMPLETED)
    controls = {
        control["control_id"]: control for control in audit.payload["audit_report"]["controls"]
    }
    assert audit.payload["audit_report"]["status"] == "failed"
    assert controls["A3"]["status"] == "PASSED"
    assert controls["A6"]["status"] == "PASSED"
    assert controls["A15"]["status"] == "PASSED"
    assert controls["A18"]["status"] == "PASSED"
    assert controls["A23"]["status"] == "NOT_APPLICABLE"
    assert controls["A29"]["status"] == "PASSED"
    assert {item["ref"] for item in controls["A18"]["evidence"] if item["kind"] == "artifact"} == {
        "profile/german_credit.json",
        "datasets/german_credit.schema.json",
        "datasets/german_credit.csv",
    }
    assert {
        control_id for control_id, control in controls.items() if control["status"] == "FAILED"
    } == {"A19"}
    final_decision = next(e for e in reversed(events) if e.type is EventType.POLICY_DECISION)
    assert final_decision.payload["subject_kind"] == "findings"
    assert final_decision.payload["decision"] == "BLOCK"
    assert not any(e.type is EventType.REWORK_STARTED for e in events)
    assert not any(e.type is EventType.RUN_COMPLETED for e in events)
    assert not any(e.type is EventType.MODEL_TRAINED for e in events)
    execution = next(
        event
        for event in events
        if event.type is EventType.TOOL_COMPLETED and event.payload["tool"] == "run_experiment"
    )
    assert execution.payload["sandbox_mode"] == "workspace_write"
    assert execution.payload["sandbox_enforcement"] == "unusable"
    assert execution.payload["exit_code"] == 125
    spec = execution.payload["sandbox_spec"]
    assert spec is not None
    assert spec["backend"] == "local_subprocess"
    assert "workspace_quota" in spec["unenforced"]


def _normalize(text: str, workspace: Path, models: Sequence[str]) -> str:
    """Replace the workspace path, prefixed ids and every recorded model id with stable tokens."""
    text = text.replace(str(workspace), "{{workspace}}").replace(
        workspace.as_posix(), "{{workspace}}"
    )
    text = re.sub(
        r"\b(run|agent|artifact|tool|task|session|project)_[0-9a-f]{32}\b", "{{id}}", text
    )
    text = re.sub(
        r"(\{\{workspace\}\})[\\/]+\.thymira[\\/]+runtime[\\/]+workspaces[\\/]+(\{\{id\}\})",
        r"\1/.thymira/runtime/workspaces/\2",
        text,
    )
    for model in models:
        text = text.replace(model, "{{model}}")
    return text


def test_snapshot_normalization_changes_separators_only_in_workspace_rendering(
    tmp_path: Path,
) -> None:
    """Windows path separators are portable in the workspace token, while payload text is intact."""
    workspace = tmp_path / "workspace"
    separator = "\\"
    rendered = (
        f"{workspace}{separator}.thymira{separator}runtime{separator}workspaces{separator}"
        "run_0123456789abcdef0123456789abcdef; payload C:\\other\\path"
    )

    normalized = _normalize(rendered, workspace, [])

    assert "{{workspace}}/.thymira/runtime/workspaces/{{id}}" in normalized
    assert "C:\\other\\path" in normalized


def _ignore_generated_project_files(directory: str, names: list[str]) -> set[str]:
    """Keep the acceptance input independent from local run outputs in the example project."""
    generated = {
        ".mlflow",
        "__pycache__",
        "german_credit.csv",
        "metrics.json",
        "model.joblib",
        "preprocess.py",
        "prepare_data.py",
        "test_preprocessed.csv",
        "train_preprocessed.csv",
    }
    ignored = {name for name in names if name in generated or name.startswith("tool_")}
    if Path(directory).name == ".thymira":
        # `runtime/` holds real run history from manually exercising this same example project
        # (events.jsonl, artifacts/<run_id>, ...) -- gitignored (`**/.thymira/runtime/`) and never
        # meant to seed a fresh acceptance run. This previously read `ignored.add(".thymira")`,
        # which added the current directory's own name to a set of its *children* -- a no-op, so
        # nothing under `.thymira/` was ever actually excluded. Caught refreshing the H2 workspace
        # snapshot: `examples/credit-risk/.thymira/runtime/` had accumulated real Runs from manual
        # testing, and every one of them leaked into what should be a clean, reproducible fixture.
        ignored.add("runtime")
    return ignored


def _artifact_oracle_entry(
    artifact: Artifact, workspace: Path, models: Sequence[str]
) -> dict[str, object]:
    """Return a stable oracle entry while retaining store-level digest verification."""
    kind = artifact.kind.value
    digest = artifact.sha256 if kind == "dataset" else "<verified-at-runtime>"
    size: int | str = artifact.size_bytes if kind == "dataset" else "<verified-at-runtime>"
    return {
        "kind": kind,
        "name": _normalize(artifact.name, workspace, models),
        "sha256": digest,
        "size_bytes": size,
    }


def test_demo_run_records_what_the_model_saw_and_what_it_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the backend explicitly rather than relying on the production default: this is a
    # slow-lane test that must pass on CI without Docker, and it documents the local development
    # configuration -- an honest all-unenforced spec that still feeds A19's critical finding.
    # Every sibling THYMIRA_SANDBOX_* name is cleared too (tests/conftest.py already clears the
    # ambient value at session start; this repeats it so the test stays correct read on its own):
    # an inherited THYMIRA_SANDBOX_MODE, for example, would force every subprocess tool into
    # danger_full_access regardless of the backend pinned below, changing the approval sequence
    # this test asserts.
    for sibling in (
        "THYMIRA_SANDBOX_IMAGE",
        "THYMIRA_SANDBOX_MEMORY",
        "THYMIRA_SANDBOX_CPUS",
        "THYMIRA_SANDBOX_PIDS_LIMIT",
        "THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES",
        "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES",
        "THYMIRA_SANDBOX_MODE",
    ):
        monkeypatch.delenv(sibling, raising=False)
    monkeypatch.setenv("THYMIRA_SANDBOX_BACKEND", "local")
    # The production composition root snapshots the operator's model-route allowlist from
    # THYMIRA_ALLOWED_MODEL_ROUTES and every tier from THYMIRA_MODEL_*; an unset allowlist is an
    # empty fail-closed snapshot that denies even the scripted provider's route, so this test
    # binds the same explicit test route the fast-lane provider seams bind.
    for tier in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(tier, "test-model")
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "test-model")
    workspace = tmp_path / "workspace"
    shutil.copytree(
        REPO / "examples" / "credit-risk", workspace, ignore=_ignore_generated_project_files
    )
    (workspace / "data").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "data" / "german_credit.csv", workspace / "data" / "applications.csv")
    workspace_inputs = {
        path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file()
    }
    classification = RiskClassification(
        risk_level=RiskLevel.MEDIUM,
        activity_category="credit_risk_model_development",
        risk_factors=("personal_data", "credit_decision_support", "sensitive_attributes"),
        missing_information=(),
        confidence=0.9,
        needs_human_review=True,
        summary="A retrospective credit-risk benchmark still models outcomes about people.",
        criteria=("human retains authority", "no actual credit decision is made"),
    )
    interview_fields = list(_INTERVIEW_ANSWERS)
    interview_responses: list[str | dict[str, Any]] = [{"facts": []}]
    interview_responses.append("Please describe the purpose of this activity.")
    for index, field in enumerate(interview_fields):
        interview_responses.append({"sufficient": True, "reason": f"The answer states {field}."})
        if index < len(interview_fields) - 1:
            next_field = interview_fields[index + 1]
            interview_responses.append(f"Please describe {next_field}.")
    # Option (b): this acceptance oracle is about THY's model input and workspace effects;
    # valid and uncertain MIRA risk judgements are covered by test_mira_risk_model.py. The
    # invalid response preserves MIRA's deterministic fallback while keeping Core's classification
    # in its original scripted position.
    provider = ScriptedProvider([*interview_responses, {}, classification, *load_demo_session()])
    deps = _production_deps(tmp_path, workspace, provider)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="acceptance"
    )
    run = deps.run_service.create_run(
        session.id,
        "Assess credit-risk applicants and recommend a model.",
        actor=Actor.system(),
        workspace=workspace,
    )
    for expected_field, answer in _INTERVIEW_ANSWERS.items():
        assert _wait_reason(deps, run.id) == "information"
        question = next(
            event
            for event in reversed(deps.run_store.events(run.id))
            if event.type is EventType.ACTIVITY_PROFILE_QUESTIONED
        )
        assert question.payload["field"] == expected_field
        deps.run_service.answer_risk_interview(run.id, answer=answer, actor=_REVIEWER)
    for _ in range(3):
        assert _wait_reason(deps, run.id) == "approval"
        _approve_pending_review(deps, run.id)
    assert _wait_reason(deps, run.id) is None

    blocked_run = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)
    store = deps.artifact_store_factory(run.id)
    assert blocked_run.status.value == "BLOCKED"
    assert store.verify() == []
    assert verify_events(events).valid

    _assert_composed_evidence(events)

    assert "datasets/german_credit.schema.json" in {a.name for a in store.list_active()}
    final_message = next(
        event
        for event in reversed(events)
        if event.type is EventType.AGENT_MESSAGE and event.payload.get("agent") == "experiment"
    )
    reported = json.loads(str(final_message.payload["summary"]))
    assert reported["metrics"] == {}
    assert reported["model_artifact_id"] is None
    assert not any(artifact.kind.value in {"metrics", "model"} for artifact in store.list_active())

    models = sorted(
        {str(e.payload["model"]) for e in events if e.type is EventType.MODEL_SELECTED},
        key=len,
        reverse=True,
    )
    for agent_name in ("data", "experiment"):
        call = next(
            c for c in provider.calls if c["system"].startswith(f"You are the {agent_name} agent")
        )
        assert_or_update(
            ACCEPTANCE_DIR / f"{agent_name}.system-prompt.expected.md",
            _normalize(call["system"], workspace, models) + "\n",
            mode=SNAPSHOT_MODE,
        )
        assert_or_update(
            ACCEPTANCE_DIR / f"{agent_name}.tool-schemas.expected.json",
            json.dumps(call["tools"], indent=1, sort_keys=True) + "\n",
            mode=SNAPSHOT_MODE,
        )
    oracle = json.dumps(
        {
            "artifacts": [
                _artifact_oracle_entry(artifact, workspace, models)
                for artifact in sorted(store.list_active(), key=lambda item: item.name)
                if artifact.kind.value != "log"
            ],
            "workspace": workspace_snapshot(workspace, paths=workspace_inputs),
        },
        indent=2,
        sort_keys=True,
    )
    assert_or_update(ACCEPTANCE_DIR / "workspace.expected.json", oracle + "\n", mode=SNAPSHOT_MODE)
