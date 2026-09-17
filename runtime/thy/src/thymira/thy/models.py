"""ThyGraph's contract: what starts a run, its own bookkeeping, and what it hands back.

ThyGraph (Inspect -> Plan -> Execute -> Summarize) coordinates the Data, Coding/Execution and
Experiment agents (MVP roadmap, Week 1). Only `ThyInput`/`ThyOutput` cross the boundary to
`thymira.core`; `ThyState` is ThyGraph's own bookkeeping between phases, never read by another
member. `THY_PHASE_ORDER` fixes the sequence as data, used both by `ThyState.advance()` and by
the LangGraph wiring in `thymira.thy.graph`.

`ThyState.usage` (`thymira.agents.usage.RunUsage`, THY-07) is a plain mutable dataclass, not a
`ThymiraModel` — pydantic v2 validates and round-trips a stdlib dataclass natively (verified: it
survives `model_dump()`/`model_validate()` without extra config), and LangGraph passes state
objects through in-process without forcing a JSON hop between nodes, so the same accumulator is
charged into across the whole run, not rebuilt per phase. "Surface source" (the roadmap's
deliverable text) is `run.id`, already carried by the `run: Run` field below — nothing new to add
for it; a node builds `current_surface(events)` from the run's own event log, not from `ThyState`.

`AgentTask.phase: ThyPhase` (THY-11's deliverable text says "tagged with Phase") is deliberately
ThyGraph's own phase, never `thymira.core.phases.Phase` (understanding/preparation/modeling/
evaluation/reporting): `thymira.thy` sits below `thymira.core` in the enforced layer order, so
importing the latter here would break `just check-imports`. `ThyPhase` is the only phase *enum*
this member defines. THY-24's rework loop (`ReworkSignal`, `thymira.thy.nodes.rework`) does need
the analytical-phase vocabulary those `Phase` values name -- it carries and validates them as plain
strings (never the imported enum), and a test locks those strings against `thymira.core.phases` so
the two never drift.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator, model_validator

from thymira.agents.usage import RunUsage
from thymira.schemas import (
    Artifact,
    ArtifactKind,
    ExecutionConstraints,
    Experiment,
    Framework,
    Id,
    PolicyDecision,
    Run,
    Task,
    TaskStatus,
    ThymiraModel,
)


class ThyPhase(StrEnum):
    """One step of the minimal ThyGraph."""

    INSPECT = "inspect"
    PLAN = "plan"
    EXECUTE = "execute"
    SUMMARIZE = "summarize"


THY_PHASE_ORDER: tuple[ThyPhase, ...] = (
    ThyPhase.INSPECT,
    ThyPhase.PLAN,
    ThyPhase.EXECUTE,
    ThyPhase.SUMMARIZE,
)

DEFAULT_MAX_REOPENS: int = 2
"""How many phase reopens a run allows before the rework loop stops with a budgeted refusal.

Small on purpose (ADR-0004): reopening is an exceptional, budgeted, justified move, not a retry
loop. A recipe (THY-28) or a caller can raise or lower it per run through `ThyState.max_reopens`.
"""


class ThyAgentKind(StrEnum):
    """Which specialised agent a task is delegated to.

    Each value equals the target agent's `AgentSpec.name` exactly (THY-01's rule): `execute_node`
    resolves a delegated `AgentTask` via `catalog.get(agent_task.agent.value)`, so a value that did
    not match a spec name would leave the task unresolvable. `DATA`/`CODING`/`EXPERIMENT` are the
    MVP roster; `STATISTICS`/`DATA_QUALITY`/`VISUALIZATION` are the shipped FINAL specialists THY-32
    opened this enum to. A specialist shipped later adds its member here the same way -- an enum
    member, a `full_agent_catalog` entry and a Plan-prompt line, never a change to `execute_node`,
    unless (like `ML`) it needs a follow-up delegation after settlement: `ML`'s primary run still
    goes through the same generic `catalog.get`/`Delegator.delegate` path as every other kind, but
    `execute_node`'s sequential loop also inspects a completed `ml-agent` settlement for
    `MLResult.needs_tuning_help` and delegates `ml-tuning-helper` when set (THY-19's helper tier is
    otherwise unreachable, since nothing lets an agent trigger a second delegation from inside its
    own turn).
    """

    DATA = "data"
    CODING = "coding"
    EXPERIMENT = "experiment"
    STATISTICS = "statistics-agent"
    DATA_QUALITY = "data-quality-agent"
    VISUALIZATION = "visualization-agent"
    ML = "ml-agent"


class ArtifactRequirement(ThymiraModel):
    """A workspace-relative artifact that a plan promises to publish before completion."""

    name: str = Field(min_length=1)
    kind: ArtifactKind = ArtifactKind.OTHER
    media_type: str | None = None

    @field_validator("name")
    @classmethod
    def _workspace_relative_name(cls, value: str) -> str:
        """Keep requirements logical and workspace-relative, independent of the host OS."""
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ":" in normalized or ".." in path.parts:
            raise ValueError("artifact requirement must be a workspace-relative path")
        return normalized


class AgentTask(ThymiraModel):
    """One unit of work ThyGraph hands to a specialised agent during a phase.

    `id` is a plan-local identifier (not a prefixed `Id`, and not globally unique): it exists so
    a sibling task's `depends_on` can name it, nothing more. `depends_on` references other
    `AgentTask.id`s in the *same* `ThyState.plan` (THY-12); a task named there that does not
    COMPLETE leaves this one SKIPPED rather than run.
    """

    id: str = Field(min_length=1)
    agent: ThyAgentKind
    phase: ThyPhase
    instruction: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    required_artifacts: tuple[ArtifactRequirement, ...] = ()


class PlanOutput(ThymiraModel):
    """THY's own structured planning output: an ordered, non-empty list of `AgentTask`s."""

    tasks: tuple[AgentTask, ...] = Field(min_length=1)


class SynthesisNarrative(ThymiraModel):
    """THY's own structured synthesis output: narrative only, never the deterministic facts.

    `best_model`/`metrics` are computed by `compare_experiments` (code), never chosen by the
    model — "the LLM proposes, code authorizes" applies to a recommendation exactly as it does to
    a policy decision. The model only ever supplies the tradeoffs/limitations framing.
    """

    tradeoffs: str = Field(min_length=1)
    limitations: str = Field(min_length=1)


class Recommendation(ThymiraModel):
    """THY's final output shape: the best model, its metrics, and the narrative around them."""

    best_model: str = Field(min_length=1)
    metrics: dict[str, float]
    tradeoffs: str = Field(min_length=1)
    limitations: str = Field(min_length=1)


class ReworkSignal(ThymiraModel):
    """A `policy.decision` asking THY to reopen an earlier analytical phase and re-run (THY-24).

    ADR-0004 dec.5: MIRA never speaks to THY directly -- it reaches THY only as a `policy.decision`
    the Policy Engine emitted. `decision` is that triggering decision, carried so the re-entry is
    genuinely policy-driven, never a finding acted on straight. `from_phase`/`target_phase` are the
    *analytical* phases (`thymira.core.phases.Phase` values: understanding/preparation/modeling/
    evaluation/reporting), named as plain strings because `thymira.thy` sits below `thymira.core`
    and may not import it; `thymira.thy.nodes.rework.can_reopen` validates `target_phase` against a
    reopenable set that mirrors `thymira.core.phases.REOPENABLE_PHASES` (a test locks the mirror).
    `justification` is required (`min_length=1`), so a reopen is *structurally* justified -- there
    is no unjustified `ReworkSignal` to construct.
    """

    decision: PolicyDecision
    from_phase: str = Field(min_length=1)
    target_phase: str = Field(min_length=1)
    justification: str = Field(min_length=1)


class RegisteredDataset(ThymiraModel):
    """A dataset Inspect registered into the Run, as the planner is told about it."""

    name: str = Field(min_length=1)
    target: str | None = None


class ThyProgress(ThymiraModel):
    """What a parked ThyGraph pass hands the Core so the next pass resumes instead of replanning.

    Carried verbatim: the gated plan, every outcome so far (`completed[i]` paired with
    `agent_messages[i]`, the PENDING one included), and the experiments and artifacts folded so
    far. The Core seeds it back into `ThyState` on the resumed pass, where Plan short-circuits
    on the seeded plan and Execute keeps every decided outcome and re-runs only the task that
    asked -- so a human's answer is re-offered to exactly the call that asked for it.
    """

    plan: tuple[AgentTask, ...] = Field(min_length=1)
    completed: tuple[AgentTask, ...] = ()
    agent_messages: tuple[Task, ...] = ()
    experiments: tuple[Experiment, ...] = ()
    artifacts: tuple[Artifact, ...] = ()

    @model_validator(mode="after")
    def _completed_pairs_with_agent_messages(self) -> ThyProgress:
        """Reject a `ThyProgress` whose `completed`/`agent_messages` cannot pair positionally.

        Execute's carried-outcomes prefix (`_carried_outcomes`, `thymira.thy.nodes.execute`) pairs
        `completed[i]` with `agent_messages[i]` by position, never by `AgentTask.id` -- a plan is
        LLM-authored and nothing enforces id uniqueness across it. This invariant has to hold
        before a `ThyProgress` ever crosses back into the Core to be seeded, not just at the
        moment Execute happens to build one.
        """
        if len(self.completed) != len(self.agent_messages):
            msg = "completed and agent_messages must pair positionally"
            raise ValueError(msg)
        return self


class ThyInput(ThymiraModel):
    """What starts a THY run: the Run it must execute."""

    run: Run
    execution_constraints: ExecutionConstraints | None = None


class ThyState(ThymiraModel):
    """ThyGraph's own bookkeeping as it moves through phases.

    Accumulates across phases rather than being overwritten, so a failure mid-run still leaves
    a readable trail of what each phase produced.

    Rework bookkeeping (THY-24): `rework` is the pending `policy.decision`-driven ask to reopen an
    earlier phase, `reopen_count`/`max_reopens` are the budget the rework loop enforces, and
    `rework_refusal` records why a reopen was denied (non-reopenable target, or budget exhausted)
    when the loop stops rather than looping. All four default to "no rework ever happens", so a run
    that is never handed a `ReworkSignal` behaves exactly as before THY-24.

    `datasets` names the project-declared datasets Inspect registered into the Run, each with its
    optional `target`, in declaration order, so Plan can tell the planner what already exists.
    """

    run: Run
    phase: ThyPhase = ThyPhase.INSPECT
    plan: tuple[AgentTask, ...] = ()
    completed: tuple[AgentTask, ...] = ()
    agent_messages: tuple[Task, ...] = ()
    usage: RunUsage = Field(default_factory=RunUsage)
    policy_signals: tuple[PolicyDecision, ...] = ()
    domain: str | None = None
    governance_frameworks: tuple[Framework, ...] = ()
    project_context: str | None = None
    datasets: tuple[RegisteredDataset, ...] = ()
    dataset_profile: dict[str, Any] | None = None
    experiments: tuple[Experiment, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    summary: str | None = None
    recommendation: Recommendation | None = None
    error: str | None = None
    rework: ReworkSignal | None = None
    reopen_count: int = 0
    max_reopens: int = DEFAULT_MAX_REOPENS
    rework_refusal: str | None = None
    single_rework: bool = False
    execution_constraints: ExecutionConstraints | None = None

    def advance(self) -> ThyState:
        """Return a copy moved to the next phase.

        Raises ``ValueError`` from ``SUMMARIZE``: summarize is the last phase, so ThyGraph
        must stop and produce a `ThyOutput` instead of advancing further.
        """
        index = THY_PHASE_ORDER.index(self.phase)
        if index + 1 >= len(THY_PHASE_ORDER):
            msg = f"ThyState is already at the last phase ({self.phase})"
            raise ValueError(msg)
        return self.model_copy(update={"phase": THY_PHASE_ORDER[index + 1]})

    def awaiting_approval(self) -> bool:
        """Whether a delegated task ended PENDING: a tool call of its is waiting for a human."""
        return any(message.status is TaskStatus.PENDING for message in self.agent_messages)


class ThyOutput(ThymiraModel):
    """What ThyGraph hands back to `thymira.core` when a run finishes, or fails."""

    run_id: Id
    summary: str | None = None
    recommendation: Recommendation | None = None
    experiment_ids: tuple[Id, ...] = ()
    artifact_ids: tuple[Id, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    missing_artifacts: tuple[str, ...] = ()
    error: str | None = None
    progress: ThyProgress | None = None
    """Set when the pass stopped on a task awaiting a human's answer (`ThyState.awaiting_approval`);
    the Core carries it into the next pass. `None` for a finished pass.
    """
    usage: dict[str, int | float | None] = Field(default_factory=dict)
    """What the run charged, as `{requests, tokens, cost_usd}`.

    A snapshot rather than the live `RunUsage`: it crosses into `thymira.core`, which folds it
    into the Run's `UsageLedger` so the Gate's `cost_so_far` is the real figure. `cost_usd` is
    `None` once any call could not be priced, which the ledger propagates rather than hiding.
    """
