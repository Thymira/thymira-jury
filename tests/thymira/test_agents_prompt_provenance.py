"""Request/prompt provenance: record what the model actually saw (THY-06)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentRunner,
    AssembledPrompt,
    PromptProvenanceContext,
    load_agent_specs,
    record_prompt,
)
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.schemas import (
    Actor,
    ArtifactKind,
    EventSurface,
    EventType,
    ModelRoutePolicy,
    Task,
    TaskStatus,
    new_id,
)
from thymira.state import LocalArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentRunner provider seam to a code-owned route and model choice."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_CHOICE = ModelChoice(
    role=Role.AGENT,
    task="analyze",
    tier_requested=ModelTier.STANDARD,
    tier_applied=ModelTier.STANDARD,
    model="test-model",
    reason="test",
)


def test_record_prompt_persists_the_exact_scrubbed_prompt_and_returns_its_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    log = InMemoryEventLog(run_id)
    ctx = PromptProvenanceContext(
        artifact_store=store, event_log=log, actor=Actor.system(), agent_id=new_id("agent")
    )
    secret = "known-provider-value"  # noqa: S105  # test fixture credential
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assembled = AssembledPrompt(
        system="You are the data agent.",
        user=f"contact alice@example.com; the API key is {secret}",
        surface_seqs=(1, 2),
    )

    artifact = record_prompt(ctx, assembled, _CHOICE)

    assert artifact.kind is ArtifactKind.LOG
    stored_text = store.load_text(artifact.name)
    assert secret not in stored_text
    assert "alice@example.com" in stored_text
    assert "[REDACTED:CREDENTIAL]" in stored_text
    assert artifact.sha256 == hashlib.sha256(stored_text.encode("utf-8")).hexdigest()


def test_record_prompt_enriches_the_model_selected_payload(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    log = InMemoryEventLog(run_id)
    ctx = PromptProvenanceContext(
        artifact_store=store, event_log=log, actor=Actor.system(), agent_id=new_id("agent")
    )
    assembled = AssembledPrompt(
        system="You are the data agent.", user="profile it", surface_seqs=(3, 5)
    )

    artifact = record_prompt(ctx, assembled, _CHOICE)

    selected = next(e for e in log.events() if e.type == EventType.MODEL_SELECTED)
    assert selected.payload["prompt_sha256"] == artifact.sha256
    assert selected.payload["surface_seqs"] == [3, 5]
    assert selected.payload["model"] == "test-model"


def _agent_spec_yaml(tmp_path: Path) -> AgentCatalog:
    (tmp_path / "data.yaml").write_text(
        """\
name: data
role: agent
task_kinds: [analyze]
max_turns: 2
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def test_one_agent_run_persists_a_prompt_artifact_and_enriches_model_selected(
    tmp_path: Path,
) -> None:
    catalog = _agent_spec_yaml(tmp_path)
    spec = catalog.get("data")
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    provider = ScriptedProvider([DataProfileOutput(row_count=10, columns=("a",))])
    ctx = AgentContext(
        catalog=catalog,
        event_log=log,
        provider=provider,
        artifact_store=store,
        route_policy=TEST_ROUTE_POLICY,
    )

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status == TaskStatus.COMPLETED
    log_artifacts = [a for a in store.list_active() if a.kind is ArtifactKind.LOG]
    assert len(log_artifacts) == 1
    selected = next(e for e in log.events() if e.type == EventType.MODEL_SELECTED)
    assert selected.payload["prompt_sha256"] == log_artifacts[0].sha256
    assert selected.payload["surface_seqs"] == []


def test_owner_request_content_is_in_the_recorded_prompt_bytes(tmp_path: Path) -> None:
    catalog = _agent_spec_yaml(tmp_path)
    spec = catalog.get("data")
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    provider = ScriptedProvider([DataProfileOutput(row_count=10, columns=("a",))])

    def before_request() -> tuple[str, ...]:
        event = log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "steer after prompt assembly"},
            surface=EventSurface.MODEL_VISIBLE,
        )
        assert event.seq > 0
        return ("steer after prompt assembly",)

    ctx = AgentContext(
        catalog=catalog,
        event_log=log,
        provider=provider,
        artifact_store=store,
        before_model_request=before_request,
        route_policy=TEST_ROUTE_POLICY,
    )
    AgentRunner().run(spec, task, ctx)

    log_artifact = next(a for a in store.list_active() if a.kind is ArtifactKind.LOG)
    stored_text = store.load_text(log_artifact.name)
    assert "steer after prompt assembly" in stored_text
    selected = next(e for e in log.events() if e.type is EventType.MODEL_SELECTED)
    assert selected.payload["prompt_sha256"] == log_artifact.sha256
    assert selected.payload["surface_seqs"] == [1]
