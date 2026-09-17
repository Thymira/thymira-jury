"""ThyGraph's contract: ThyInput/ThyState/ThyOutput, AgentTask, and the phase sequence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thymira.schemas import ArtifactKind, Id, Run, new_id
from thymira.thy import (
    THY_PHASE_ORDER,
    AgentTask,
    ArtifactRequirement,
    ThyAgentKind,
    ThyInput,
    ThyOutput,
    ThyPhase,
    ThyProgress,
    ThyState,
)


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def test_thy_input_wraps_a_run() -> None:
    run = _run()
    thy_input = ThyInput(run=run)
    assert thy_input.run is run


def test_thy_state_starts_at_inspect() -> None:
    state = ThyState(run=_run())
    assert state.phase is ThyPhase.INSPECT
    assert state.plan == ()
    assert state.completed == ()
    assert state.agent_messages == ()
    assert state.usage.requests == 0
    assert state.policy_signals == ()
    assert state.error is None


def test_thy_state_advances_in_order() -> None:
    state = ThyState(run=_run())
    for expected in THY_PHASE_ORDER[1:]:
        state = state.advance()
        assert state.phase is expected


def test_thy_state_cannot_advance_past_summarize() -> None:
    state = ThyState(run=_run(), phase=ThyPhase.SUMMARIZE)
    with pytest.raises(ValueError, match="last phase"):
        state.advance()


def test_thy_state_is_frozen() -> None:
    state = ThyState(run=_run())
    with pytest.raises(ValidationError):
        setattr(state, "phase", ThyPhase.PLAN)  # noqa: B010  # frozen model must refuse


def test_agent_task_requires_a_non_empty_instruction() -> None:
    with pytest.raises(ValidationError):
        AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.INSPECT, instruction="")


def test_agent_task_requires_a_non_empty_id() -> None:
    with pytest.raises(ValidationError):
        AgentTask(id="", agent=ThyAgentKind.DATA, phase=ThyPhase.INSPECT, instruction="x")


def test_agent_task_can_declare_required_artifacts() -> None:
    task = AgentTask(
        id="t1",
        agent=ThyAgentKind.CODING,
        phase=ThyPhase.EXECUTE,
        instruction="Create the report.",
        required_artifacts=(
            ArtifactRequirement(name="reports/result.md", kind=ArtifactKind.REPORT),
        ),
    )

    assert task.required_artifacts[0].name == "reports/result.md"
    assert task.required_artifacts[0].kind is ArtifactKind.REPORT


@pytest.mark.parametrize("name", ["../result.md", "/result.md", "C:/result.md"])
def test_artifact_requirement_rejects_paths_outside_the_workspace(name: str) -> None:
    with pytest.raises(ValidationError, match="workspace-relative"):
        ArtifactRequirement(name=name)


def test_thy_output_defaults_to_no_experiments_or_artifacts() -> None:
    run_id: Id = new_id("run")
    output = ThyOutput(run_id=run_id)
    assert output.experiment_ids == ()
    assert output.artifact_ids == ()
    assert output.error is None


def test_thy_progress_requires_completed_and_agent_messages_to_pair_positionally() -> None:
    task = AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="x")
    with pytest.raises(ValidationError):
        ThyProgress(plan=(task,), completed=(task,), agent_messages=())
