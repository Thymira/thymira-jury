"""Shared lifecycle exceptions for private state collaborators."""

from __future__ import annotations


class LifecycleError(RuntimeError):
    """Base error raised by the lifecycle repository."""


class OwnerBusyError(LifecycleError):
    """Raised when another live process owns a Run."""


class StaleOwnerError(LifecycleError):
    """Raised when a writer has no live lock or carries an old fencing epoch."""


class PublicationError(LifecycleError):
    """Raised when a staged publication cannot be made visible."""


class IdempotencyConflictError(LifecycleError):
    """Raised when an idempotency key is reused for a different command."""


class LifecycleBackendUnavailableError(LifecycleError):
    """Raised when a requested lifecycle backend has no safe implementation."""


LifecycleBackendUnavailable = LifecycleBackendUnavailableError


__all__ = [
    "IdempotencyConflictError",
    "LifecycleBackendUnavailable",
    "LifecycleBackendUnavailableError",
    "LifecycleError",
    "OwnerBusyError",
    "PublicationError",
    "StaleOwnerError",
]
