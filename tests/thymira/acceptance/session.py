"""Load recorded, provider-neutral acceptance sessions.

The fixture format deliberately contains only the scripted model turns. Prompts and tool
schemas are captured by the acceptance test as readable sidecars, while the workspace oracle
is generated from the files the run actually leaves behind.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.thy import AgentTask, PlanOutput, SynthesisNarrative, ThyAgentKind, ThyPhase

SESSION_PATH = Path(__file__).with_name("demo.session.jsonl")
SNAPSHOT_MODES = frozenset({"replay", "record", "refresh"})


def load_demo_session(path: Path = SESSION_PATH) -> list[Any]:
    """Load the demo session JSONL into ``ScriptedProvider`` response objects."""
    responses: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid session JSON on line {line_number}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
            raise TypeError(f"session line {line_number} must contain a kind")
        kind = record["kind"]
        if kind == "plan":
            responses.append(
                PlanOutput(
                    tasks=tuple(
                        AgentTask(
                            id=task["id"],
                            agent=ThyAgentKind(task["agent"]),
                            phase=ThyPhase(task["phase"]),
                            instruction=task["instruction"],
                        )
                        for task in record["tasks"]
                    )
                )
            )
        elif kind == "tool_call":
            responses.append(
                LLMToolCall(id=record["id"], name=record["name"], arguments=record["arguments"])
            )
        elif kind == "synthesis":
            responses.append(
                SynthesisNarrative(tradeoffs=record["tradeoffs"], limitations=record["limitations"])
            )
        else:
            raise ValueError(f"unsupported session turn kind {kind!r} on line {line_number}")
    return responses


def workspace_snapshot(
    workspace: Path, *, paths: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Return a deterministic manifest of the selected files in a run workspace."""
    files: list[dict[str, Any]] = []
    candidates = (candidate for candidate in workspace.rglob("*") if candidate.is_file())
    for path in sorted(
        candidates, key=lambda candidate: candidate.relative_to(workspace).as_posix().casefold()
    ):
        relative = path.relative_to(workspace)
        if paths is not None and relative.as_posix() not in paths:
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": relative.as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {"files": files}


def snapshot_mode() -> str:
    """Read and validate the acceptance snapshot mode."""
    mode = os.environ.get("THYMIRA_SNAPSHOT", "replay")
    if mode not in SNAPSHOT_MODES:
        choices = ", ".join(sorted(SNAPSHOT_MODES))
        raise ValueError(f"THYMIRA_SNAPSHOT must be one of {choices}; got {mode!r}")
    return mode


def assert_or_update(path: Path, content: str, *, mode: str) -> None:
    """Compare a fixture or update it only in the explicit ``refresh`` mode."""
    if mode == "refresh":
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    if mode == "record":
        if path.exists():
            raise AssertionError(f"{path.name} exists; use THYMIRA_SNAPSHOT=refresh to replace it")
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    actual = path.read_text(encoding="utf-8")
    if actual != content:
        diff = "".join(
            difflib.unified_diff(
                actual.splitlines(keepends=True), content.splitlines(keepends=True)
            )
        )
        raise AssertionError(
            f"{path.name} drifted; run with THYMIRA_SNAPSHOT=refresh and review the diff\n{diff}"
        )


def provider_from_demo_session() -> ScriptedProvider:
    """Build the offline provider from the committed demo session."""
    return ScriptedProvider(load_demo_session())
