"""A fake local `Tool` and a review-gated `ToolContext`, shared across tool-bridge/THY tests.

`FakeTool` never calls a subprocess or the filesystem: `execute()` just records the invocation it
saw and returns a canned `ToolResult` (or raises, when `raises=True`), so a test can assert on
`seen_invocation` without depending on a real tool's behaviour. `review_gated_context` builds a
`ToolContext` whose low-confidence `RiskProfile` makes the default policy stack escalate every
local tool call to `REQUIRE_HUMAN_REVIEW`: with no `approver` the Gate is `DEFERRED` and answers
nothing (the call ends PENDING, THY-17); given one, the Gate is `SYNCHRONOUS` and records the
approver's answer as evidence, never authority (C-15) -- the call is still denied either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from thymira.events import InMemoryEventLog
from thymira.policies import (
    ApprovalRequest,
    CapabilityRule,
    Gate,
    GateMode,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    load_policy_stack,
)
from thymira.schemas import Actor, ActorKind, Decision, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolInvocation,
    ToolResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.events import EventLog
    from thymira.policies import Approver


@dataclass(frozen=True, slots=True)
class FakeTool(Tool):
    """A local tool that records what it was invoked with and returns a canned result."""

    name: str
    capability: ToolCapability
    description: str = "A fake bridge tool."
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue
    result: ToolResult = field(
        default_factory=lambda: ToolResult(
            success=True, stdout="ok", value=LegacyToolValue(text="ok")
        )
    )
    raises: bool = False
    seen_invocation: list[ToolInvocation] = field(default_factory=list)
    seen_arguments: list[dict[str, Any]] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        self.seen_invocation.append(invocation)
        self.seen_arguments.append(arguments)
        if self.raises:
            raise ToolExecutionError("expected failure")
        return self.result


class EchoArguments(BaseModel):
    """The arguments schema for a `FakeTool` named "echo" in tests that call it through a model."""

    value: str = Field(min_length=1)


_TEST_HUMAN = Actor(
    kind=ActorKind.HUMAN,
    id="sandbox-integration-reviewer",
    role="test-reviewer",
    authenticated=True,
)


def _human_approve(_request: ApprovalRequest) -> bool:
    """Answer a capability review as the explicit human fixture identity."""
    return True


def tool_invocation(tmp_path: Path) -> ToolInvocation:
    """Build a real tool invocation backed by isolated temporary state."""
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def development_policy() -> Policy:
    """Return a policy that explicitly permits trusted temporary-directory test effects."""
    base = load_policy_stack()
    return base.model_copy(
        update={
            "capability_rules": (
                *[rule for rule in base.capability_rules if rule.id not in {"GOV-003", "GOV-008"}],
                CapabilityRule(
                    id="test-development-side-effects",
                    description="Allow local side effects in trusted temporary-directory tests.",
                    side_effects=("*",),
                    decision=Decision.PASS,
                    reason="Trusted test code may write isolated temporary state.",
                ),
                CapabilityRule(
                    id="test-development-unconfined",
                    description="Allow unconfined subprocesses in trusted tmp_path tests.",
                    risk_tags=("unconfined_code",),
                    decision=Decision.PASS,
                    reason="Trusted test code runs with explicit danger-full-access in tmp_path.",
                ),
            ),
        }
    )


def review_gated_context(
    tmp_path: Path,
    *,
    run_id: str,
    log: EventLog | None = None,
    approver: Approver | None = None,
    engine: PolicyEngine | None = None,
) -> ToolContext:
    """A `ToolContext` whose Gate escalates every local tool call to a human (low-confidence risk).

    `log` defaults to a fresh `InMemoryEventLog(run_id)` when omitted. `approver=None` (the
    default) builds a `DEFERRED` Gate that answers nothing, so a review-gated call ends PENDING;
    passing an `Approver` builds a `SYNCHRONOUS` Gate instead, whose answer is recorded as evidence
    but never as authority. `engine` replaces the default policy stack, for a test that needs
    rules of its own -- execution constraints or a budget ceiling.
    """
    event_log = log if log is not None else InMemoryEventLog(run_id)
    mode = GateMode.DEFERRED if approver is None else GateMode.SYNCHRONOUS
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=event_log,
        gate=Gate(
            engine or PolicyEngine(load_policy_stack()),
            event_log,
            approver=approver,
            mode=mode,
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=0.1
        ),
    )


def human_approved_context(
    tmp_path: Path,
    *,
    run_id: str,
    log: EventLog | None = None,
    engine: PolicyEngine | None = None,
) -> ToolContext:
    """Build a review-gated context with an explicit human approval for side-effect tests."""
    context = review_gated_context(
        tmp_path,
        run_id=run_id,
        log=log,
        approver=_human_approve,
        engine=engine,
    )
    return replace(
        context,
        gate=Gate(
            context.gate.engine,
            context.event_log,
            approver=_human_approve,
            human=_TEST_HUMAN,
        ),
    )
