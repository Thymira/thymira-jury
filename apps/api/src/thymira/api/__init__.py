"""Thymira API (FastAPI): the only door to the runtime for every client.

Endpoints planned for the MVP: POST /runs, GET /runs/{id}, POST and GET /runs/{id}/plan,
POST /runs/{id}/resume,
POST /runs/{id}/approve, POST /runs/{id}/reject, GET /runs/{id}/events, GET /runs/{id}/audit,
GET /runs/{id}/assurance, GET /runs/{id}/experiments.

The Run boundary models and the MVP Run route handlers use shared domain records without
redefining them in the API layer.
"""

from thymira.api.app import create_app
from thymira.api.credential import (
    API_TOKEN_ENV_VAR,
    AUTHORITY_KEY_FILE_NAME,
    ApiCredentialError,
    ProcessCredential,
    resolve_process_authority_secret,
    resolve_process_credential,
)
from thymira.api.deps import RuntimeDeps, build_default_deps, get_runtime_deps
from thymira.api.errors import install_error_handlers, problem
from thymira.api.lifecycle import AuthorityProofBuilder
from thymira.api.permissions import (
    BearerTokenRegistry,
    Permission,
    Principal,
    PrincipalResolver,
    principal_for_role,
)
from thymira.api.principal import resolve_actor, resolve_principal
from thymira.api.schemas import (
    AnswerRiskInterviewRequest,
    ApiError,
    ArtifactContentResponse,
    ArtifactListResponse,
    CreateRunRequest,
    CreateRunResponse,
    DelegateRequest,
    DurablePlanResponse,
    EventPage,
    EventProjection,
    ExperimentListResponse,
    HumanDecisionRequest,
    PendingRiskQuestionResponse,
    PlanArtifactResponse,
    ProjectContextResponse,
    ProjectContextUpdate,
    ProjectDatasetListResponse,
    ProjectDatasetTargetUpdate,
    ProjectDatasetUploadResponse,
    ProjectDatasetView,
    ResumeRunRequest,
    RiskInterviewResponse,
    RunAuditResponse,
    RunPage,
    RunPlan,
    SettingsListResponse,
    SettingsPutRequest,
    ToolDescriptor,
    ToolListResponse,
)
from thymira.api.subscription import EventSubscription, PollingEventSubscription
from thymira.api.telemetry import TelemetryConfigurationError, install_tracing

__all__ = [
    "API_TOKEN_ENV_VAR",
    "AUTHORITY_KEY_FILE_NAME",
    "AnswerRiskInterviewRequest",
    "ApiCredentialError",
    "ApiError",
    "ArtifactContentResponse",
    "ArtifactListResponse",
    "AuthorityProofBuilder",
    "BearerTokenRegistry",
    "CreateRunRequest",
    "CreateRunResponse",
    "DelegateRequest",
    "DurablePlanResponse",
    "EventPage",
    "EventProjection",
    "EventSubscription",
    "ExperimentListResponse",
    "HumanDecisionRequest",
    "PendingRiskQuestionResponse",
    "Permission",
    "PlanArtifactResponse",
    "PollingEventSubscription",
    "Principal",
    "PrincipalResolver",
    "ProcessCredential",
    "ResumeRunRequest",
    "RiskInterviewResponse",
    "RunAuditResponse",
    "RunPage",
    "RunPlan",
    "RuntimeDeps",
    "SettingsListResponse",
    "SettingsPutRequest",
    "TelemetryConfigurationError",
    "ToolDescriptor",
    "ToolListResponse",
    "build_default_deps",
    "create_app",
    "get_runtime_deps",
    "install_error_handlers",
    "install_tracing",
    "principal_for_role",
    "problem",
    "resolve_actor",
    "resolve_principal",
    "resolve_process_authority_secret",
    "resolve_process_credential",
]
