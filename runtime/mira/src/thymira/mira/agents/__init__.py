"""MIRA audit-agent declarations and the lazily loaded audit runner and verifier."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from thymira.mira.agents.loader import load_default_specs
from thymira.mira.agents.skills import load_mira_skill_catalog
from thymira.mira.agents.spec import AuditAgentSpec, load_specs

if TYPE_CHECKING:
    from thymira.mira.agents.dispatcher import dispatch_audit_agent
    from thymira.mira.agents.runner import (
        AuditAgentContext,
        EvidenceExcerpt,
        EvidenceProjectionLimits,
        EvidenceReader,
        build_audit_tools,
        run_audit_agent,
        runtime_skill_instructions,
        validate_audit_agent_context,
    )
    from thymira.mira.agents.verification import build_finding_verifier

_LAZY_EXPORTS = {
    "AuditAgentContext": ("thymira.mira.agents.runner", "AuditAgentContext"),
    "EvidenceExcerpt": ("thymira.mira.agents.runner", "EvidenceExcerpt"),
    "EvidenceProjectionLimits": ("thymira.mira.agents.runner", "EvidenceProjectionLimits"),
    "EvidenceReader": ("thymira.mira.agents.runner", "EvidenceReader"),
    "run_audit_agent": ("thymira.mira.agents.runner", "run_audit_agent"),
    "runtime_skill_instructions": (
        "thymira.mira.agents.runner",
        "runtime_skill_instructions",
    ),
    "build_finding_verifier": ("thymira.mira.agents.verification", "build_finding_verifier"),
    "build_audit_tools": ("thymira.mira.agents.runner", "build_audit_tools"),
    "dispatch_audit_agent": ("thymira.mira.agents.dispatcher", "dispatch_audit_agent"),
    "validate_audit_agent_context": (
        "thymira.mira.agents.runner",
        "validate_audit_agent_context",
    ),
}


def __getattr__(name: str) -> object:
    """Load runner/verifier dependencies only when a caller explicitly requests that API."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "AuditAgentContext",
    "AuditAgentSpec",
    "EvidenceExcerpt",
    "EvidenceProjectionLimits",
    "EvidenceReader",
    "build_audit_tools",
    "build_finding_verifier",
    "dispatch_audit_agent",
    "load_default_specs",
    "load_mira_skill_catalog",
    "load_specs",
    "run_audit_agent",
    "runtime_skill_instructions",
    "validate_audit_agent_context",
]
