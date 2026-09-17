"""FastAPI application factory for the Thymira runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from thymira.api.errors import install_error_handlers
from thymira.api.export_redaction import ExportRedactionMiddleware
from thymira.api.route_table import verify_route_permissions
from thymira.api.routes import (
    artifacts_router,
    audit_router,
    events_router,
    governance_router,
    mlflow_router,
    project_router,
    runs_router,
    settings_router,
    tools_router,
)
from thymira.api.telemetry import install_tracing
from thymira.observability import configure as configure_tracing
from thymira.observability import shutdown as shutdown_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opentelemetry.trace import TracerProvider

    from thymira.api.deps import RuntimeDeps


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Drain Langfuse on the way out, so a stopped server does not lose the end of a Run.

    The SDK batches: an observation is queued and the queue drains on a timer, so whatever is
    still in it when the process stops is lost. That tail is the part a reviewer most wants,
    because a Run that failed failed at its end. The SDK does register an `atexit` handler, but a
    container is stopped with a signal and killed if it does not exit in time, and `atexit` runs
    only on the orderly path — the lifespan is the one hook that is reached either way.
    """
    yield
    shutdown_tracing()


def create_app(
    deps: RuntimeDeps,
    *,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    """Create a configured FastAPI application from explicit runtime dependencies.

    Every route is verified to declare a :class:`~thymira.api.permissions.Permission` before the
    application is returned: a handler added without one fails the process here rather than
    serving an anonymous caller (F13.3).

    Two independent traces are installed. `install_tracing` gives this app its own request
    spans on its own `TracerProvider`, and `configure_tracing` builds the Langfuse client on a
    provider of its own — both no-ops unless configured, and neither touches the process-global
    OpenTelemetry provider (ADR-0012). Langfuse is drained again on shutdown, in `_lifespan`.
    """
    app = FastAPI(
        title="Thymira API",
        version="0.1.0",
        lifespan=_lifespan,
        # The four interactive-documentation routes FastAPI serves by default are Starlette
        # ``Route`` objects: they accept no dependencies, so they cannot carry a Permission and
        # cannot be authenticated in place. Serving them would disclose the whole surface to an
        # anonymous caller, so they are not served at all. ``app.openapi()`` still builds the
        # schema in process, which is what the contract tests read.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(ExportRedactionMiddleware)
    app.state.runtime_deps = deps
    install_error_handlers(app)

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        """Report that the API application is available."""
        return {"status": "ok"}

    # Route verification compares endpoint identity as well as path and method. This keeps a
    # mounted or later-added lookalike at ``/healthz`` from inheriting the reviewed public escape.
    app.state.public_liveness_endpoint = healthz

    app.include_router(runs_router)
    app.include_router(settings_router)
    app.include_router(events_router)
    app.include_router(audit_router)
    app.include_router(artifacts_router)
    app.include_router(project_router)
    app.include_router(mlflow_router)
    app.include_router(governance_router)
    app.include_router(tools_router)
    verify_route_permissions(app)
    install_tracing(app, tracer_provider=tracer_provider)
    configure_tracing()
    return app


__all__ = ["create_app"]
