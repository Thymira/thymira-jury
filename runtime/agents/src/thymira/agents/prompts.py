"""PromptBuilder: assembles a step's prompt from current_surface(events) (THY-05).

ADR-0006's consequence: "a prompt builder now has a defined input: current_surface(events)".
`PromptBuilder.build` reads only that derived projection for history, never the raw log, so a
shadowed event (one a `context.compacted` has folded away) simply never reaches the prompt —
compaction (THY-25) is a drop-in later because this is the one place history is read from.

Scope note — no separate `SystemPromptCatalog`. The roadmap text for this task names one, but
`AgentCatalog.system_prompt` (THY-01/THY-03) already resolves `spec.system_prompt_ref` to file
text, refusing at load time if the file is missing — the exact "role/agent prompts, from files,
never string literals" job a second catalog would only duplicate. `PromptBuilder` is built on
`AgentCatalog` directly instead.

Prefix stability (`AssembledPrompt.system` byte-identical across two builds of the same spec with
different tasks) falls out of keeping `system` and `user` as separate fields: `system` is a pure
function of `(spec.name, environment)`, never of `task` or `events`, so a provider that caches on
the system prefix (ADR-0004's fan-out cache sharing, THY-23) sees the same prefix on every call
for a given agent. The optional `PromptEnvironment` (harness basics 1, ADR-0013) opens the prompt
with a persona line and closes it with one "when to use" paragraph per tool; it is constant for
one agent within one Run — same model, workspace and tool guidance every step — so the prefix
stays byte-stable per Run and the cache still shares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents.context_recovery import (
    MODEL_OUTPUT_CHAR_LIMIT,
    ContextCheckpoint,
    prune_head_marker_tail,
    render_checkpoint,
)
from thymira.agents.prompt_framing import frame_untrusted
from thymira.agents.runtime_catalog import frame_untrusted_snapshot, runtime_skill_manifest_sha256
from thymira.agents.runtime_context import RUNTIME_CONTEXT_FORM
from thymira.events import current_surface, scrub_credentials
from thymira.schemas import EventType, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.agents.runtime_catalog import RuntimeSkillCatalog
    from thymira.agents.spec import AgentCatalog, AgentSpec
    from thymira.schemas import Event, Task


EVENT_HISTORY_LABEL = "event-history"
"""Frame label for the rendered event history.

The whole history is wrapped in **one** frame, not one frame per event: the boundary the model
needs is between "the run's history" and the direct task objective, and a single frame keeps the
wrapper a fixed cost instead of one that grows with the number of events. Token budgeting sizes
that fixed cost with :func:`thymira.agents.prompt_framing.frame_overhead`.
"""


class AssembledPrompt(ThymiraModel):
    """One step's prompt: a task-independent `system` prefix and a task-specific `user` tail."""

    system: str
    user: str
    surface_seqs: tuple[int, ...]
    runtime_skill_names: tuple[str, ...] = ()
    runtime_skill_manifest_sha256: str | None = None
    runtime_skill_orchestrator: str | None = None
    runtime_skill_selection_id: str | None = None
    runtime_skill_request_id: str | None = None
    runtime_skill_dispatch_id: str | None = None
    runtime_skill_projection_sha256: str | None = None
    runtime_skill_projection_size_bytes: int | None = None
    runtime_skill_frame_nonce: str | None = None

    def runtime_skill_event_payload(self) -> dict[str, object]:
        """Return append-only evidence for the runtime projection in this prompt."""
        if self.runtime_skill_projection_sha256 is None:
            return {}
        return {
            "runtime_skill_manifest_sha256": self.runtime_skill_manifest_sha256,
            "runtime_skill_orchestrator": self.runtime_skill_orchestrator,
            "runtime_skill_selection_id": self.runtime_skill_selection_id,
            "runtime_skill_request_id": self.runtime_skill_request_id,
            "runtime_skill_dispatch_id": self.runtime_skill_dispatch_id,
            "runtime_skill_projection_sha256": self.runtime_skill_projection_sha256,
            "runtime_skill_projection_size_bytes": self.runtime_skill_projection_size_bytes,
            "runtime_skill_frame_nonce": self.runtime_skill_frame_nonce,
        }


class PromptEnvironment(ThymiraModel):
    """The Run-stable facts that open the system prompt: who the agent is, on what, where."""

    agent_name: str
    model: str
    workspace: str
    tool_guidance: tuple[str, ...] = ()

    def persona(self) -> str:
        """The one-line identity every request starts with."""
        return (
            f"You are the {self.agent_name} agent of Thymira, powered by the {self.model} "
            f"model. Your working directory is {self.workspace}."
        )


class PromptBuilder:
    """Builds `AssembledPrompt`s for one agent from its catalog-resolved system prompt."""

    def __init__(
        self,
        catalog: AgentCatalog,
        runtime_skill_catalog: RuntimeSkillCatalog | None = None,
        *,
        runtime_skill_budget: int = 32_000,
    ) -> None:
        self._catalog = catalog
        self._runtime_skill_catalog = runtime_skill_catalog
        self._runtime_skill_budget = runtime_skill_budget

    def build(
        self,
        spec: AgentSpec,
        task: Task,
        events: Sequence[Event],
        *,
        environment: PromptEnvironment | None = None,
        checkpoint_max_chars: int | None = 240,
        selected_skill_names: Sequence[str] = (),
        runtime_skill_request_id: str | None = None,
        runtime_skill_dispatch_id: str | None = None,
    ) -> AssembledPrompt:
        """Assemble the prompt for `task`, reading history from `current_surface(events)` only.

        Only `agent.message` events contribute a rendered line to `user` (their `text` payload
        field); every other event kind in the surface still counts toward `surface_seqs` — it was
        read, even though this builder has nothing to render for it yet. Runtime-context snapshots
        (payload `form == RUNTIME_CONTEXT_FORM`, harness basics 1) supersede one another: only the
        highest-`seq` snapshot contributes its text, in its own position, so a step never sees a
        stale workspace/dataset/artifact picture; every other snapshot is dropped from `user` while
        still counting toward `surface_seqs`. Event-owned history is framed as untrusted data —
        one `EVENT_HISTORY_LABEL` frame around the whole rendered history, so the wrapper is a
        fixed cost a token budget can subtract — while the final task objective stays outside it,
        a direct user instruction. With no rendered history there is nothing to frame and `user`
        is the objective alone.

        With an `environment`, `system` opens with the persona line, then the catalog prompt, then
        (when present) the tool guidance, each separated by a blank line; without one, `system` is
        the catalog prompt alone. `system` stays a pure function of `(spec.name, environment)`, so
        the prefix is byte-stable across tasks within one Run. `checkpoint_max_chars` bounds the
        model projection of a durable source checkpoint; ``None`` retains every source section for
        a resumed conversation.
        """
        system = self._catalog.system_prompt(spec.name)
        selected = tuple(selected_skill_names)
        runtime_skill_manifest = None
        if self._runtime_skill_catalog is not None:
            system = f"{system}\n\n{self._runtime_skill_catalog.index_text()}"
            selection = self._runtime_skill_catalog.select(
                selected,
                max_bytes=self._runtime_skill_budget,
                request_id=runtime_skill_request_id,
                dispatch_id=runtime_skill_dispatch_id,
            )
            runtime_skill_manifest = self._runtime_skill_catalog.manifest()
            if selection.rendered:
                system = f"{system}\n\n{selection.rendered}"
        if environment is not None:
            sections = [environment.persona(), system, *environment.tool_guidance]
            system = "\n\n".join(section for section in sections if section)
        surface = current_surface(events)
        latest_snapshot_seq = max(
            (event.seq for event in surface if event.payload.get("form") == RUNTIME_CONTEXT_FORM),
            default=None,
        )
        history: list[str] = []
        for event in surface:
            if not event.payload.get("text") or (
                event.payload.get("form") == RUNTIME_CONTEXT_FORM
                and event.seq != latest_snapshot_seq
            ):
                continue
            text = prune_head_marker_tail(str(event.payload["text"]), MODEL_OUTPUT_CHAR_LIMIT).text
            if event.type is EventType.CONTEXT_COMPACTED:
                raw_checkpoint = event.payload.get("checkpoint")
                if isinstance(raw_checkpoint, dict):
                    try:
                        checkpoint = ContextCheckpoint.model_validate(raw_checkpoint)
                    except (TypeError, ValueError):
                        checkpoint = None
                    if checkpoint is not None:
                        # Keep prompt growth bounded while the durable event retains every source
                        # section and fact. The header makes the continuation explicit; the
                        # summary remains a safe model projection alongside it. The source payload
                        # is the recovery authority; this short model projection prevents a
                        # checkpoint from immediately overflowing the request it is recovering.
                        text = (
                            f"{render_checkpoint(checkpoint, max_chars=checkpoint_max_chars)}\n"
                            f"Compaction summary: {text}"
                        )
            if event.payload.get("form") == RUNTIME_CONTEXT_FORM:
                # A runtime-context snapshot may carry text authored by a previous session or an
                # external producer; frame it as untrusted, escaped and nonce-bound before it joins
                # the rest of the rendered history, which is itself framed as a whole below.
                text = frame_untrusted_snapshot(text, snapshot_id=f"seq-{event.seq}")
            history.append(text)
        framed = (
            (frame_untrusted("\n".join(history), label=EVENT_HISTORY_LABEL),) if history else ()
        )
        user = "\n".join((*framed, task.objective))
        # Scrub known credential values after the complete request is assembled. This covers
        # credentials that arrived through event history, task text, or environment/tool guidance
        # before the provider receives either prompt half.
        return AssembledPrompt(
            system=scrub_credentials(system),
            user=scrub_credentials(user),
            surface_seqs=tuple(event.seq for event in surface),
            runtime_skill_names=selected,
            runtime_skill_manifest_sha256=(
                runtime_skill_manifest_sha256(runtime_skill_manifest)
                if runtime_skill_manifest is not None
                else None
            ),
            runtime_skill_orchestrator=(
                runtime_skill_manifest.orchestrator if runtime_skill_manifest is not None else None
            ),
            runtime_skill_selection_id=(
                runtime_skill_manifest.selection_id if runtime_skill_manifest is not None else None
            ),
            runtime_skill_request_id=(
                runtime_skill_manifest.request_id if runtime_skill_manifest is not None else None
            ),
            runtime_skill_dispatch_id=(
                runtime_skill_manifest.dispatch_id if runtime_skill_manifest is not None else None
            ),
            runtime_skill_projection_sha256=(
                runtime_skill_manifest.selected_projection_sha256
                if runtime_skill_manifest is not None
                else None
            ),
            runtime_skill_projection_size_bytes=(
                runtime_skill_manifest.selected_projection_size_bytes
                if runtime_skill_manifest is not None
                else None
            ),
            runtime_skill_frame_nonce=(
                runtime_skill_manifest.selected_frame_nonce
                if runtime_skill_manifest is not None
                else None
            ),
        )


__all__ = ["EVENT_HISTORY_LABEL", "AssembledPrompt", "PromptBuilder", "PromptEnvironment"]
