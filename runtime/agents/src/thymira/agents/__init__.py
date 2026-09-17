"""Thymira agents: individual agents and the LLM provider contract they call.

Owner (MVP roadmap): P2 (THY / Agents) and P4 (MIRA / Governance). Landed: the LLM provider
contract (:mod:`thymira.agents.llm`), `AgentSpec`/`AgentCatalog` (:mod:`thymira.agents.spec`,
THY-01) — sub-agents declared as data, with prompt/schema references resolved at load time —
`routed_model` (:mod:`thymira.agents.model_binding`, THY-02), the PydanticAI `Model` that routes
every call (free-text or structured) through the router and `LLMProvider`, `AgentRunner`
(:mod:`thymira.agents.runner`, THY-03), the generic spec-driven execution loop every sub-agent is
driven through, `build_agent_tools` (:mod:`thymira.agents.tool_bridge`, THY-04), which exposes
only `spec.tool_allowlist` to a step as PydanticAI tools gated through the Tool Manager + Gate,
`PromptBuilder` (:mod:`thymira.agents.prompts`, THY-05), which assembles a step's prompt from
`current_surface(events)` with a task-independent, prefix-stable system prompt, `RunUsage`/
`UsageLimits` (:mod:`thymira.agents.usage`, THY-07), the shared budget every model and tool call
charges against, enforced at a hard ceiling, and `Delegator`/`DepthGuard`
(:mod:`thymira.agents.delegation`, THY-08), the only hub-and-spoke channel one agent reaches
another through, recording every outcome as `agent.message`.

THY-33: `AgentRunner`/`AgentContext`/`AgentResult` (:mod:`thymira.agents.runner`),
`build_agent_tools` (:mod:`thymira.agents.tool_bridge`), `Delegator`/`DepthGuard`/
`DelegationDepthExceededError` (:mod:`thymira.agents.delegation`, which itself instantiates
`AgentRunner`), and `PendingResume`/`ResumeLookup`/`ResumeStatus`/`load_pending_resume`
(:mod:`thymira.agents.resume` — a parked step's own conversation, spilled to the `ArtifactStore`
so a later call continues it instead of asking the model its objective again from nothing) are the
only symbols here that need the Tool Manager, so they load lazily through `__getattr__` (mirroring
:mod:`thymira.mira.agents`'s pattern) — a caller that only wants routing types or `AgentSpec` never
pulls in `thymira.tools`. Every other export stays eager.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from thymira.agents.context_recovery import (
    CHECKPOINT_SECTION_NAMES,
    MODEL_OUTPUT_CHAR_LIMIT,
    CheckpointFact,
    ContextCheckpoint,
    PrunedText,
    build_source_checkpoint,
    merge_checkpoints,
    prune_head_marker_tail,
    recover_checkpoint,
    render_checkpoint,
)
from thymira.agents.llm import (
    LiteLLMProvider,
    LLMCallError,
    LLMConfigurationError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
    ScriptedProvider,
    get_provider,
    resolve_proxy_endpoint,
)
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import FRAME_NONCE_BYTES, frame_untrusted
from thymira.agents.prompt_provenance import PromptProvenanceContext, record_prompt
from thymira.agents.prompts import AssembledPrompt, PromptBuilder, PromptEnvironment
from thymira.agents.request_ledger import (
    LedgerProvider,
    RequestLease,
    RequestLedger,
    RequestLedgerError,
    instrument_provider,
    provider_messages,
)
from thymira.agents.risk import RiskClassification, RiskFacts, classify_risk
from thymira.agents.route_policy import (
    ModelRouteDeniedError,
    ModelRoutePolicyUnavailableError,
    enforce_model_route,
    model_route_policy_payload,
    record_model_route_policy_snapshot,
)
from thymira.agents.runtime_catalog import (
    RuntimeSkillCatalog,
    RuntimeSkillChange,
    RuntimeSkillDeclaration,
    RuntimeSkillFile,
    RuntimeSkillManifest,
    RuntimeSkillManifestEntry,
    RuntimeSkillSelection,
    RuntimeSkillSelectionFile,
    frame_untrusted_snapshot,
    load_runtime_skill_catalog,
    runtime_skill_manifest_sha256,
    verify_runtime_skill_manifest,
)
from thymira.agents.runtime_context import RUNTIME_CONTEXT_FORM, runtime_context_text
from thymira.agents.spec import AgentCatalog, AgentSpec, load_agent_specs
from thymira.agents.usage import RunUsage, UsageLimitExceededError, UsageLimits

if TYPE_CHECKING:
    from thymira.agents.delegation import (
        DelegationBoundaryError,
        DelegationDepthExceededError,
        Delegator,
        DepthGuard,
    )
    from thymira.agents.resume import (
        ParkedExecutionIdentity,
        ParkedToolIdentity,
        PendingResume,
        ResumeLookup,
        ResumeStatus,
        abort_pending_resume,
        build_execution_identity,
        continuation_key,
        load_pending_resume,
    )
    from thymira.agents.runner import (
        AgentContext,
        AgentEndReason,
        AgentResult,
        AgentRunner,
        ResumeHistoryError,
    )
    from thymira.agents.settlement import (
        DIAGNOSTICS_LIMIT,
        DelegationIdentity,
        PendingDelegation,
        Settlement,
        delegation_key,
        has_open_parked_invocation,
        select_canonical,
        settlements_from_events,
    )
    from thymira.agents.tool_bridge import build_agent_tools

_LAZY_EXPORTS = {
    "AgentContext": ("thymira.agents.runner", "AgentContext"),
    "AgentEndReason": ("thymira.agents.runner", "AgentEndReason"),
    "AgentResult": ("thymira.agents.runner", "AgentResult"),
    "AgentRunner": ("thymira.agents.runner", "AgentRunner"),
    "ResumeHistoryError": ("thymira.agents.runner", "ResumeHistoryError"),
    "DelegationDepthExceededError": ("thymira.agents.delegation", "DelegationDepthExceededError"),
    "DelegationBoundaryError": ("thymira.agents.delegation", "DelegationBoundaryError"),
    "Delegator": ("thymira.agents.delegation", "Delegator"),
    "DepthGuard": ("thymira.agents.delegation", "DepthGuard"),
    "PendingResume": ("thymira.agents.resume", "PendingResume"),
    "ParkedExecutionIdentity": ("thymira.agents.resume", "ParkedExecutionIdentity"),
    "ParkedToolIdentity": ("thymira.agents.resume", "ParkedToolIdentity"),
    "ResumeLookup": ("thymira.agents.resume", "ResumeLookup"),
    "ResumeStatus": ("thymira.agents.resume", "ResumeStatus"),
    "build_execution_identity": ("thymira.agents.resume", "build_execution_identity"),
    "continuation_key": ("thymira.agents.resume", "continuation_key"),
    "abort_pending_resume": ("thymira.agents.resume", "abort_pending_resume"),
    "build_agent_tools": ("thymira.agents.tool_bridge", "build_agent_tools"),
    "load_pending_resume": ("thymira.agents.resume", "load_pending_resume"),
    "DIAGNOSTICS_LIMIT": ("thymira.agents.settlement", "DIAGNOSTICS_LIMIT"),
    "DelegationIdentity": ("thymira.agents.settlement", "DelegationIdentity"),
    "PendingDelegation": ("thymira.agents.settlement", "PendingDelegation"),
    "Settlement": ("thymira.agents.settlement", "Settlement"),
    "delegation_key": ("thymira.agents.settlement", "delegation_key"),
    "select_canonical": ("thymira.agents.settlement", "select_canonical"),
    "settlements_from_events": ("thymira.agents.settlement", "settlements_from_events"),
    "has_open_parked_invocation": ("thymira.agents.settlement", "has_open_parked_invocation"),
}


def __getattr__(name: str) -> object:
    """Load `AgentRunner`/`build_agent_tools`/delegation only when a caller requests them."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "CHECKPOINT_SECTION_NAMES",
    "DIAGNOSTICS_LIMIT",
    "FRAME_NONCE_BYTES",
    "MODEL_OUTPUT_CHAR_LIMIT",
    "RUNTIME_CONTEXT_FORM",
    "AgentCatalog",
    "AgentContext",
    "AgentEndReason",
    "AgentResult",
    "AgentRunner",
    "AgentSpec",
    "AssembledPrompt",
    "CheckpointFact",
    "ContextCheckpoint",
    "DelegationBoundaryError",
    "DelegationDepthExceededError",
    "DelegationIdentity",
    "Delegator",
    "DepthGuard",
    "LLMCallError",
    "LLMConfigurationError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "LLMToolCall",
    "LLMToolDefinition",
    "LedgerProvider",
    "LiteLLMProvider",
    "ModelRouteDeniedError",
    "ModelRoutePolicyUnavailableError",
    "ParkedExecutionIdentity",
    "ParkedToolIdentity",
    "PendingDelegation",
    "PendingResume",
    "PromptBuilder",
    "PromptEnvironment",
    "PromptProvenanceContext",
    "PrunedText",
    "RequestLease",
    "RequestLedger",
    "RequestLedgerError",
    "ResumeHistoryError",
    "ResumeLookup",
    "ResumeStatus",
    "RiskClassification",
    "RiskFacts",
    "RunUsage",
    "RuntimeSkillCatalog",
    "RuntimeSkillChange",
    "RuntimeSkillDeclaration",
    "RuntimeSkillFile",
    "RuntimeSkillManifest",
    "RuntimeSkillManifestEntry",
    "RuntimeSkillSelection",
    "RuntimeSkillSelectionFile",
    "ScriptedProvider",
    "Settlement",
    "UsageLimitExceededError",
    "UsageLimits",
    "abort_pending_resume",
    "build_agent_tools",
    "build_execution_identity",
    "build_source_checkpoint",
    "classify_risk",
    "continuation_key",
    "delegation_key",
    "enforce_model_route",
    "frame_untrusted",
    "frame_untrusted_snapshot",
    "get_provider",
    "has_open_parked_invocation",
    "instrument_provider",
    "load_agent_specs",
    "load_pending_resume",
    "load_runtime_skill_catalog",
    "merge_checkpoints",
    "model_route_policy_payload",
    "provider_messages",
    "prune_head_marker_tail",
    "record_model_route_policy_snapshot",
    "record_prompt",
    "recover_checkpoint",
    "render_checkpoint",
    "resolve_proxy_endpoint",
    "routed_model",
    "runtime_context_text",
    "runtime_skill_manifest_sha256",
    "select_canonical",
    "settlements_from_events",
    "verify_runtime_skill_manifest",
]
