"""Contract v0.1: shape, immutability, serialisation and the rules the models enforce."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thymira.schemas import (
    CONTRACT_VERSION,
    GENESIS_HASH,
    Actor,
    ActorKind,
    Agent,
    Approval,
    Artifact,
    ArtifactKind,
    AuditFinding,
    DatasetConfig,
    Decision,
    Event,
    EventType,
    Evidence,
    Experiment,
    Framework,
    Layer,
    PolicyDecision,
    ProjectConfig,
    Run,
    RunStatus,
    SandboxEnforcement,
    SandboxMode,
    Session,
    Severity,
    Task,
    ToolCall,
    can_transition,
    id_kind,
    new_id,
)

SHA = "a" * 64


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def test_contract_version_is_declared() -> None:
    assert CONTRACT_VERSION == "0.9"


def test_ids_are_prefixed_random_and_typed() -> None:
    run_id = new_id("run")
    assert run_id.startswith("run_")
    assert id_kind(run_id) == "run"
    assert new_id("run") != run_id
    with pytest.raises(ValueError, match="malformed"):
        id_kind("not-an-id")
    with pytest.raises(ValidationError):
        Run(id="run-1", project_id=new_id("project"), session_id=new_id("session"), prompt="x")


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    run = _run()
    with pytest.raises(ValidationError):
        setattr(run, "status", RunStatus.RUNNING)  # noqa: B010  # frozen model must refuse
    with pytest.raises(ValidationError):
        Session.model_validate(
            {"id": new_id("session"), "project_id": new_id("project"), "client": "cli", "x": 1}
        )


def test_run_transitions_follow_the_lifecycle() -> None:
    run = _run()
    assert can_transition(RunStatus.CREATED, RunStatus.PLANNING)
    assert not can_transition(RunStatus.CREATED, RunStatus.COMPLETED)
    planning = run.with_status(RunStatus.PLANNING)
    assert planning.started_at is not None
    running = planning.with_status(RunStatus.RUNNING)
    auditing = running.with_status(RunStatus.AUDITING)
    done = auditing.with_status(RunStatus.COMPLETED)
    assert done.completed_at is not None
    with pytest.raises(ValueError, match="not allowed"):
        done.with_status(RunStatus.RUNNING)


def test_event_hashable_dict_excludes_hash_and_round_trips() -> None:
    event = Event(
        event_id=new_id("event"),
        run_id=new_id("run"),
        seq=0,
        type=EventType.RUN_STARTED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.events",
        producer_version="0.3",
        payload={"prompt": "x"},
    )
    assert event.prev_hash == GENESIS_HASH
    body = event.hashable_dict()
    assert "hash" not in body
    assert body["type"] == "run.started"
    assert body["actor"]["kind"] == "system"
    restored = Event.model_validate_json(event.model_dump_json())
    assert restored == event


def test_decision_precedence_and_separate_human_approval() -> None:
    assert (
        Decision.BLOCK.precedence
        > Decision.REQUIRE_HUMAN_REVIEW.precedence
        > Decision.WARNING.precedence
    )
    decision = PolicyDecision(
        id=new_id("decision"),
        run_id=new_id("run"),
        subject_kind="run",
        subject_id="run",
        decision=Decision.REQUIRE_HUMAN_REVIEW,
        rule_id="high-severity-missing-evidence",
        reason="HIGH severity finding without evidence",
        policy_name="credit-risk@1.0",
        policy_sha256=SHA,
    )
    assert decision.requires_human_approval
    human = Actor(kind=ActorKind.HUMAN, id="alice", role="risk-officer")
    approval = Approval(
        id=new_id("approval"),
        run_id=decision.run_id,
        policy_decision_id=decision.id,
        authorization_context_sha256=SHA,
        approved=True,
        approved_by=human,
    )
    assert approval.policy_decision_id == decision.id
    assert "approved" not in PolicyDecision.model_fields


def test_finding_artifact_experiment_toolcall_shapes() -> None:
    run_id = new_id("run")
    agent = Agent(id=new_id("agent"), run_id=run_id, name="methodology-agent", layer=Layer.AUDIT)
    task = Task(id=new_id("task"), run_id=run_id, agent_id=agent.id, objective="audit the split")
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        agent_id=agent.id,
        control_id="A20",
        framework=Framework.METHODOLOGY,
        title="Test rows used for preprocessing",
        finding="The scaler was fitted on the full dataset.",
        severity=Severity.HIGH,
        confidence=0.92,
        evidence=(Evidence(kind="artifact", ref=new_id("artifact"), sha256=SHA),),
    )
    artifact = Artifact(
        id=new_id("artifact"),
        run_id=run_id,
        name="metrics.json",
        kind=ArtifactKind.METRICS,
        uri="artifacts/metrics.json",
        sha256=SHA,
        size_bytes=12,
        produced_by=agent.id,
    )
    experiment = Experiment(
        id=new_id("experiment"), run_id=run_id, name="logreg", metrics={"auc": 0.81}
    )
    call = ToolCall(
        id=new_id("tool"), run_id=run_id, agent_id=agent.id, task_id=task.id, tool_name="run_python"
    )
    for record in (agent, task, finding, artifact, experiment, call):
        assert type(record).model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        AuditFinding.model_validate({**finding.to_json_dict(), "confidence": 1.5})
    with pytest.raises(ValidationError):
        Artifact.model_validate({**artifact.to_json_dict(), "sha256": "xyz"})


def test_toolcall_sandbox_fields_round_trip_and_reject_unknown_values() -> None:
    call = ToolCall(
        id=new_id("tool"),
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        tool_name="run_python",
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,
        sandbox_enforcement=SandboxEnforcement.PARTIAL,
    )

    restored = ToolCall.model_validate(call.to_json_dict())

    assert restored == call
    assert restored.sandbox_mode is SandboxMode.WORKSPACE_WRITE
    assert restored.sandbox_enforcement is SandboxEnforcement.PARTIAL
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call.to_json_dict(), "sandbox_enforcement": "unknown"})
    omitted = ToolCall(
        id=new_id("tool"),
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        tool_name="read_file",
    )
    assert omitted.sandbox_mode is None
    assert omitted.sandbox_enforcement is None


def test_project_config_matches_the_example_file() -> None:
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "credit-risk", "domain": "credit_risk"},
            "experiments": {"tracking": "mlflow"},
            "agents": {"default": "thy"},
            "governance": {"audit_required": True, "frameworks": ["EU_AI_ACT", "CREDIT_RISK"]},
        }
    )
    assert config.governance.frameworks == (Framework.EU_AI_ACT, Framework.CREDIT_RISK)
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({"project": {"name": "Credit Risk", "domain": "x"}})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_payload_value_is_refused(bad: float) -> None:
    # JSON records all three as null, so the hash could not tell them apart from each other or
    # from None: a diverged model's loss=inf would be recorded as nothing.
    with pytest.raises(ValidationError, match="which JSON records as null"):
        Event(
            event_id=new_id("event"),
            run_id=new_id("run"),
            seq=0,
            type=EventType.EXPERIMENT_COMPLETED,
            schema_version="0.3",
            actor=Actor.system(),
            producer="thymira.events",
            producer_version="0.3",
            payload={"metrics": {"loss": bad}},
        )


def test_a_non_finite_value_is_refused_wherever_it_is_nested() -> None:
    with pytest.raises(ValidationError, match=r"payload\.scores\[1\]"):
        Event(
            event_id=new_id("event"),
            run_id=new_id("run"),
            seq=0,
            type=EventType.EXPERIMENT_COMPLETED,
            schema_version="0.3",
            actor=Actor.system(),
            producer="thymira.events",
            producer_version="0.3",
            payload={"scores": [0.5, float("nan")]},
        )


def test_ordinary_floats_still_round_trip() -> None:
    event = Event(
        event_id=new_id("event"),
        run_id=new_id("run"),
        seq=0,
        type=EventType.EXPERIMENT_COMPLETED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.events",
        producer_version="0.3",
        payload={"auc": 0.87, "loss": 0.0, "n": -1.5e10},
    )
    assert event.to_json_dict()["payload"] == {"auc": 0.87, "loss": 0.0, "n": -1.5e10}


def test_project_config_declares_datasets_with_a_contained_relative_path() -> None:
    config = ProjectConfig.model_validate(
        {
            "project": {"name": "demo", "domain": "credit_risk"},
            "datasets": [
                {"name": "applications", "path": "data/applications.csv", "target": "is_high_risk"}
            ],
        }
    )

    assert config.datasets[0].name == "applications"
    assert config.datasets[0].path == "data/applications.csv"
    assert config.datasets[0].target == "is_high_risk"


def test_project_config_declares_no_datasets_by_default() -> None:
    config = ProjectConfig.model_validate({"project": {"name": "demo", "domain": "credit_risk"}})

    assert config.datasets == ()


@pytest.mark.parametrize(
    "path",
    # not a real temp-file path; exercises the dataset path containment check
    [
        "/tmp/x.csv",  # noqa: S108
        "C:/data/x.csv",
        "D:data/x.csv",
        "../x.csv",
        "data/../../x.csv",
        "data\\..\\x.csv",
    ],
)
def test_a_dataset_path_that_leaves_the_project_is_refused(path: str) -> None:
    with pytest.raises(ValidationError, match="relative to the project directory"):
        DatasetConfig(name="x", path=path)


def test_a_dataset_name_is_a_stable_identifier() -> None:
    with pytest.raises(ValidationError):
        DatasetConfig(name="German Credit", path="data/x.csv")


def test_a_project_config_rejects_two_datasets_with_the_same_name() -> None:
    with pytest.raises(ValidationError, match="duplicate dataset name"):
        ProjectConfig.model_validate(
            {
                "project": {"name": "demo", "domain": "credit_risk"},
                "datasets": [
                    {"name": "applications", "path": "data/a.csv"},
                    {"name": "applications", "path": "data/b.csv"},
                ],
            }
        )
