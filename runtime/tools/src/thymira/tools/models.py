"""In-process Tool Manager contracts owned by the P3 member."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from thymira.policies import DEFAULT_APPROVAL_TTL
from thymira.schemas import ExecutionConstraints, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.events import EventLog
    from thymira.policies import Gate, RiskProfile, ToolCapability
    from thymira.schemas import (
        EventType,
        PolicyDecision,
        SandboxEnforcement,
        SandboxMode,
        ToolCall,
    )
    from thymira.state import ArtifactStore
    from thymira.tools.sandbox.base import ResolvedExecutionSpec, WorkspaceQuotaEvidence
    from thymira.tools.sandbox.termination import TerminationEvidence


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """What a tool is handed: a workspace and a store, never the event log or the Gate.

    A tool holding a writable `EventLog` could append its own `human.approval` — the event would
    chain, `verify()` would call it valid, and the tool would have manufactured the approval a
    `REQUIRE_HUMAN_REVIEW` decision was waiting for. Authorisation and recording belong to the
    Tool Manager; a tool only ever produces a :class:`ToolResult` and lets the manager write it.
    """

    run_id: str
    agent_id: str
    workspace: Path
    artifact_store: ArtifactStore
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetRefusal:
    """Why a Core-owned budget guard refuses one tool call, and under which decision.

    ``decision_id`` is the ``policy.decision`` the guard obtained before refusing -- the Gate's
    run-level budget decision. The denial names it, so the log alone says what refused the call:
    without it the ``tool.denied`` pointed at the capability decision that had just *allowed* the
    call, and nothing in the Run explained the refusal.
    """

    reason: str
    decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Runtime-owned context for one tool invocation.

    Held by the Tool Manager, never by a tool: it carries the `Gate` that authorises the call and
    the log that records it. ``allowed_tools`` is an optional caller-owned allowlist enforced by
    the manager before policy evaluation; :meth:`for_tool` narrows the context to the part a tool
    may see.
    """

    run_id: str
    agent_id: str
    workspace: Path
    event_log: EventLog
    gate: Gate
    artifact_store: ArtifactStore
    risk_profile: RiskProfile
    task_id: str | None = None
    project_context: str | None = None
    """The project's own `.thymira/context.md`, when the project declares one (bug-hunt H4):
    domain knowledge the runtime already loaded at Inspect but that never reached a delegated
    step's prompt. Carried here so every step's runtime-context snapshot can surface it, not just
    THY's own Plan."""
    workspace_dataset_paths: tuple[tuple[str, str], ...] = ()
    """Logical dataset names paired with their relative paths in this Run's workspace.

    The artifact store uses its own immutable names (for example, `datasets/<name>.csv`), which
    are evidence identifiers rather than filesystem paths. A coding agent needs this mapping to
    use `read_file` and `run_python` against the staged project input without guessing either
    representation.
    """
    execution_constraints: ExecutionConstraints = field(default_factory=ExecutionConstraints)
    execution_decision: PolicyDecision | None = None
    available_evidence: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] | None = None
    delegation_depth: int = 0
    """How deep the calling agent sits in the delegation chain; the root agent is zero.

    Part of an approval's *scope*, never of the call's identity: a credit is spendable only at
    the depth it was raised at, so a delegate draws on no authority a human extended to its
    parent. A tool never sees it -- :meth:`for_tool` does not pass it on."""
    approval_ttl: timedelta = DEFAULT_APPROVAL_TTL
    """How long an approval this context raises stays spendable.

    A runtime-owned constant with a per-context override, not configuration: the deadline is the
    backstop behind pass-level release and Run-terminal closure. A tool never sees it."""
    now: Callable[[], datetime] = utc_now
    """The clock the manager reads when it mints and when it spends an approval scope.

    Injected so expiry is testable without sleeping; a tool never sees it."""
    budget_guard: Callable[[], BudgetRefusal | str | None] | None = None
    """Core-owned budget check, consulted after the capability decision and before the call runs.

    ``None`` allows the call. A :class:`BudgetRefusal` refuses it and says under which decision:
    ``decision_id`` is the ``policy.decision`` the guard obtained before refusing, or ``None``
    when the refusal was taken without one -- the manager records exactly that on the
    ``tool.denied``, so the log never attributes the refusal to the decision that allowed the
    call. A plain string is the same refusal with no decision to name, and is normalised to
    ``BudgetRefusal(reason)`` before it is recorded."""

    def for_tool(self) -> ToolInvocation:
        """Return the narrowed view handed to the tool itself."""
        return ToolInvocation(
            run_id=self.run_id,
            agent_id=self.agent_id,
            workspace=self.workspace,
            artifact_store=self.artifact_store,
            task_id=self.task_id,
        )


@dataclass(frozen=True, slots=True)
class ToolEvent:
    """An event declaration the Manager may append after a tool returns."""

    type: EventType
    payload: dict[str, Any]


class ToolResultCode(StrEnum):
    """Closed result outcomes that need more meaning than a boolean success flag."""

    SUCCESS = "SUCCESS"
    FAILURE = "TOOL_FAILURE"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Bounded result returned by a tool implementation.

    ``sandbox_mode`` and ``sandbox_enforcement`` are the confinement facts a tool that executes
    code must report; the manager copies them onto the recorded ``ToolCall``. A tool that runs no
    code leaves them ``None``. Enforcement is reported, never inferred: a tool that ran
    unconfined says ``PARTIAL``, because claiming ``FULL`` would put a false statement inside the
    evidence chain and silence the MIRA control meant to catch it.

    ``sandbox_spec``, ``sandbox_cleanup_confirmed`` and ``sandbox_termination`` are three more
    facts a code-executing tool reports, and they stay separate from the first two: requested
    mode, actual mode, enforcement, exit outcome, cleanup result and termination evidence are six
    orthogonal facts (F6.6/F6.8) -- a cleanup failure never changes what enforcement or exit_code
    say, a weak enforcement value never implies a cleanup failure, and a deadline that passed is
    recorded in ``sandbox_termination`` whatever exit code the child itself produced.
    """

    success: bool
    value: BaseModel | None = None
    """The immutable, schema-validated value produced by the tool.

    This is the source for model and client projections.  The legacy ``stdout``/``stderr``
    fields remain on the envelope for lifecycle compatibility, but the manager replaces the
    model-facing stdout with :func:`thymira.tools.results.value_text` after validation and clears
    auxiliary stderr unless the typed value declares it (as ``ProcessToolValue`` does).
    """
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    artifact_ids: tuple[str, ...] = ()
    result_sha256: str | None = None
    error: str | None = None
    sandbox_mode: SandboxMode | None = None
    sandbox_enforcement: SandboxEnforcement | None = None
    sandbox_spec: ResolvedExecutionSpec | None = None
    sandbox_cleanup_confirmed: bool | None = None
    sandbox_termination: TerminationEvidence | None = None
    quota_evidence: WorkspaceQuotaEvidence | None = None
    events: tuple[ToolEvent, ...] = ()
    code: ToolResultCode | None = None
    """Machine-readable outcome code; the manager fills an absent code from ``success``."""
    timeout_s: float | None = None
    """The execution budget when this result represents a timeout."""
    aborted: bool = False
    """Whether the child was actively terminated when producing this result."""


class ToolExecutionError(RuntimeError):
    """Expected execution failure that the manager records as a failed ToolCall."""


class Tool(Protocol):
    """Implementation contract for one registered tool."""

    name: str
    capability: ToolCapability
    description: str
    arguments_model: type[BaseModel] | None
    result_model: type[BaseModel]

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Execute an already-authorised call and return its result.

        The invocation deliberately exposes no event log and no Gate: a tool reports, it never
        records or authorises. Anything the tool wants on the chain travels back in the
        :class:`ToolResult` and is written by the manager.
        """
        ...


def input_schema(tool: Tool) -> dict[str, Any]:
    """Return the JSON schema advertised by ``tool`` to external callers."""
    if tool.arguments_model is None:
        return {}
    return tool.arguments_model.model_json_schema()


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """What the manager hands back: the recorded call, its result, and a review still pending."""

    call: ToolCall
    result: ToolResult
    pending_approval: PolicyDecision | None = None
    """Set only when the call was denied because a human must first approve it and no answer
    exists yet: the ``REQUIRE_HUMAN_REVIEW`` decision the Gate recorded. The agent bridge ends
    the step on it; every other denial leaves it ``None`` and is an ordinary result."""
