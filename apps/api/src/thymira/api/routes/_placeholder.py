"""Temporary responses for route groups owned by later API roadmap tasks."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from thymira.api.schemas import ApiError


def not_implemented_response(feature: str) -> JSONResponse:
    """Return a stable response while a route's dedicated roadmap task is pending."""
    error = ApiError(
        code="not_implemented",
        message=f"{feature} is not implemented yet.",
    )
    return JSONResponse(status_code=501, content=error.model_dump(mode="json"))


__all__ = ["not_implemented_response"]
