"""Closed contracts for provider request capture and independent replay.

These records are deliberately provider-neutral. They describe the wire-shaped representation
that the runtime hands to its gateway, while keeping event ownership and revision history explicit.
The producer lives in ``thymira.agents``; MIRA may validate these records without importing it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from thymira.schemas.base import ThymiraModel
from thymira.schemas.enums import ReasoningStatus, ResponseOutcome
from thymira.schemas.ids import Id

REQUEST_LEDGER_VERSION = "0.1"
"""The supported payload version for provider request ledger records."""

RESPONSE_LEDGER_VERSION = "0.2"
"""The response payload version with terminal completeness evidence."""


class ReasoningMetadata(ThymiraModel):
    """Provider reasoning metadata that never contains the reasoning text itself."""

    status: ReasoningStatus = ReasoningStatus.ABSENT
    token_count: int = Field(default=0, ge=0)
    digest: str | None = None


class ProviderToolCall(ThymiraModel):
    """A provider-shaped tool call preserved inside an assistant message."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ProviderMessage(ThymiraModel):
    """One provider-shaped message in request or response order."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: tuple[ProviderToolCall, ...] = ()


class ProviderToolDefinition(ThymiraModel):
    """A provider-shaped callable tool schema."""

    name: str = Field(min_length=1)
    description: str | None = None
    parameters: dict[str, Any]


class RequestFactOwner(ThymiraModel):
    """The single durable owner reference for one model-visible input fact."""

    fact: Literal["model", "system", "settings", "tools", "output_schema", "messages"]
    owner_id: str = Field(min_length=1)


class RequestHeader(ThymiraModel):
    """Versioned model-visible configuration used to build a provider request."""

    ledger_version: Literal["0.1"] = REQUEST_LEDGER_VERSION
    id: Id
    revision: int = Field(ge=1)
    model: str = Field(min_length=1)
    # ``system`` remains for compatibility with the initial ledger payload.  New producers also
    # retain the role-bound sequence below; the joined text alone cannot represent interleaving or
    # duplicate system messages without changing what the gateway receives.
    system: str = ""
    system_messages: tuple[ProviderMessage, ...] = ()
    settings: dict[str, Any] = Field(default_factory=dict)
    tools: tuple[ProviderToolDefinition, ...] = ()
    output_schema: dict[str, Any] | None = None
    owners: tuple[RequestFactOwner, ...] = Field(min_length=6, max_length=6)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_owners(self) -> RequestHeader:
        """Require exactly one owner for every closed header fact and no duplicates."""
        facts = tuple(owner.fact for owner in self.owners)
        expected = {"model", "system", "settings", "tools", "output_schema", "messages"}
        if (
            len(set(facts)) != len(facts)
            or set(facts) != expected
            or any(not owner.owner_id.strip() for owner in self.owners)
            or not self.reason.strip()
        ):
            raise ValueError("header owners must contain each model input fact exactly once")
        return self


class SurfaceOperation(ThymiraModel):
    """An ordered append or replacement of the model-visible message surface."""

    ledger_version: Literal["0.1"] = REQUEST_LEDGER_VERSION
    operation: Literal["append", "replace"]
    messages: tuple[ProviderMessage, ...]
    owner_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_text(self) -> SurfaceOperation:
        """Refuse whitespace-only ownership and change reasons."""
        if not self.owner_id.strip() or not self.reason.strip():
            raise ValueError("surface operations require a non-empty owner and reason")
        return self


class ProviderGatewayRequest(ThymiraModel):
    """Canonical wire-shaped arguments observed immediately before gateway dispatch."""

    ledger_version: Literal["0.1"] = REQUEST_LEDGER_VERSION
    model: str = Field(min_length=1)
    messages: tuple[ProviderMessage, ...]
    tools: tuple[ProviderToolDefinition, ...] = ()
    settings: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None


class RuntimeSkillRequestBinding(ThymiraModel):
    """Selection evidence attached to the exact provider request that consumed it.

    The request id remains the enclosing :class:`ProviderRequest.id`.  This binding deliberately
    carries the immutable selection identity and the hash-chained selection event, so a fresh
    reader can associate one request with the selected runtime projection without treating a
    locally generated catalog token as a provider request id.
    """

    orchestrator: str = Field(min_length=1)
    selection_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_id: str = Field(min_length=1)
    projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_size_bytes: int = Field(ge=0)
    selection_event_id: Id
    selection_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderRequest(ThymiraModel):
    """One durable identity linking a gateway call to its owned input revisions."""

    ledger_version: Literal["0.1"] = REQUEST_LEDGER_VERSION
    id: Id
    attempt: int = Field(ge=1)
    retry_of: Id | None = None
    header_event_id: Id
    surface_event_ids: tuple[Id, ...] = Field(min_length=1)
    owners: tuple[RequestFactOwner, ...] = Field(min_length=6, max_length=6)
    gateway: ProviderGatewayRequest
    gateway_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_skill: RuntimeSkillRequestBinding | None = None

    @model_validator(mode="after")
    def validate_owners(self) -> ProviderRequest:
        """Require one owner for each gateway input fact before dispatch can be recorded."""
        facts = tuple(owner.fact for owner in self.owners)
        expected = {"model", "system", "settings", "tools", "output_schema", "messages"}
        if (
            len(set(facts)) != len(facts)
            or set(facts) != expected
            or any(not owner.owner_id.strip() for owner in self.owners)
        ):
            raise ValueError("request owners must contain each model input fact exactly once")
        return self


class ResponseTerminal(ThymiraModel):
    """Safe, request-correlated proof of a provider response's terminal outcome."""

    ledger_version: Literal["0.2"] = RESPONSE_LEDGER_VERSION
    outcome: ResponseOutcome
    chunk_count: int = Field(ge=0)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    error_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_outcome(self) -> ResponseTerminal:
        """Require complete success and safe, classified failure records."""
        if self.outcome is ResponseOutcome.SUCCESS:
            if self.chunk_count < 1 or self.error_code is not None or self.error_digest is not None:
                raise ValueError("successful response terminal must have content and no error")
        elif not self.error_code:
            raise ValueError("failed response terminal requires a safe error code")
        return self


class ResponseChunk(ThymiraModel):
    """One response chunk or typed terminal marker, ordered by provider source sequence."""

    ledger_version: Literal["0.2"] = RESPONSE_LEDGER_VERSION
    response_id: Id
    request_id: Id
    source_sequence: int = Field(ge=0)
    message: ProviderMessage | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reasoning: ReasoningMetadata = Field(default_factory=ReasoningMetadata)
    terminal: ResponseTerminal | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> ResponseChunk:
        """Require content and terminal markers to have unambiguous source semantics."""
        if self.message is None and self.terminal is None:
            raise ValueError("response record requires a message or terminal outcome")
        if self.message is None and self.terminal is not None:
            if self.source_sequence != self.terminal.chunk_count:
                raise ValueError("terminal-only response sequence must equal chunk count")
            if any(
                (
                    self.input_tokens,
                    self.output_tokens,
                    self.cached_input_tokens,
                    self.reasoning_output_tokens,
                )
            ):
                raise ValueError("terminal-only response cannot carry usage")
        elif self.terminal is None:
            if self.reasoning.status is not ReasoningStatus.ABSENT:
                raise ValueError("non-terminal response chunks cannot carry reasoning metadata")
        else:
            terminal = self.terminal
            if self.source_sequence != terminal.chunk_count - 1:
                raise ValueError("terminal message must be the final source chunk")
        return self


__all__ = [
    "REQUEST_LEDGER_VERSION",
    "RESPONSE_LEDGER_VERSION",
    "ProviderGatewayRequest",
    "ProviderMessage",
    "ProviderRequest",
    "ProviderToolCall",
    "ProviderToolDefinition",
    "ReasoningMetadata",
    "RequestFactOwner",
    "RequestHeader",
    "ResponseChunk",
    "ResponseTerminal",
    "RuntimeSkillRequestBinding",
    "SurfaceOperation",
]
