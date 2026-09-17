"""Typed records for the single mutable user settings layer."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from thymira.schemas.base import ThymiraModel
from thymira.schemas.credentials import is_credential_environment_name

_ASCII_CONTROL_LIMIT = 32


class SecretReference(ThymiraModel):
    """A source reference for a secret value that is never stored in settings."""

    source: Literal["env", "file"]
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        """Reject whitespace and control characters from a source reference."""
        if value.strip() != value or any(ord(char) < _ASCII_CONTROL_LIMIT for char in value):
            raise ValueError(
                "secret reference name must not contain surrounding whitespace or controls"
            )
        return value


class SettingsSnapshot(ThymiraModel):
    """One namespace revision from the sole mutable user settings layer.

    ``values`` contains non-secret configuration only. Secret material is represented by an
    explicit :class:`SecretReference`, so a caller either supplies an environment/file source or
    receives a validation error rather than persisting a value and pretending it was redacted.
    """

    namespace: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    revision: int = Field(ge=0)
    values: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, SecretReference] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _json_values_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Require values to be JSON data before they can reach durable storage."""
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("settings values must be finite JSON data") from exc
        return value

    @model_validator(mode="after")
    def _secrets_are_references(self) -> SettingsSnapshot:
        """Ensure secret-shaped fields use references instead of raw material."""
        overlap = sorted(set(self.values).intersection(self.secret_refs))
        if overlap:
            raise ValueError(
                "secret fields must use source references and cannot also have values: "
                + ", ".join(overlap)
            )
        unsupported = sorted(_credential_field_paths(self.values))
        if unsupported:
            raise ValueError(
                "secret-shaped fields require an env/file source reference: "
                + ", ".join(unsupported)
            )
        return self


def _credential_field_paths(value: object, prefix: str = "") -> list[str]:
    """Return every nested mapping key classified as credential-shaped."""
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            path = f"{prefix}.{key}" if prefix else key
            if is_credential_environment_name(key):
                paths.append(path)
            paths.extend(_credential_field_paths(item, path))
        return paths
    if isinstance(value, list | tuple):
        paths = []
        for index, item in enumerate(value):
            paths.extend(_credential_field_paths(item, f"{prefix}[{index}]"))
        return paths
    return []


__all__ = ["SecretReference", "SettingsSnapshot"]
