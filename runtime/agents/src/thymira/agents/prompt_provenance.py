"""Request/prompt provenance: record what the model actually saw (THY-06).

`record_prompt` persists the exact rendered prompt (`AssembledPrompt.system` + `.user`) as a
content-addressed `ArtifactKind.LOG` artifact and appends `model.selected` enriched with
`{prompt_sha256, surface_seqs}` — the envelope, never the model's reasoning: only the text that
was actually sent goes in, after source credential values are removed. `routed_model` (THY-02)
calls this instead of its own plain `model.selected` append whenever both `assembled` and
`artifact_store` are given,
so every real model call a wired `AgentRunner` (THY-03) makes is covered without either module
inventing a second copy of the routing/event logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.events import scrub_credentials
from thymira.schemas import Artifact, ArtifactKind, EventType, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.prompts import AssembledPrompt
    from thymira.events import EventLog
    from thymira.schemas import Actor
    from thymira.state import ArtifactStore


@dataclass(frozen=True, slots=True)
class PromptProvenanceContext:
    """What `record_prompt` needs to persist an artifact and record its event."""

    artifact_store: ArtifactStore
    event_log: EventLog
    actor: Actor
    agent_id: str
    """The prefixed `agent_*` id `Artifact.produced_by` requires — `ctx.actor.id` (e.g. `"system"`)
    is not one, so this travels separately rather than being derived from the actor."""


def record_prompt(
    ctx: PromptProvenanceContext,
    assembled: AssembledPrompt,
    choice: ModelChoice,
    *,
    rendered_system: str | None = None,
    rendered_user: str | None = None,
    surface_seqs: Sequence[int] | None = None,
) -> Artifact:
    """Persist the exact prompt with source credentials scrubbed and append `model.selected`.

    Replaces the plain `model.selected` append `routed_model` would otherwise do for this call —
    callers never append both.
    """
    system = assembled.system if rendered_system is None else rendered_system
    user = assembled.user if rendered_user is None else rendered_user
    rendered = f"{system}\n\n{user}"
    artifact = ctx.artifact_store.save_text(
        f"prompts/{choice.role.value}-{choice.task}-{new_id('artifact')}.txt",
        scrub_credentials(rendered),
        produced_by=ctx.agent_id,
        kind=ArtifactKind.LOG,
        media_type="text/plain",
    )
    ctx.event_log.append(
        EventType.MODEL_SELECTED,
        ctx.actor,
        {
            **choice.event_payload(),
            "prompt_sha256": artifact.sha256,
            "surface_seqs": list(assembled.surface_seqs if surface_seqs is None else surface_seqs),
            **assembled.runtime_skill_event_payload(),
        },
        subject_id=artifact.id,
    )
    return artifact


__all__ = ["PromptProvenanceContext", "record_prompt"]
