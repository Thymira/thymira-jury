"""Regression tests for `spill_result`: redaction at the persistence boundary and honest digests.

Two defects are pinned here:

* N1 -- the artifact store performs no redaction, so oversized tool output carrying a secret was
  written verbatim to `artifacts/tool-results/<call>/<field>.log` while the event log's copy of the
  same text was redacted. `spill_result` is the trust boundary and must redact before it persists.
* 74-1 -- past the inline limit, `spill_result` overwrote any `result_sha256` with a sha256 over a
  private, NUL-separated `b"".join(...)` concatenation that nothing else in the repository can
  recompute, silently discarding a digest the tool itself computed honestly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from thymira.events import InMemoryEventLog, redact, sha256_text
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import (
    Gate,
    PolicyEngine,
    RiskProfile,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import Actor, EventType, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolContext, ToolResult, spill_result

if TYPE_CHECKING:
    import pytest

# A terminated PEM block: `redact` masks the whole `BEGIN`..`END` span with one marker, so the
# redacted text is both shorter than and byte-for-byte different from the original.
# The banners are assembled at run time, as tests/thymira/test_events.py does and for the
# same reason: spelled out in full they would trip the repository's detect-private-key
# pre-commit hook, and the hook is worth more armed than this literal is worth spelled out.
_KEY_WORDS = "PRIVATE " + "KEY"
_BEGIN_KEY = f"-----BEGIN RSA {_KEY_WORDS}-----"
_END_KEY = f"-----END RSA {_KEY_WORDS}-----"
_PRIVATE_KEY = (
    f"{_BEGIN_KEY}\n"
    + "MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4wggE6AgEAAkEA1234567abcXYZ\n" * 6
    + f"{_END_KEY}\n"
)
# The secret sits at the very start so that, before the fix, both the inline head and the stored
# artifact leak it; the padding forces a spill and keeps the redacted text over the inline limit.
_SECRET_STDOUT = _PRIVATE_KEY + "padding to force a spill\n" * 30
_INLINE_LIMIT = 128


def _context(tmp_path: Path) -> ToolContext:
    """Build a real `ToolContext`; `spill_result` reads only its agent id and artifact store."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_spill_redacts_a_secret_before_it_is_persisted_or_left_inline(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = spill_result(
        ToolResult(success=True, stdout=_SECRET_STDOUT),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    stored = context.artifact_store.load_text(f"tool-results/{context.agent_id}/stdout.log")
    assert _BEGIN_KEY not in stored
    assert "[REDACTED:PRIVATE_KEY]" in stored
    # The inline head is sliced from the redacted text, so the raw key never survives inline either.
    assert _BEGIN_KEY not in result.stdout


def test_spill_stores_the_redacted_text_the_manifest_digest_covers(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = spill_result(
        ToolResult(success=True, stdout=_SECRET_STDOUT),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    artifact = context.artifact_store.get(f"tool-results/{context.agent_id}/stdout.log")
    assert artifact is not None
    stored = context.artifact_store.load_text(artifact.name)
    # A single stream spilled and the tool set no digest: `result_sha256` is the digest of the
    # redacted text that was stored -- the same value the manifest holds -- so a verifier can
    # recompute it from the artifact alone.
    assert result.result_sha256 == artifact.sha256
    assert result.result_sha256 == sha256_text(stored)
    assert result.result_sha256 == sha256_text(redact(_SECRET_STDOUT))


def test_spill_does_not_overwrite_a_result_sha256_the_tool_already_set(tmp_path: Path) -> None:
    context = _context(tmp_path)
    tool_digest = sha256_text(_SECRET_STDOUT)

    result = spill_result(
        ToolResult(success=True, stdout=_SECRET_STDOUT, result_sha256=tool_digest),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    # A tool's claim about its own result is evidence; spilling stores the artifact but preserves
    # the digest the tool computed rather than replacing it with an unrecomputable one.
    assert result.result_sha256 == tool_digest
    assert result.artifact_ids


def test_spill_leaves_result_sha256_none_when_both_streams_spill(tmp_path: Path) -> None:
    context = _context(tmp_path)
    both = "A" * (_INLINE_LIMIT * 2)

    result = spill_result(
        ToolResult(success=True, stdout=both, stderr=both),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    # No canonical single text exists; the two artifacts already carry their own sha256, and a
    # fabricated concatenation digest is worse than no digest.
    assert result.result_sha256 is None
    assert len(result.artifact_ids) == 2


def test_spill_failure_keeps_the_inline_result_and_fabricates_no_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("artifact store unavailable")

    monkeypatch.setattr(context.artifact_store, "save_text", fail_save)
    result = spill_result(
        ToolResult(success=False, stdout=_SECRET_STDOUT, error="failed"),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    # A store that raises must never make the tool call fail: the inline result stands unchanged and
    # no artifact id or digest is invented for something that was never persisted.
    assert result.stdout == _SECRET_STDOUT
    assert result.artifact_ids == ()
    assert result.result_sha256 is None


def test_a_truncated_spill_is_detected_rather_than_served(tmp_path: Path) -> None:
    """The stored spill carries a sha256 a reader that performed none of the writes recomputes.

    Two independent readers grade the tampering: `LocalArtifactStore.verify`, which recomputes
    the file's digest from the manifest, and MIRA's A5 over a replayed log plus that same store
    -- neither of which imports `thymira.tools` or ran the spill (F6.8).
    """
    context = _context(tmp_path)
    log = context.event_log
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})

    result = spill_result(
        ToolResult(success=True, stdout="x" * (_INLINE_LIMIT * 4)),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    store = context.artifact_store
    assert isinstance(store, LocalArtifactStore)
    assert result.result_sha256 is not None
    assert store.verify() == []
    log.append(EventType.RUN_COMPLETED, system, {})
    intact = audit_run(AuditContext(log.run_id, log.events(), store))
    assert next(c for c in intact.controls if c.control_id == "A5").status is ControlStatus.PASSED

    spilled = next(
        artifact for artifact in store.list_active() if artifact.id in result.artifact_ids
    )
    path = tmp_path / "artifacts" / spilled.uri
    path.write_text("x" * 8, encoding="utf-8", newline="\n")

    problems = LocalArtifactStore(tmp_path / "artifacts", log.run_id).verify()
    assert any("modified artifact" in problem for problem in problems), problems
    regraded = audit_run(AuditContext(log.run_id, log.events(), store))
    assert next(c for c in regraded.controls if c.control_id == "A5").status is ControlStatus.FAILED


def test_a_spilled_result_lands_outside_the_sandbox_workspace(tmp_path: Path) -> None:
    """A confined child cannot reach, read or alter the artifact its own output was spilled to."""
    context = _context(tmp_path)

    result = spill_result(
        ToolResult(success=True, stdout="y" * (_INLINE_LIMIT * 4)),
        context,
        max_inline_bytes=_INLINE_LIMIT,
        produced_by=context.agent_id,
    )

    store = context.artifact_store
    assert isinstance(store, LocalArtifactStore)
    spilled = next(
        artifact for artifact in store.list_active() if artifact.id in result.artifact_ids
    )
    path = (tmp_path / "artifacts" / spilled.uri).resolve()
    assert not path.is_relative_to(Path(context.workspace).resolve())
