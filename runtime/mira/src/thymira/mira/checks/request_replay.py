"""Independent replay of the provider request ledger.

This reader intentionally repeats the projection algorithm from the event stream instead of
importing ``thymira.agents.request_ledger``. It is therefore useful as an oracle for detecting a
producer that recorded a self-consistent but different provider request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.events import canonical_json, sha256_text, verify_events
from thymira.schemas import (
    Event,
    EventType,
    ProviderGatewayRequest,
    ProviderMessage,
    ProviderRequest,
    ReasoningStatus,
    RequestHeader,
    ResponseChunk,
    ResponseOutcome,
    SurfaceOperation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEADER_DIGEST = "header_sha256"


class RequestReplayError(ValueError):
    """The durable request ledger cannot be reconstructed or verified."""


@dataclass(frozen=True, slots=True)
class ReplayedRequest:
    """One request reconstructed from its owned header and surface events."""

    request: ProviderRequest
    gateway: ProviderGatewayRequest
    event_seq: int


@dataclass(frozen=True, slots=True)
class ReplayedResponse:
    """One terminal response outcome and any message reconstructed in source order."""

    request_id: str
    message: ProviderMessage | None
    source_event_seqs: tuple[int, ...]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_output_tokens: int
    reasoning_status: ReasoningStatus
    outcome: ResponseOutcome
    terminal_event_seq: int


@dataclass(frozen=True, slots=True)
class RequestReplayReport:
    """All request and response observations proven by one event stream."""

    requests: tuple[ReplayedRequest, ...]
    responses: tuple[ReplayedResponse, ...]
    in_flight_request_ids: tuple[str, ...] = ()

    @property
    def in_flight(self) -> tuple[str, ...]:
        """Return requests without a durable terminal outcome."""
        return self.in_flight_request_ids


def _header(event: Event) -> RequestHeader:
    """Read and independently authenticate one header revision."""
    payload = dict(event.payload)
    digest = payload.pop(_HEADER_DIGEST, None)
    try:
        value = RequestHeader.model_validate(payload)
    except ValueError as exc:
        raise RequestReplayError(f"invalid request header at event {event.seq}") from exc
    expected = sha256_text(canonical_json(value.to_json_dict()))
    if digest != expected:
        raise RequestReplayError(f"request header digest mismatch at event {event.seq}")
    return value


def _operation(event: Event) -> SurfaceOperation:
    """Read one ordered surface operation and require its owner."""
    try:
        value = SurfaceOperation.model_validate(event.payload)
    except ValueError as exc:
        raise RequestReplayError(f"invalid request surface at event {event.seq}") from exc
    if not value.owner_id.strip():
        raise RequestReplayError(f"request surface at event {event.seq} has no owner")
    return value


def _request(event: Event) -> ProviderRequest:
    """Read one recorded provider request with all closed fields present."""
    try:
        return ProviderRequest.model_validate(event.payload)
    except ValueError as exc:
        raise RequestReplayError(f"invalid provider request at event {event.seq}") from exc


def _response(event: Event) -> ResponseChunk:
    """Read one recorded response chunk and keep its metadata opaque to this reader."""
    try:
        return ResponseChunk.model_validate(event.payload)
    except ValueError as exc:
        raise RequestReplayError(f"invalid provider response at event {event.seq}") from exc


def _reasoning_invalid(chunk: ResponseChunk) -> bool:
    """Reject impossible reasoning metadata while keeping redaction distinct from absence."""
    metadata = chunk.reasoning
    if metadata.status is ReasoningStatus.INVALID:
        return True
    if metadata.digest is not None and _DIGEST.fullmatch(metadata.digest) is None:
        return True
    if metadata.token_count != chunk.reasoning_output_tokens:
        return True
    if metadata.status is ReasoningStatus.PRESENT and metadata.digest is None:
        return True
    return metadata.status is ReasoningStatus.ABSENT and (
        metadata.digest is not None or metadata.token_count != 0
    )


def _same_header_facts(left: RequestHeader, right: RequestHeader) -> bool:
    """Compare headers without treating a human-readable revision reason as a model fact."""
    return left.model_dump(mode="json", exclude={"id", "revision", "reason"}) == right.model_dump(
        mode="json", exclude={"id", "revision", "reason"}
    )


def replay_request_ledger(  # noqa: PLR0912, PLR0915  # one pass preserves event ordering
    events: Sequence[Event],
) -> RequestReplayReport:
    """Replay requests and response chunks, comparing every request with its gateway projection.

    Raises:
        RequestReplayError: If the chain, owners, revisions, ordering or recorded gateway input is
            incomplete or inconsistent.
    """
    chain = verify_events(events)
    if not chain.valid:
        raise RequestReplayError(f"event chain is invalid: {chain.error}")
    header_events: dict[str, Event] = {}
    operations: dict[str, tuple[SurfaceOperation, Event]] = {}
    requests: list[ReplayedRequest] = []
    chunks: list[tuple[ResponseChunk, Event]] = []
    last_revision = 0
    previous_header: RequestHeader | None = None
    request_by_id: dict[str, ProviderRequest] = {}
    request_event_ids: dict[str, str] = {}
    for event in events:
        if event.type is EventType.MODEL_INPUT_HEADER_REVISED:
            value = _header(event)
            if value.revision != last_revision + 1:
                raise RequestReplayError(f"header revision is not monotonic at event {event.seq}")
            if previous_header is not None and _same_header_facts(previous_header, value):
                raise RequestReplayError(
                    f"header revision {value.revision} repeats unchanged input facts"
                )
            last_revision = value.revision
            previous_header = value
            header_events[str(event.event_id)] = event
        elif event.type is EventType.MODEL_INPUT_SURFACE_UPDATED:
            value = _operation(event)
            operations[str(event.event_id)] = (value, event)
        elif event.type is EventType.MODEL_REQUEST_RECORDED:
            value = _request(event)
            header_event = header_events.get(str(value.header_event_id))
            if (
                header_event is None
                or header_event.type is not EventType.MODEL_INPUT_HEADER_REVISED
            ):
                raise RequestReplayError(f"request {value.id} references no header owner")
            header = _header(header_event)
            if header_event.seq >= event.seq:
                raise RequestReplayError(f"request {value.id} references a future header")
            request_surface: list[ProviderMessage] = []
            referenced_operations: list[tuple[SurfaceOperation, Event]] = []
            for operation_id in value.surface_event_ids:
                operation_item = operations.get(str(operation_id))
                if operation_item is None:
                    raise RequestReplayError(f"request {value.id} references no surface owner")
                referenced_operations.append(operation_item)
            for operation, operation_event in sorted(
                referenced_operations, key=lambda item: item[1].seq
            ):
                if operation_event.seq >= event.seq:
                    raise RequestReplayError(f"request {value.id} references a future surface")
                if operation.operation == "append":
                    request_surface.extend(operation.messages)
                else:
                    request_surface = list(operation.messages)
            # Current producers put the complete ordered provider surface in the replacement
            # operation.  The fallback keeps the first ledger payload shape replayable, where
            # system messages were held separately and only non-system messages were surfaced.
            expected_messages = (
                tuple(request_surface)
                if header.system_messages
                else (
                    (ProviderMessage(role="system", content=header.system),)
                    if header.system
                    else ()
                )
                + tuple(request_surface)
            )
            expected = ProviderGatewayRequest(
                model=header.model,
                messages=expected_messages,
                tools=header.tools,
                settings=header.settings,
                output_schema=header.output_schema,
            )
            if value.owners != header.owners:
                raise RequestReplayError(f"request {value.id} changed input ownership")
            if value.gateway != expected:
                raise RequestReplayError(
                    f"request {value.id} diverges from reconstructed gateway input"
                )
            if value.gateway_sha256 != sha256_text(canonical_json(expected.to_json_dict())):
                raise RequestReplayError(f"request {value.id} has a gateway digest mismatch")
            requests.append(ReplayedRequest(value, expected, event.seq))
            request_id = str(value.id)
            if request_id in request_by_id:
                raise RequestReplayError(f"request {request_id} is recorded more than once")
            if value.retry_of is not None:
                retry_id = str(value.retry_of)
                if retry_id not in request_by_id:
                    raise RequestReplayError(
                        f"request {request_id} retries an unknown or future request"
                    )
            request_by_id[request_id] = value
            request_event_ids[request_id] = str(event.event_id)
        elif event.type is EventType.MODEL_RESPONSE_CHUNK:
            chunks.append((_response(event), event))
    known_requests = {str(item.request.id) for item in requests}
    response_groups: dict[str, list[tuple[ResponseChunk, Event]]] = {}
    response_ids: set[str] = set()
    for chunk, event in chunks:
        if str(chunk.request_id) not in known_requests:
            raise RequestReplayError(f"response {chunk.response_id} references no request")
        if str(chunk.response_id) in response_ids:
            raise RequestReplayError(f"response {chunk.response_id} is recorded more than once")
        response_ids.add(str(chunk.response_id))
        request = request_by_id[str(chunk.request_id)]
        if event.correlation_id != chunk.request_id:
            raise RequestReplayError(f"response {chunk.response_id} has a correlation mismatch")
        request_event_id = request_event_ids[str(request.id)]
        if event.causation_id != request_event_id:
            raise RequestReplayError(f"response {chunk.response_id} has a causation mismatch")
        response_groups.setdefault(str(chunk.request_id), []).append((chunk, event))
    responses: list[ReplayedResponse] = []
    in_flight: list[str] = []
    for request in requests:
        request_id = str(request.request.id)
        group = response_groups.get(request_id, [])
        terminal_items = [item for item in group if item[0].terminal is not None]
        content_items = [item for item in group if item[0].message is not None]
        if not terminal_items:
            in_flight.append(request_id)
            continue
        if len(terminal_items) != 1:
            raise RequestReplayError(f"response {request_id} has multiple terminal outcomes")
        terminal_chunk, terminal_event = terminal_items[0]
        terminal = terminal_chunk.terminal
        if terminal is None:  # defensive: filtered above
            raise RequestReplayError(f"response {request_id} has no terminal payload")
        ordered = sorted(content_items, key=lambda item: (item[0].source_sequence, item[1].seq))
        source_sequences = [item[0].source_sequence for item in ordered]
        expected_sequences = list(range(terminal.chunk_count))
        if source_sequences != expected_sequences:
            raise RequestReplayError(
                f"response {request_id} source sequence is incomplete: "
                f"expected {expected_sequences}, got {source_sequences}"
            )
        if ordered:
            last_content_seq = ordered[-1][1].seq
            if terminal_chunk.message is None and terminal_event.seq <= last_content_seq:
                raise RequestReplayError(
                    f"response {request_id} terminal precedes content completion"
                )
            if terminal_chunk.message is not None and terminal_event.seq != last_content_seq:
                raise RequestReplayError(f"response {request_id} terminal is not final content")
        responses.append(
            ReplayedResponse(
                request_id=request_id,
                message=(
                    ProviderMessage(
                        role="assistant",
                        content="".join(
                            item[0].message.content for item in ordered if item[0].message
                        ),
                        tool_calls=tuple(
                            call
                            for item in ordered
                            if item[0].message is not None
                            for call in item[0].message.tool_calls
                        ),
                    )
                    if ordered
                    else None
                ),
                source_event_seqs=tuple(item[1].seq for item in ordered),
                input_tokens=sum(item[0].input_tokens for item in ordered),
                output_tokens=sum(item[0].output_tokens for item in ordered),
                cached_input_tokens=sum(item[0].cached_input_tokens for item in ordered),
                reasoning_output_tokens=sum(item[0].reasoning_output_tokens for item in ordered),
                reasoning_status=(
                    ReasoningStatus.INVALID
                    if any(_reasoning_invalid(item[0]) for item in ordered)
                    else ordered[-1][0].reasoning.status
                    if ordered
                    else ReasoningStatus.ABSENT
                ),
                outcome=terminal.outcome,
                terminal_event_seq=terminal_event.seq,
            )
        )
    return RequestReplayReport(tuple(requests), tuple(responses), tuple(in_flight))


__all__ = [
    "ReplayedRequest",
    "ReplayedResponse",
    "RequestReplayError",
    "RequestReplayReport",
    "replay_request_ledger",
]
