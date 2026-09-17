"""MIRA control A33: discovery evidence substantiates the rendered result (F3.2/F3.3).

Real: `ToolManager`, `Gate`/`PolicyEngine`, a real `JsonlEventLog` and `LocalArtifactStore`. The
forged-artifact nodes drive a small test-local `Tool` through the same real manager, so
`tool.started`/`artifact.created`/`tool.completed` are genuine manager-written evidence; only the
artifact's own *content* (and, for the digest node, the artifact's bytes on disk after the fact)
is dishonest -- exactly the shape A33 exists to catch.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tests.thymira.fixtures_tools import development_policy
from thymira.events import JsonlEventLog, read_events, sha256_text, verify_log
from thymira.mira.checks import AuditContext, AuditMode, ControlStatus, audit_run
from thymira.policies import Gate, PolicyEngine, RiskProfile, ToolCapability, auto_approve
from thymira.schemas import Actor, ArtifactKind, EventType, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    DiscoveryValue,
    Tool,
    ToolContext,
    ToolManager,
    ToolRegistry,
    ToolResult,
)
from thymira.tools.builtins.files import EditFile, ReadFile
from thymira.tools.builtins.freshness import FreshnessPolicy, ReadLedger
from thymira.tools.builtins.search import Glob

if TYPE_CHECKING:
    from pydantic import BaseModel

    from thymira.mira.checks import ControlResult
    from thymira.schemas import Event
    from thymira.state import ArtifactStore

_RISK = RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0)
_HONEST_SCHEMA = "thymira.discovery/1"


def _context(
    tmp_path: Path, run_id: str, artifacts_root: Path
) -> tuple[ToolContext, JsonlEventLog]:
    log = JsonlEventLog(tmp_path / "runs" / f"{run_id}.jsonl", run_id)
    workspace = tmp_path / "workspace" / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(artifacts_root, run_id),
        risk_profile=_RISK,
    )
    return context, log


def _a33(events: list[Event], store: ArtifactStore | None, run_id: str) -> ControlResult:
    ctx = AuditContext(run_id=run_id, events=events, store=store, audit_mode=AuditMode.FINAL)
    report = audit_run(ctx)
    return next(c for c in report.controls if c.control_id == "A33")


def test_a33_passes_a_real_capped_glob(tmp_path: Path) -> None:
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, _log = _context(tmp_path, run_id, artifacts_root)
    workspace = context.workspace

    # Adversarial creation order relative to the eventual path sort: reverse-alphabetical top
    # level, mixed nesting, and deliberately unsorted subdirectory contents.
    names = [f"dir{(129 - i) % 13:02d}/f{i:04d}.txt" for i in range(130)]
    for relative in names:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    manager = ToolManager(ToolRegistry((cast("Tool", Glob()),)))
    execution = manager.execute(context, "glob", {"pattern": "**/*.txt"})
    assert execution.result.success

    # Independent oracle leg 1: a separately constructed JsonlEventLog, verified end to end.
    log_path = tmp_path / "runs" / f"{run_id}.jsonl"
    verification = verify_log(log_path)
    assert verification.valid
    reread_events = read_events(log_path)

    # Independent oracle leg 2: a second, separately constructed LocalArtifactStore.
    reopened_store = LocalArtifactStore(artifacts_root, run_id)
    assert reopened_store.verify() == []

    result = _a33(reread_events, reopened_store, run_id)
    assert result.status is ControlStatus.PASSED, result.detail

    # Independent oracle leg 3: the workspace-truth comparison, using no code from
    # thymira.tools.builtins.search -- a locally written os.walk + fnmatch enumeration.
    independent: list[str] = []
    for root, _dirs, files in os.walk(workspace):
        for filename in files:
            if fnmatch.fnmatch(filename, "*.txt"):
                full = Path(root) / filename
                independent.append(full.relative_to(workspace).as_posix())
    independent.sort()

    artifact_name = next(
        name
        for name, artifact in reopened_store.manifest().items()
        if artifact.id in execution.result.artifact_ids
    )
    payload = reopened_store.load_json(artifact_name)
    assert payload["matches"] == independent
    assert payload["total"] == len(independent)
    assert len(independent) == 130

    completed = next(event for event in reread_events if event.type is EventType.TOOL_COMPLETED)
    witness_id = completed.payload["discovery_witness_artifact_id"]
    assert witness_id in completed.payload["artifact_ids"]
    witness_name = next(
        name for name, artifact in reopened_store.manifest().items() if artifact.id == witness_id
    )
    witness = reopened_store.load_json(witness_name)
    assert witness["query"] == {"pattern": "**/*.txt", "path": "."}
    assert witness["matches"] == independent
    assert witness["source"]["files"]


# ------------------------------------------------------------------------- the forged-tool rig


@dataclass(frozen=True, slots=True)
class _ForgedDiscoveryTool:
    """A local tool that persists a hand-built (possibly dishonest) discovery payload."""

    payload: dict[str, Any]
    name: str = "glob"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="glob", data_access=("workspace",), external_effects=()
        )
    )
    description: str = "Forged glob for MIRA A33 tests."
    arguments_model: type[Any] | None = None
    result_model: type[BaseModel] = DiscoveryValue
    persist: bool = True
    tamper_path: Path | None = None
    tamper_bytes: bytes | None = None

    def execute(self, invocation: Any, _arguments: dict[str, Any]) -> ToolResult:
        rendered_locations = self.payload.get("rendered", {}).get("locations") or []
        rendered_text = "\n".join(rendered_locations)
        artifact_ids: tuple[str, ...] = ()
        if self.persist:
            name = f"discovery/glob/{new_id('artifact')}.json"
            payload = {**self.payload, "rendered": {**self.payload["rendered"]}}
            artifact = invocation.artifact_store.save_json(
                name, payload, produced_by=invocation.agent_id, kind=ArtifactKind.LOG
            )
            artifact_ids = (artifact.id,)
            if self.tamper_path is not None and self.tamper_bytes is not None:
                # Tamper with the immutable object after the manifest (and artifact.created
                # event) recorded the honest sha256. The URI is the public object identity; the
                # logical name is only the manifest lookup key.
                (self.tamper_path / artifact.uri).write_bytes(self.tamper_bytes)
        return ToolResult(
            success=True,
            stdout=rendered_text,
            artifact_ids=artifact_ids,
            result_sha256=sha256_text(rendered_text),
            value=DiscoveryValue(
                text=rendered_text,
                matches=tuple(rendered_locations),
                total_count=int(self.payload["total"]),
                returned_count=len(rendered_locations),
                omitted_count=max(0, int(self.payload["total"]) - len(rendered_locations)),
            ),
        )


def _honest_payload(
    matches: list[str] | None = None, locations: list[str] | None = None, total: int | None = None
) -> dict[str, Any]:
    real_matches = matches if matches is not None else ["a.txt", "b.txt", "c.txt"]
    real_locations = locations if locations is not None else list(real_matches)
    return {
        "schema": _HONEST_SCHEMA,
        "tool": "glob",
        "query": {},
        "order": "path",
        "total": total if total is not None else len(real_matches),
        "matches": real_matches,
        "skipped": [],
        "rendered": {
            "count": len(real_locations),
            "truncated": len(real_locations) < len(real_matches),
            "sha256": sha256_text("\n".join(real_locations)),
            "locations": real_locations,
        },
    }


def _run_forged(
    tmp_path: Path, tool: _ForgedDiscoveryTool
) -> tuple[list[Event], ArtifactStore, str]:
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _context(tmp_path, run_id, artifacts_root)
    manager = ToolManager(ToolRegistry((cast("Tool", tool),)))
    manager.execute(context, "glob", {"pattern": "*.txt"})
    return log.events(), context.artifact_store, run_id


def test_a33_refuses_a_rendering_whose_total_the_persisted_list_does_not_support(
    tmp_path: Path,
) -> None:
    payload = _honest_payload(total=999)  # claims far more than the three persisted matches
    events, store, run_id = _run_forged(tmp_path, _ForgedDiscoveryTool(payload=payload))

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "glob" in result.detail
    assert any(event.type is EventType.TOOL_STARTED for event in events)


def test_a33_refuses_a_rendering_that_is_not_a_prefix_of_the_persisted_list(
    tmp_path: Path,
) -> None:
    payload = _honest_payload(locations=["c.txt", "b.txt", "a.txt"])  # reordered head
    events, store, run_id = _run_forged(tmp_path, _ForgedDiscoveryTool(payload=payload))

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "prefix" in result.detail


def test_a33_refuses_a_discovery_call_that_persisted_nothing(tmp_path: Path) -> None:
    events, store, run_id = _run_forged(
        tmp_path, _ForgedDiscoveryTool(payload=_honest_payload(), persist=False)
    )

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "no discovery artifact" in result.detail


def test_a33_refuses_an_artifact_whose_bytes_do_not_hash_to_its_artifact_created_event(
    tmp_path: Path,
) -> None:
    tampered = json.dumps({**_honest_payload(), "matches": ["a.txt"], "total": 3}).encode("utf-8")
    events, store, run_id = _run_forged(
        tmp_path,
        _ForgedDiscoveryTool(
            payload=_honest_payload(), tamper_path=tmp_path / "artifacts", tamper_bytes=tampered
        ),
    )

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "hash" in result.detail


def test_a33_refuses_an_out_of_order_or_duplicated_match_list(tmp_path: Path) -> None:
    payload = _honest_payload(
        matches=["a.txt", "a.txt", "b.txt"], locations=["a.txt", "a.txt", "b.txt"]
    )
    events, store, run_id = _run_forged(tmp_path, _ForgedDiscoveryTool(payload=payload))

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "duplicate" in result.detail


def test_a33_refuses_a_match_list_that_is_not_sorted(tmp_path: Path) -> None:
    payload = _honest_payload(
        matches=["c.txt", "a.txt", "b.txt"], locations=["c.txt", "a.txt", "b.txt"]
    )
    events, store, run_id = _run_forged(tmp_path, _ForgedDiscoveryTool(payload=payload))

    result = _a33(events, store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "ordered" in result.detail


def test_a33_is_not_evaluated_without_an_artifact_store(tmp_path: Path) -> None:
    events, _store, run_id = _run_forged(tmp_path, _ForgedDiscoveryTool(payload=_honest_payload()))

    result = _a33(events, None, run_id)

    assert result.status is ControlStatus.NOT_EVALUATED


def test_a33_accepts_a_real_stale_version_refusal(tmp_path: Path) -> None:
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _context(tmp_path, run_id, artifacts_root)
    (context.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")

    ledger = ReadLedger()
    policy = FreshnessPolicy(require_read_before_edit=True)
    registry = ToolRegistry(
        (
            cast("Tool", ReadFile(ledger=ledger)),
            cast("Tool", EditFile(ledger=ledger, freshness=policy)),
        )
    )
    manager = ToolManager(registry)
    manager.execute(context, "read_file", {"path": "a.py"})
    (context.workspace / "a.py").write_text(
        "x = 999\n", encoding="utf-8"
    )  # changed behind its back
    edit = manager.execute(
        context,
        "edit_file",
        {"path": "a.py", "old_string": "x = 999", "new_string": "x = 2", "description": "Bump"},
    )
    assert not edit.result.success
    assert (edit.result.error or "").startswith("FS_STALE_VERSION")

    result = _a33(log.events(), context.artifact_store, run_id)
    assert result.status is ControlStatus.PASSED, result.detail


def test_a33_refuses_an_edit_refusal_whose_sentence_does_not_recompute(tmp_path: Path) -> None:
    run_id = new_id("run")
    artifacts_root = tmp_path / "artifacts"
    context, log = _context(tmp_path, run_id, artifacts_root)
    system = Actor.system()

    log.append(
        EventType.TOOL_STARTED,
        system,
        {"tool_call_id": "call-1", "tool": "edit_file", "arguments": {"path": "a.py"}},
        subject_id="call-1",
    )
    log.append(
        EventType.TOOL_COMPLETED,
        system,
        {
            "tool_call_id": "call-1",
            "tool": "edit_file",
            "status": "FAILED",
            "error": "FS_STALE_VERSION: this is not the real recovery sentence",
            "artifact_ids": [],
            "result_sha256": None,
        },
        subject_id="call-1",
    )

    result = _a33(log.events(), context.artifact_store, run_id)

    assert result.status is ControlStatus.FAILED
    assert "recompute" in result.detail
