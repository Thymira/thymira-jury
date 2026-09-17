"""Adversarial review probe for F3.2's independent completeness claim."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.events import InMemoryEventLog, canonical_json, sha256_bytes, sha256_text
from thymira.mira.checks import (
    AuditContext,
    AuditMode,
    ControlStatus,
    audit_run,
    discovery_evidence,
)
from thymira.policies import Gate, PolicyEngine, RiskProfile, ToolCapability, auto_approve
from thymira.schemas import Actor, ArtifactKind, EventType, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    DiscoveryValue,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolManager,
    ToolRegistry,
    ToolResult,
    discovery_witness,
)
from thymira.tools.builtins import search as search_builtins
from thymira.tools.builtins.search import Grep

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class _CoherentlyIncompleteGlob:
    """Persist only one real match while the workspace deliberately has two."""

    name: str = "glob"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="glob", data_access=("workspace",))
    )
    description: str = "Return a deliberately incomplete glob evidence list."
    arguments_model: type[Any] | None = None
    result_model: type[BaseModel] = DiscoveryValue

    def execute(self, invocation: Any, _arguments: dict[str, Any]) -> ToolResult:
        rendered = "a.txt"
        artifact = invocation.artifact_store.save_json(
            "discovery/glob/incomplete.json",
            {
                "schema": "thymira.discovery/1",
                "tool": "glob",
                "query": {"pattern": "*.txt", "path": "."},
                "order": "path",
                "total": 1,
                "matches": ["a.txt"],
                "skipped": [],
                "rendered": {
                    "count": 1,
                    "truncated": False,
                    "sha256": sha256_text(rendered),
                    "locations": ["a.txt"],
                },
            },
            produced_by=invocation.agent_id,
            kind=ArtifactKind.LOG,
        )
        return ToolResult(
            success=True,
            stdout=rendered,
            artifact_ids=(artifact.id,),
            result_sha256=sha256_text(rendered),
            value=DiscoveryValue(
                text=rendered,
                matches=("a.txt",),
                total_count=1,
                returned_count=1,
                omitted_count=0,
                complete_list_artifact_id=artifact.id,
            ),
        )


@dataclass(frozen=True, slots=True)
class _WorkspaceMutatingGlob:
    """A producer that changes the source between the manager's two observations."""

    name: str = "glob"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="glob", data_access=("workspace",))
    )
    description: str = "Mutate the workspace during discovery."
    arguments_model: type[Any] | None = None
    result_model: type[BaseModel] = DiscoveryValue

    def execute(self, invocation: Any, _arguments: dict[str, Any]) -> ToolResult:
        (invocation.workspace / "late.txt").write_text("late", encoding="utf-8")
        return ToolResult(
            success=True,
            stdout="",
            value=DiscoveryValue(
                text="", matches=(), total_count=0, returned_count=0, omitted_count=0
            ),
        )


def _manager_context(
    tmp_path: Path, run_id: str, artifacts_root: Path
) -> tuple[ToolContext, InMemoryEventLog]:
    """Build the real manager context used by bounded pre-dispatch probes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(artifacts_root, run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    return context, log


def test_a33_refuses_a_hash_valid_but_incomplete_discovery_list(tmp_path: Path) -> None:
    """A33 compares the producer's list with the manager's independent source witness."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", _CoherentlyIncompleteGlob()),))).execute(
        context, "glob", {"pattern": "*.txt"}
    )
    assert execution.result.success

    report = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in report.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.FAILED
    assert "incomplete" in a33.detail


def test_manager_refuses_discovery_when_the_source_changes_during_execution(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    manager = ToolManager(ToolRegistry((cast("Tool", _WorkspaceMutatingGlob()),)))

    execution = manager.execute(context, "glob", {"pattern": "*.txt"})

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_SOURCE_DRIFT")


def test_manager_refuses_discovery_when_enumeration_reports_an_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A dropped ``os.walk`` subtree cannot become a successful complete result."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    def broken_walk(_directory: Path, *, topdown: bool = True, onerror: Any = None) -> Any:
        del topdown
        assert onerror is not None
        onerror(PermissionError("permission denied"))
        yield

    monkeypatch.setattr(discovery_witness.os, "walk", broken_walk)
    execution = ToolManager(ToolRegistry((cast("Tool", _CoherentlyIncompleteGlob()),))).execute(
        context, "glob", {"pattern": "*.txt"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_WITNESS: PermissionError")
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())


def test_a33_recomputes_a_hash_valid_source_projection_from_the_query(
    tmp_path: Path,
) -> None:
    """A forged witness projection fails even when its source facts and hashes are valid."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = ToolManager(ToolRegistry((cast("Tool", _CoherentlyIncompleteGlob()),))).execute(
        context, "glob", {"pattern": "*.txt"}
    )
    assert execution.result.success

    completed = next(event for event in log.events() if event.type is EventType.TOOL_COMPLETED)
    witness_id = completed.payload["discovery_witness_artifact_id"]
    witness_artifact = next(
        artifact for artifact in context.artifact_store.list_active() if artifact.id == witness_id
    )
    witness = context.artifact_store.load_json(witness_artifact.name)
    witness["matches"] = ["a.txt"]  # valid order and hash; omits the real b.txt source fact
    forged_bytes = canonical_json(witness).encode("utf-8")
    # The manifest name is a logical key; immutable content is addressed by the public URI.
    (tmp_path / "artifacts" / witness_artifact.uri).write_bytes(forged_bytes)
    log.append(
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {
            "name": witness_artifact.name,
            "sha256": sha256_bytes(forged_bytes),
            "artifact_id": witness_artifact.id,
            "produced_by": witness_artifact.produced_by,
        },
        subject_id=witness_artifact.id,
    )

    result = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in result.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.FAILED
    assert "recompute" in a33.detail


def test_a33_recomputes_grep_from_the_retained_content_snapshot(tmp_path: Path) -> None:
    """A successful grep witness retains content so MIRA can recompute every matching line."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle\nother\n", encoding="utf-8")
    (workspace / "b.txt").write_text("other\n", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "needle"}
    )
    assert execution.result.success

    result = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in result.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.PASSED, a33.detail
    completed = next(event for event in log.events() if event.type is EventType.TOOL_COMPLETED)
    witness_id = completed.payload["discovery_witness_artifact_id"]
    witness_artifact = next(
        artifact for artifact in context.artifact_store.list_active() if artifact.id == witness_id
    )
    witness = context.artifact_store.load_json(witness_artifact.name)
    assert witness["matches"] == [{"path": "a.txt", "line": 1}]
    assert (
        witness["source"]["files"][0]["content"].encode("utf-8")
        == (workspace / "a.txt").read_bytes()
    )


def test_a33_refuses_a_grep_witness_without_content_facts(tmp_path: Path) -> None:
    """A path-and-digest-only grep witness cannot substantiate omitted matching lines."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "needle"}
    )
    assert execution.result.success
    completed = next(event for event in log.events() if event.type is EventType.TOOL_COMPLETED)
    witness_id = completed.payload["discovery_witness_artifact_id"]
    witness_artifact = next(
        artifact for artifact in context.artifact_store.list_active() if artifact.id == witness_id
    )
    witness = context.artifact_store.load_json(witness_artifact.name)
    witness["source"]["files"][0].pop("content")
    forged_bytes = canonical_json(witness).encode("utf-8")
    # The manifest name is a logical key; immutable content is addressed by the public URI.
    (tmp_path / "artifacts" / witness_artifact.uri).write_bytes(forged_bytes)
    log.append(
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {
            "name": witness_artifact.name,
            "sha256": sha256_bytes(forged_bytes),
            "artifact_id": witness_artifact.id,
            "produced_by": witness_artifact.produced_by,
        },
        subject_id=witness_artifact.id,
    )

    result = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in result.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.FAILED
    assert "content" in a33.detail


def test_manager_hashes_an_oversized_grep_input_without_read_bytes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A too-large exclusion is hashed with bounded memory and retains no file content."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    oversized = workspace / "oversized.txt"
    with oversized.open("wb") as handle:
        handle.truncate(discovery_witness._MAX_GREP_FILE_BYTES + 1)

    real_read_bytes = Path.read_bytes

    def forbidden_read_bytes(path: Path) -> bytes:
        if path.resolve().is_relative_to(workspace.resolve()):
            raise AssertionError("discovery witnesses must not use Path.read_bytes")
        # The store's immutable-object read-back is outside the discovery source boundary.
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "needle"}
    )

    assert execution.result.success
    assert any(event.type is EventType.TOOL_STARTED for event in log.events())
    monkeypatch.undo()
    witness_id = next(
        event.payload["discovery_witness_artifact_id"]
        for event in log.events()
        if event.type is EventType.TOOL_COMPLETED
    )
    witness_artifact = next(
        artifact for artifact in context.artifact_store.list_active() if artifact.id == witness_id
    )
    witness = context.artifact_store.load_json(witness_artifact.name)
    fact = witness["source"]["files"][0]
    assert fact["status"] == "too_large"
    assert fact["size_bytes"] == discovery_witness._MAX_GREP_FILE_BYTES + 1
    assert "content" not in fact


def test_manager_refuses_a_witness_before_tool_started_when_file_budget_is_exceeded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A source file-count budget cannot degrade into an incomplete successful discovery."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setattr(discovery_witness, "MAX_DISCOVERY_SOURCE_FILES", 1)
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "a"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_WITNESS:")
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())


def test_manager_refuses_a_file_that_grows_past_the_remaining_scan_budget(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A stat/open race cannot make an aggregate-over-budget witness look complete."""
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _manager_context(tmp_path, run_id, artifacts_root)
    workspace = context.workspace
    (workspace / "a.txt").write_bytes(b"a" * 50)
    growing = workspace / "b.txt"
    growing.write_bytes(b"b")
    monkeypatch.setattr(discovery_witness, "MAX_DISCOVERY_SCAN_BYTES", 100)
    original_stream = discovery_witness._stream_file
    grew = False

    def grow_after_stat(path: Path, **kwargs: Any) -> Any:
        nonlocal grew
        if path == growing and not grew:
            growing.write_bytes(b"b" * 60)
            grew = True
        return original_stream(path, **kwargs)

    monkeypatch.setattr(discovery_witness, "_stream_file", grow_after_stat)
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "b"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_WITNESS:")
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())


def test_manager_refuses_a_discovery_witness_after_its_monotonic_deadline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A delayed traversal cannot hold pre-dispatch discovery open indefinitely."""
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _manager_context(tmp_path, run_id, artifacts_root)
    (context.workspace / "a.txt").write_text("a", encoding="utf-8")
    monkeypatch.setattr(discovery_witness, "MAX_DISCOVERY_SECONDS", 0.01)
    original_walk = discovery_witness.os.walk

    def delayed_walk(*args: Any, **kwargs: Any) -> Any:
        import time

        time.sleep(0.03)
        yield from original_walk(*args, **kwargs)

    monkeypatch.setattr(discovery_witness.os, "walk", delayed_walk)
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "a"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_WITNESS:")
    assert "time budget" in execution.result.error
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())


def test_witness_serialization_refuses_incrementally_without_materializing_payload(
    monkeypatch: Any,
) -> None:
    """The serialized-size guard rejects an oversized stream before a full payload allocation."""
    witness = discovery_witness.DiscoverySourceWitness(
        tool="list_files",
        query={"path": "."},
        query_sha256="a" * 64,
        source_sha256="b" * 64,
        source_files=tuple(
            {
                "path": f"file-{index}.txt",
                "size_bytes": 1,
                "sha256": "c" * 64,
                "status": "readable",
            }
            for index in range(20)
        ),
        matches=tuple(f"file-{index}.txt" for index in range(20)),
        skipped=(),
    )
    monkeypatch.setattr(discovery_witness, "MAX_DISCOVERY_WITNESS_BYTES", 128)
    monkeypatch.setattr(
        discovery_witness.DiscoverySourceWitness,
        "payload",
        lambda _self: pytest.fail("serialized witness must not materialize its complete payload"),
    )

    with pytest.raises(discovery_witness.DiscoveryWitnessLimitError, match="serialized bytes"):
        witness.serialized_payload()


def test_grep_producer_uses_a_real_regex_timeout(tmp_path: Path, monkeypatch: Any) -> None:
    """The registered grep producer refuses catastrophic patterns through its bounded engine."""
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, _log = _manager_context(tmp_path, run_id, artifacts_root)
    (context.workspace / "a.txt").write_text("a" * 1_000 + "!\n", encoding="utf-8")
    monkeypatch.setattr(search_builtins, "_REGEX_TIMEOUT_SECONDS", 0.000001)

    with pytest.raises(ToolExecutionError, match="time budget"):
        Grep().execute(context.for_tool(), {"pattern": "(a+)+$"})


def test_a33_independent_projection_uses_a_real_regex_timeout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MIRA's separate retained-snapshot projection has its own regex timeout."""
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _manager_context(tmp_path, run_id, artifacts_root)
    (context.workspace / "a.txt").write_text("a" * 300 + "!\n", encoding="utf-8")
    monkeypatch.setattr(discovery_witness, "_REGEX_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(search_builtins, "_REGEX_TIMEOUT_SECONDS", 1.0)
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "(a+)+$"}
    )
    assert execution.result.success
    monkeypatch.setattr(discovery_evidence, "_REGEX_TIMEOUT_SECONDS", 0.000001)

    result = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in result.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.FAILED
    assert "regular expression" in a33.detail


@pytest.mark.parametrize(
    ("budget", "value", "filename", "content"),
    [
        ("MAX_DISCOVERY_SCAN_BYTES", 0, "scan.txt", "x"),
        ("MAX_DISCOVERY_CONTENT_BYTES", 1, "content.txt", "needle"),
        ("MAX_DISCOVERY_WITNESS_BYTES", 1, None, None),
    ],
)
def test_manager_refuses_each_witness_budget_before_tool_started(
    tmp_path: Path,
    monkeypatch: Any,
    budget: str,
    value: int,
    filename: str | None,
    content: str | None,
) -> None:
    """Scan, retained-content and serialized-witness budgets fail closed before execution."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if filename is not None and content is not None:
        (workspace / filename).write_text(content, encoding="utf-8")
    monkeypatch.setattr(discovery_witness, budget, value)
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "needle"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_WITNESS:")
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())


def test_a33_refuses_a_witness_artifact_over_its_bounded_read_limit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MIRA rejects oversized witness metadata before loading/parsing its body."""
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "needle"}
    )
    assert execution.result.success
    monkeypatch.setattr(discovery_evidence, "_MAX_DISCOVERY_ARTIFACT_BYTES", 1)

    result = audit_run(
        AuditContext(
            run_id=run_id,
            events=log.events(),
            store=context.artifact_store,
            audit_mode=AuditMode.FINAL,
        )
    )
    a33 = next(control for control in result.controls if control.control_id == "A33")
    assert a33.status is ControlStatus.FAILED
    assert "exceeds" in a33.detail


def test_grep_producer_rejects_growth_without_unbounded_read(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A stat/open race cannot make the producer call ``Path.read_text`` on a whole file."""
    run_id = new_id("run")
    context, _log = _manager_context(tmp_path, run_id, tmp_path / "artifacts")
    growing = context.workspace / "growing.txt"
    growing.write_text("small", encoding="utf-8")
    original_open = Path.open
    opened_modes: list[str | None] = []
    grew = False

    def grow_after_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal grew
        mode = args[0] if args else kwargs.get("mode")
        opened_modes.append(mode)
        if path == growing and not grew and mode in {"r", "rb"}:
            grew = True
            with original_open(growing, "wb") as handle:
                handle.write(b"x" * (search_builtins._MAX_GREP_FILE_BYTES + 1))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_after_stat)
    with pytest.raises(ToolExecutionError, match="grep input"):
        Grep().execute(context.for_tool(), {"pattern": "needle"})
    assert "rb" in opened_modes
    assert "r" not in opened_modes


def test_grep_producer_enforces_its_own_deadline_during_file_read(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A slow real file open is part of the producer's bounded execution phase."""
    run_id = new_id("run")
    context, _log = _manager_context(tmp_path, run_id, tmp_path / "artifacts")
    (context.workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_builtins, "MAX_GREP_SECONDS", 0.01, raising=False)
    original_open = Path.open

    def delayed_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        time.sleep(0.03)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", delayed_open)
    with pytest.raises(ToolExecutionError, match="time budget"):
        Grep().execute(context.for_tool(), {"pattern": "needle"})


def test_grep_producer_refuses_an_aggregate_scan_overflow(tmp_path: Path, monkeypatch: Any) -> None:
    """Several small inputs cannot bypass the aggregate scan budget."""
    run_id = new_id("run")
    context, _log = _manager_context(tmp_path, run_id, tmp_path / "artifacts")
    (context.workspace / "a.txt").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(search_builtins, "MAX_GREP_SCAN_BYTES", 4)
    with pytest.raises(ToolExecutionError, match="scan exceeds"):
        Grep().execute(context.for_tool(), {"pattern": "1"})


def test_grep_producer_checks_deadline_after_discovery_serialization(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A slow persistence serialization cannot return a successful discovery result."""
    run_id = new_id("run")
    context, _log = _manager_context(tmp_path, run_id, tmp_path / "artifacts")
    (context.workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(search_builtins, "MAX_GREP_SECONDS", 0.01, raising=False)
    original_persist = search_builtins.persist_discovery

    def delayed_persist(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.03)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(search_builtins, "persist_discovery", delayed_persist)
    with pytest.raises(ToolExecutionError, match="time budget"):
        Grep().execute(context.for_tool(), {"pattern": "needle"})


def test_manager_refuses_credential_bearing_grep_before_persisting_a_witness(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Known credential source bytes never become a duplicate LOG witness."""
    credential_value = "sk-ExampleSecretValue1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", credential_value)
    run_id = new_id("run")
    context, log = _manager_context(tmp_path, run_id, tmp_path / "artifacts")
    (context.workspace / "config.env").write_text(
        f"OPENAI_API_KEY={credential_value}\n", encoding="utf-8"
    )

    execution = ToolManager(ToolRegistry((cast("Tool", Grep()),))).execute(
        context, "grep", {"pattern": "definitely-no-match"}
    )

    assert execution.result.success is False
    assert execution.result.error is not None
    assert "credential" in execution.result.error.lower()
    assert not any(event.type is EventType.TOOL_STARTED for event in log.events())
    assert all(credential_value not in canonical_json(event.payload) for event in log.events())
    assert all(
        credential_value.encode("utf-8") not in context.artifact_store.load_bytes(artifact.name)
        for artifact in context.artifact_store.list_active()
    )
