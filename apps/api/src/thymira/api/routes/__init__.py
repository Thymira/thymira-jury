"""HTTP route groups assembled by the API application factory."""

from thymira.api.routes.artifacts import router as artifacts_router
from thymira.api.routes.audit import router as audit_router
from thymira.api.routes.events import router as events_router
from thymira.api.routes.governance import router as governance_router
from thymira.api.routes.mlflow import router as mlflow_router
from thymira.api.routes.project import router as project_router
from thymira.api.routes.runs import router as runs_router
from thymira.api.routes.settings import router as settings_router
from thymira.api.routes.tools import router as tools_router

__all__ = [
    "artifacts_router",
    "audit_router",
    "events_router",
    "governance_router",
    "mlflow_router",
    "project_router",
    "runs_router",
    "settings_router",
    "tools_router",
]
