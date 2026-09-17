"""Sandbox contracts, local/container execution backends, and runtime-owned selection."""

from thymira.tools.sandbox.base import (
    ResolvedExecutionSpec,
    Sandbox,
    SandboxRun,
    StagedInput,
    WorkspaceQuotaEvidence,
    validate_staged_inputs,
)
from thymira.tools.sandbox.container import ContainerSandbox
from thymira.tools.sandbox.environment import (
    BOOTSTRAP_ENVIRONMENT_NAMES,
    BOOTSTRAP_ENVIRONMENT_SUFFIXES,
    is_bootstrap_environment_name,
    is_credential_environment_name,
    scrub_environment,
)
from thymira.tools.sandbox.local import LocalSubprocessSandbox
from thymira.tools.sandbox.settings import SandboxConfigurationError, SandboxSettings, build_sandbox
from thymira.tools.sandbox.termination import ProcessObservation, TerminationEvidence

__all__ = [
    "BOOTSTRAP_ENVIRONMENT_NAMES",
    "BOOTSTRAP_ENVIRONMENT_SUFFIXES",
    "ContainerSandbox",
    "LocalSubprocessSandbox",
    "ProcessObservation",
    "ResolvedExecutionSpec",
    "Sandbox",
    "SandboxConfigurationError",
    "SandboxRun",
    "SandboxSettings",
    "StagedInput",
    "TerminationEvidence",
    "WorkspaceQuotaEvidence",
    "build_sandbox",
    "is_bootstrap_environment_name",
    "is_credential_environment_name",
    "scrub_environment",
    "validate_staged_inputs",
]
