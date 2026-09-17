"""Stable HTTP error construction and domain-error status mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from thymira.api.schemas import ApiError
from thymira.core import (
    InvalidRunStateError,
    InvalidRunTransitionError,
    RunNotAwaitingApprovalError,
    RunNotFoundError,
    RunNotResumableError,
    SessionNotFoundError,
    SessionProjectMismatchError,
    StaleRunVersionError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI


def problem(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Build one structured HTTP problem for the API boundary.

    ``headers`` carries response headers the status requires, such as ``WWW-Authenticate`` on a
    401; they are propagated onto the problem+json response by :func:`http_error_handler`.
    """
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
        headers=headers,
    )


def _problem_response(
    status_code: int,
    error: ApiError,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Serialize one API error with the problem media type."""
    return JSONResponse(
        status_code=status_code,
        content=error.model_dump(mode="json"),
        media_type="application/problem+json",
        headers=headers,
    )


def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Serialize FastAPI validation failures in the stable API envelope."""
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation handler received an unexpected exception")
    return _problem_response(
        422,
        ApiError(
            code="validation_error",
            message="Request validation failed.",
            details={"errors": exc.errors()},
        ),
    )


def http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Serialize explicit HTTP errors in the stable API envelope."""
    if not isinstance(exc, HTTPException):
        raise TypeError("HTTP error handler received an unexpected exception")
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        error = ApiError(
            code=detail["code"],
            message=str(detail.get("message", "Request failed.")),
            details=detail.get("details", {})
            if isinstance(detail.get("details", {}), dict)
            else {},
        )
    else:
        error = ApiError(code=f"http_{exc.status_code}", message=str(detail))
    return _problem_response(exc.status_code, error, headers=exc.headers)


def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Map a known runtime exception to its stable HTTP problem."""
    mappings: dict[type[Exception], tuple[int, str, str]] = {
        RunNotFoundError: (404, "run_not_found", "The requested Run was not found."),
        SessionNotFoundError: (404, "session_not_found", "The requested Session was not found."),
        SessionProjectMismatchError: (
            404,
            "session_not_found",
            "The requested Session was not found.",
        ),
        RunNotAwaitingApprovalError: (
            409,
            "approval_not_pending",
            "The Run has no pending approval.",
        ),
        RunNotResumableError: (
            409,
            "run_not_resumable",
            "The Run cannot be resumed.",
        ),
        InvalidRunStateError: (409, "invalid_run_state", "The Run state is invalid."),
        InvalidRunTransitionError: (
            409,
            "invalid_run_transition",
            "The requested Run transition is not allowed.",
        ),
        StaleRunVersionError: (
            409,
            "stale_run_version",
            "The Run has changed; retry with fresh state.",
        ),
    }
    for error_type, (status_code, code, message) in mappings.items():
        if isinstance(exc, error_type):
            return _problem_response(status_code, ApiError(code=code, message=message))
    raise TypeError(f"unmapped domain exception: {type(exc).__name__}")


def install_error_handlers(app: FastAPI) -> None:
    """Install the API and runtime exception handlers on a FastAPI application."""
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    for error_type in (
        RunNotFoundError,
        SessionNotFoundError,
        SessionProjectMismatchError,
        RunNotAwaitingApprovalError,
        RunNotResumableError,
        InvalidRunStateError,
        InvalidRunTransitionError,
        StaleRunVersionError,
    ):
        app.add_exception_handler(error_type, domain_error_handler)


__all__ = [
    "domain_error_handler",
    "http_error_handler",
    "install_error_handlers",
    "problem",
    "validation_error_handler",
]
