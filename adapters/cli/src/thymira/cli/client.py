"""HTTP client for the Thymira API."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import typer

from thymira.cli.render import render_error

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
"""Reaching the API is either quick or it is not running; a slow connect is a real failure."""

DEFAULT_READ_TIMEOUT_SECONDS = 900.0
"""How long to wait for API responses and event pages. Override with `THYMIRA_API_TIMEOUT`."""

TIMEOUT_ENV_VAR = "THYMIRA_API_TIMEOUT"

API_TOKEN_ENV_VAR = "THYMIRA_API_TOKEN"  # noqa: S105 - a variable name, not a value.
"""Environment variable carrying the API bearer token.

Duplicated rather than imported: an adapter imports no runtime member (the
``cli imports no runtime member`` import-linter contract). The two constants are pinned equal by
``tests/thymira/test_cli_credential.py::test_cli_token_variable_matches_the_api_boundary``, the
same arrangement ``thymira.cli.env`` already uses for the bootstrap blocklist."""
_MAX_SSE_RECONNECTS = 1
_APPROVAL_REQUESTED_EVENT = "human.approval_requested"
_APPROVAL_RESOLVED_EVENT = "human.approval"
_RUN_TRANSITIONED_EVENT = "run.transitioned"
_WAIT_FOR_APPROVAL_COMMAND = "wait_for_approval"
_RUN_COMPLETED_EVENT = "run.completed"
_RUN_FAILED_EVENT = "run.failed"
_TURN_ENDED_EVENT = "turn.ended"
_TURN_END_REASONS = frozenset({"completed", "rejected", "failed", "cancelled", "paused", "unknown"})
_MAX_EVENT_REPAIRS = 3
_SHA256_HEX_LENGTH = 64
_HTTP_SERVER_ERROR = 500
_STREAM_GUARD_HEADER = "x-thymira-redacted-stream"
_STREAM_GUARD_VALUE = "event-projection-v1"


class ApiError(RuntimeError):
    """Base error raised when the Thymira API cannot satisfy a request."""


class ApiTransportError(ApiError):
    """Raised when the API transport cannot provide a usable response."""


class ApiDomainError(ApiError):
    """Raised when the API returned a structured domain outcome for a request."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Keep the server's stable domain code available to non-rendering callers."""
        self.code = code
        super().__init__(message)


class ApiConnectionError(ApiTransportError):
    """Raised when the CLI cannot connect to the Thymira API."""


class ApiTimeoutError(ApiTransportError):
    """Raised when the API did not answer before the client's deadline."""


class RunNotFoundError(ApiDomainError):
    """Raised when the requested run does not exist."""


class ApprovalNotPendingError(ApiDomainError):
    """Raised when a run has no pending human approval to resolve."""


class ApiResponseError(ApiTransportError):
    """Raised when the API returns an error or an invalid response."""


class MissingCredentialError(ApiError):
    """Raised when no API credential is available to send."""


class AuthenticationError(ApiError):
    """Raised when the API rejects the credential the CLI presented."""


class PermissionDeniedError(ApiError):
    """Raised when the credential is valid but not permitted to perform the action."""


@dataclass(frozen=True, slots=True)
class RunView:
    """Validated subset of a Run response needed by the CLI."""

    id: str
    status: str
    prompt: str
    created_at: str | None
    started_at: str | None
    agent_count: int
    artifact_count: int
    tool_count: int = 0
    experiment_count: int = 0
    phase: str | None = None
    final_decision: str | None = None
    publication_receipt: PublicationReceiptView | None = None


@dataclass(frozen=True, slots=True)
class PublicationReceiptView:
    """Wire fields acknowledging a committed Run publication."""

    publication_id: str
    run_id: str
    session_id: str
    work_ids: tuple[str, ...]
    committed_at: str


@dataclass(frozen=True, slots=True)
class EnqueueReceiptView:
    """Wire fields acknowledging one durably accepted control input."""

    input_id: str
    run_id: str
    accepted: bool
    duplicate: bool
    durable_at: str


type MutationAck = EnqueueReceiptView | RunView
"""What a lifecycle mutation acknowledges.

An owner-bound API answers with an :class:`EnqueueReceiptView`, whose correlated ``turn.ended``
event settles the mutation. An API that still applies the mutation inside the request answers with
the resulting Run record instead, and there is nothing left to observe.
"""


@dataclass(frozen=True, slots=True)
class RunPage:
    """A page of runs returned by the Thymira API."""

    items: tuple[RunView, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class EventView:
    """Validated event fields rendered by the CLI."""

    seq: int
    type: str
    actor: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class _FollowState:
    """Mutable cursor and pending events shared by one follow/reconnect sequence."""

    last_seq: int
    reconnects: int = 0
    repairs: int = 0
    history_read: bool = False
    finished: bool = False
    pending: dict[int, EventView] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventPage:
    """A page of events returned by the Thymira API."""

    items: tuple[EventView, ...]
    next_after_seq: int | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ControlView:
    """One MIRA deterministic control result rendered by the CLI."""

    control_id: str
    status: str
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class FindingView:
    """One audit finding rendered by the CLI."""

    id: str
    control_id: str
    title: str
    severity: str


@dataclass(frozen=True, slots=True)
class AuditView:
    """Validated audit report and decision, plus the raw report for ``--json``."""

    run_id: str
    status: str
    controls: tuple[ControlView, ...]
    findings: tuple[FindingView, ...]
    decision: str | None
    decision_reason: str | None
    report_json: str


@dataclass(frozen=True, slots=True)
class AssuranceView:
    """Validated bundle identity plus its raw JSON representation."""

    run_id: str
    bundle_sha256: str
    bundle_json: str


@dataclass(frozen=True, slots=True)
class ExperimentView:
    """Validated experiment fields rendered by the CLI."""

    id: str
    name: str
    status: str
    parameters: dict[str, object]
    metrics: dict[str, float]
    seed: int | None
    tracker_run_id: str | None
    model_artifact_id: str | None


@dataclass(frozen=True, slots=True)
class MlflowRunView:
    """One MLflow tracker run rendered by the CLI."""

    tracker_run_id: str
    experiment_name: str
    status: str
    params: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PendingApprovalView:
    """One still-unresolved human-approval request folded from a Run's event history."""

    decision_id: str
    rule_id: str | None
    reason: str | None
    summary: str
    cost_so_far: dict[str, object] | None
    tool_call: dict[str, object] | None = None
    """The tool the human is asked to approve and its arguments, or ``None`` for a run-level
    review. Carries only ``tool``/``arguments`` -- the ``tool_intent_sha256`` digest is not for
    humans."""


@dataclass(frozen=True, slots=True)
class PlanArtifactView:
    """Content-addressed rendered plan artifact returned by the read-only plan endpoint."""

    artifact_id: str
    run_id: str
    revision: int
    sha256: str
    storage_key: str
    advisory: bool
    content: str


@dataclass(frozen=True, slots=True)
class DurablePlanView:
    """The complete durable goal/plan board returned after receipt settlement."""

    run_id: str
    plan_id: str
    revision: int
    mode: str
    goal: str | None
    goal_status: str
    items: tuple[dict[str, object], ...]
    completions: tuple[dict[str, object], ...]
    artifact: PlanArtifactView | None
    review_feedback: tuple[str, ...]
    rounds: tuple[dict[str, object], ...]
    autonomous_rounds: int
    blocked_cause: str | None
    blocked_count: int
    repeat_state: dict[str, object]


@dataclass(frozen=True, slots=True)
class PendingRiskQuestionView:
    """One pending activity-profile question rendered by the CLI."""

    field: str
    question: str
    profile_version: int
    question_number: int


@dataclass(frozen=True, slots=True)
class RiskInterviewView:
    """The current Run and the one risk-interview action the caller may take."""

    run: RunView
    pending_question: PendingRiskQuestionView | None
    requires_human_review: bool


def _default_timeout() -> httpx.Timeout:
    """Quick to connect, patient for an answer; `THYMIRA_API_TIMEOUT` overrides the read wait."""
    raw = os.environ.get(TIMEOUT_ENV_VAR, "").strip()
    try:
        read = float(raw) if raw else DEFAULT_READ_TIMEOUT_SECONDS
    except ValueError:
        read = DEFAULT_READ_TIMEOUT_SECONDS
    return httpx.Timeout(read, connect=DEFAULT_CONNECT_TIMEOUT_SECONDS)


class EventFollower:
    """Observe durable event history and live SSE without owning Run state."""

    def __init__(self, owner: ApiClient) -> None:
        """Bind observation to the API client's transport and error mapping."""
        self._owner = owner

    def stream_events(
        self,
        run_id: str,
        *,
        since: int = -1,
        follow: bool = False,
        require_turn_end: bool = False,
        work_ids: tuple[str, ...] = (),
        input_id: str | None = None,
    ) -> Iterator[EventView]:
        """Yield buffered events or follow the API's Server-Sent Event stream.

        A follow starts the live stream before it reconciles the buffered history.  This ordering
        closes the race where a Run appends an event between a page request and the first stream
        connection.  The two sources are merged by their durable sequence number, so reconnects
        and overlapping pages are safe to replay.
        """
        if since < -1:
            raise ValueError("since must be greater than or equal to -1")
        if follow:
            yield from self._follow_events(
                run_id,
                since=since,
                require_turn_end=require_turn_end,
                work_ids=work_ids,
                input_id=input_id,
            )
            return
        yield from self._read_events(run_id, after_seq=since)

    def wait_for_terminal(self, run_id: str, *, since: int = -1) -> EventView:
        """Follow a Run until its durable turn or Run outcome is observed.

        The returned event is evidence read from the API event stream.  It is deliberately not
        inferred from an HTTP status or a Run creation response, because enqueueing can succeed
        while execution later fails or waits for a human decision.

        Raises:
            ApiResponseError: If the stream closes without a terminal durable event.
        """
        return self._wait_for_receipt(run_id, since=since, require_turn_end=False)

    def wait_for_publication(
        self,
        receipt: PublicationReceiptView,
        *,
        since: int = -1,
    ) -> EventView:
        """Observe the turn associated with a committed publication receipt."""
        return self._wait_for_receipt(
            receipt.run_id,
            since=since,
            work_ids=receipt.work_ids,
            require_turn_end=True,
        )

    def wait_for_enqueue(self, receipt: EnqueueReceiptView, *, since: int = -1) -> EventView:
        """Observe the turn associated with one durably accepted control input."""
        if not receipt.accepted:
            raise ApiDomainError(f"Control input '{receipt.input_id}' was not accepted.")
        return self._wait_for_receipt(
            receipt.run_id,
            since=since,
            input_id=receipt.input_id,
            require_turn_end=True,
        )

    def _wait_for_receipt(
        self,
        run_id: str,
        *,
        since: int,
        work_ids: tuple[str, ...] = (),
        input_id: str | None = None,
        require_turn_end: bool,
    ) -> EventView:
        """Follow until a strict turn-end record correlates to the initiating receipt."""
        terminal: EventView | None = None
        for event in self.stream_events(
            run_id,
            since=since,
            follow=True,
            require_turn_end=require_turn_end,
            work_ids=work_ids,
            input_id=input_id,
        ):
            if _is_terminal_event(
                event,
                run_id=run_id,
                work_ids=work_ids,
                input_id=input_id,
                require_turn_end=require_turn_end,
            ):
                terminal = event
                break
        if terminal is None:
            raise ApiResponseError("The event stream closed before the receipt's turn ended.")
        return terminal

    def read_events(self, run_id: str, *, after_seq: int = -1) -> Iterator[EventView]:
        """Yield the committed event history for a Run."""
        yield from self._read_events(run_id, after_seq=after_seq)

    def _read_events(
        self,
        run_id: str,
        *,
        after_seq: int,
    ) -> Iterator[EventView]:
        """Yield every buffered event page after one sequence cursor."""
        cursor = after_seq
        while True:
            previous_cursor = cursor
            request = self._owner._request  # noqa: SLF001  # Reuse owner transport mapping.
            payload = request(
                "GET",
                f"/runs/{run_id}/events",
                run_id=run_id,
                params={"after_seq": cursor},
            )
            page = _parse_event_page(payload, run_id=run_id)
            items = _deduplicate_events(page.items)
            for event in items:
                if event.seq <= cursor:
                    continue
                if event.seq != cursor + 1:
                    raise ApiResponseError("The event history has an unrepaired sequence gap.")
                yield event
                cursor = event.seq
            if items and cursor == previous_cursor:
                raise ApiResponseError(
                    "The Event page response made no cursor progress while returning events."
                )
            if not page.has_more:
                return
            if cursor == previous_cursor:
                raise ApiResponseError(
                    "The Event page response made no cursor progress while more events were "
                    "claimed."
                )
            next_cursor = page.next_after_seq
            if next_cursor is None or next_cursor != cursor:
                raise ApiResponseError("The Event page response has an invalid next cursor.")

    def _follow_events(
        self,
        run_id: str,
        *,
        since: int,
        require_turn_end: bool = False,
        work_ids: tuple[str, ...],
        input_id: str | None,
    ) -> Iterator[EventView]:
        """Yield a durable history/live merge and reconnect after an interrupted connection."""
        state = _FollowState(last_seq=since)
        while True:
            try:
                yield from self._follow_attempt(
                    run_id,
                    state,
                    require_turn_end=require_turn_end,
                    work_ids=work_ids,
                    input_id=input_id,
                )
            except (httpx.RequestError, ApiConnectionError) as error:
                if state.reconnects >= _MAX_SSE_RECONNECTS:
                    if isinstance(error, ApiConnectionError):
                        raise
                    message = f"Cannot connect to the Thymira API at {self._owner.base_url}."
                    raise ApiConnectionError(message) from error
                state.reconnects += 1
            else:
                return

    def _follow_attempt(
        self,
        run_id: str,
        state: _FollowState,
        *,
        require_turn_end: bool,
        work_ids: tuple[str, ...],
        input_id: str | None,
    ) -> Iterator[EventView]:
        """Consume one SSE connection, reconciling durable pages when the stream is guarded."""
        headers = {"Accept": "text/event-stream"}
        if state.reconnects:
            headers["Last-Event-ID"] = str(state.last_seq)
        stream = self._owner._client.stream  # noqa: SLF001  # Reuse owner live transport.
        with stream(
            "GET",
            f"/runs/{run_id}/events",
            params={"follow": "true", "since": state.last_seq},
            headers=headers,
        ) as response:
            raise_for_status = self._owner._raise_for_status  # noqa: SLF001  # Reuse owner mapping.
            raise_for_status(response, run_id=run_id)
            if not state.history_read and _supports_reconciliation(response.headers):
                yield from self._reconcile_history(
                    run_id,
                    state,
                    require_turn_end=require_turn_end,
                    work_ids=work_ids,
                    input_id=input_id,
                )
                if state.finished:
                    return
            state.history_read = True
            for event in _parse_sse_events(response.iter_lines()):
                yield from self._merge_stream_event(
                    run_id,
                    event,
                    state,
                    require_turn_end=require_turn_end,
                    work_ids=work_ids,
                    input_id=input_id,
                )
                if state.finished:
                    return
            if state.pending:
                raise ApiResponseError("The event stream closed with an unrepaired sequence gap.")

    def _reconcile_history(
        self,
        run_id: str,
        state: _FollowState,
        *,
        require_turn_end: bool,
        work_ids: tuple[str, ...],
        input_id: str | None,
    ) -> Iterator[EventView]:
        """Read the history already committed when a guarded live stream opens."""
        for buffered in self._read_events(
            run_id,
            after_seq=state.last_seq,
        ):
            state.pending[buffered.seq] = buffered
        yield from self._yield_ready_events(
            run_id,
            state,
            require_turn_end=require_turn_end,
            work_ids=work_ids,
            input_id=input_id,
        )

    def _merge_stream_event(
        self,
        run_id: str,
        event: EventView,
        state: _FollowState,
        *,
        require_turn_end: bool,
        work_ids: tuple[str, ...],
        input_id: str | None,
    ) -> Iterator[EventView]:
        """Merge one SSE event, repairing a cursor gap from the paginated history."""
        if event.seq <= state.last_seq:
            return
        state.pending[event.seq] = event
        if event.seq > state.last_seq + 1:
            state.repairs += 1
            if state.repairs > _MAX_EVENT_REPAIRS:
                raise ApiResponseError("The event stream has an unrepaired sequence gap.")
            for buffered in self._read_events(run_id, after_seq=state.last_seq):
                state.pending[buffered.seq] = buffered
        yield from self._yield_ready_events(
            run_id,
            state,
            require_turn_end=require_turn_end,
            work_ids=work_ids,
            input_id=input_id,
        )

    def _yield_ready_events(
        self,
        run_id: str,
        state: _FollowState,
        *,
        require_turn_end: bool,
        work_ids: tuple[str, ...],
        input_id: str | None,
    ) -> Iterator[EventView]:
        """Yield contiguous pending events and stop after a durable terminal event."""
        cursor = [state.last_seq]
        for event in _drain_events(state.pending, cursor):
            state.last_seq = cursor[0]
            yield event
            if _is_terminal_event(
                event,
                run_id=run_id,
                work_ids=work_ids,
                input_id=input_id,
                require_turn_end=require_turn_end,
            ):
                state.finished = True
                return


class ApiClient:
    """Small synchronous client for the run endpoints used by the CLI."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout: float | httpx.Timeout | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a client for ``base_url`` that presents ``token`` on every request.

        The token is a required argument, not an option: every API route but the liveness probe
        needs one, so a client built without it could only produce 401s.

        The timeout default splits the two phases: connecting is expected to be quick, so a server
        that is not running still fails fast and honestly, while waiting for an answer is generous
        because the API runs the Run inside the request. This also gives durable event pages and
        Run observation room to take longer; enqueue responses never imply that execution is
        complete.
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else _default_timeout(),
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._events = EventFollower(self)

    @classmethod
    def from_environment(cls) -> ApiClient:
        """Create a client using ``THYMIRA_API_URL`` and ``THYMIRA_API_TOKEN``.

        Raises:
            MissingCredentialError: ``THYMIRA_API_TOKEN`` is unset or blank. The message names the
                variable and where the server writes a token it minted; it never prints a value.
        """
        token = os.environ.get(API_TOKEN_ENV_VAR, "").strip()
        if not token:
            message = (
                f"No Thymira API credential: set ${API_TOKEN_ENV_VAR}. "
                "The server prints where it wrote one at start-up "
                "(<state-root>/api-token, by default <workspace>/.thymira/runtime/api-token)."
            )
            raise MissingCredentialError(message)
        return cls(os.environ.get("THYMIRA_API_URL", DEFAULT_API_URL), token=token)

    def close(self) -> None:
        """Release network resources owned by the client."""
        self._client.close()

    def create_run(self, prompt: str) -> RunView:
        """Create a Run and return its committed publication receipt alongside the Run view."""
        payload = self._request("POST", "/runs", json={"prompt": prompt})
        return _parse_create_run_response(payload)

    def get_run(self, run_id: str) -> RunView:
        """Retrieve a run through ``GET /runs/{id}``."""
        payload = self._request("GET", f"/runs/{run_id}", run_id=run_id)
        return _parse_run(payload)

    def resume_run(self, run_id: str) -> MutationAck:
        """Enqueue a resume control input through ``POST /runs/{id}/resume``."""
        payload = self._request("POST", f"/runs/{run_id}/resume", run_id=run_id)
        return parse_mutation_ack(payload)

    def cancel_run(
        self,
        run_id: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> MutationAck:
        """Enqueue cancellation through ``POST /runs/{id}/cancel``."""
        body: dict[str, str] = {}
        if actor is not None:
            body["actor"] = actor
        if reason is not None:
            body["reason"] = reason
        payload = self._request("POST", f"/runs/{run_id}/cancel", json=body or None, run_id=run_id)
        return parse_mutation_ack(payload)

    def get_risk_interview(self, run_id: str) -> RiskInterviewView:
        """Read the current pending activity-profile question for a Run."""
        payload = self._request("GET", f"/runs/{run_id}/risk-interview", run_id=run_id)
        return _parse_risk_interview(payload)

    def answer_risk_interview(self, run_id: str, answer: str) -> MutationAck:
        """Enqueue an answer for the Run's pending intake question."""
        payload = self._request(
            "POST",
            f"/runs/{run_id}/risk-interview",
            json={"answer": answer},
            run_id=run_id,
        )
        if (
            isinstance(payload, dict)
            and "accepted" not in payload
            and isinstance(payload.get("run"), dict)
            and _is_run_record(payload["run"])
        ):
            # The current API answers the interview itself (Run + profile + next question), so
            # the mutation is already settled; a receipt-answering API still takes the strict path.
            return _parse_run(payload["run"])
        return parse_mutation_ack(payload)

    def approve_run(
        self,
        run_id: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> MutationAck:
        """Enqueue approval of a pending human decision through the Thymira API."""
        return self._resolve_approval(run_id, approved=True, actor=actor, note=note)

    def reject_run(
        self,
        run_id: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> MutationAck:
        """Enqueue rejection of a pending human decision through the Thymira API."""
        return self._resolve_approval(run_id, approved=False, actor=actor, note=note)

    def approve_decision(
        self,
        run_id: str,
        decision_id: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> MutationAck:
        """Enqueue approval through the decision-scoped governance endpoint."""
        return self._resolve_decision(
            run_id,
            decision_id,
            approved=True,
            actor=actor,
            note=note,
        )

    def reject_decision(
        self,
        run_id: str,
        decision_id: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> MutationAck:
        """Enqueue rejection through the decision-scoped governance endpoint."""
        return self._resolve_decision(
            run_id,
            decision_id,
            approved=False,
            actor=actor,
            note=note,
        )

    def list_runs(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> RunPage:
        """List runs through ``GET /runs`` using the API cursor contract."""
        params: dict[str, str | int] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        payload = self._request("GET", "/runs", params=params or None)
        return _parse_run_page(payload)

    def get_trace_url(self, run_id: str) -> str | None:
        """Retrieve where this Run's trace can be read, through ``GET /runs/{id}/trace``.

        The URL names the Langfuse project, which only the runtime's API keys know, so a client
        cannot build it even though the trace id is derived from the run id.
        """
        payload = self._request("GET", f"/runs/{run_id}/trace", run_id=run_id)
        url = payload.get("trace_url") if isinstance(payload, dict) else None
        return url if isinstance(url, str) and url else None

    def get_audit(self, run_id: str) -> AuditView:
        """Run MIRA's deterministic audit checks through ``GET /runs/{id}/audit``."""
        payload = self._request("GET", f"/runs/{run_id}/audit", run_id=run_id)
        return _parse_audit(payload)

    def get_assurance(self, run_id: str) -> AssuranceView:
        """Retrieve the verified assurance bundle through ``GET /runs/{id}/assurance``."""
        payload = self._request("GET", f"/runs/{run_id}/assurance", run_id=run_id)
        return _parse_assurance(payload)

    def plan_run(self, run_id: str) -> MutationAck:
        """Enqueue deterministic plan generation through ``POST /runs/{id}/plan``."""
        payload = self._request("POST", f"/runs/{run_id}/plan", run_id=run_id)
        return parse_mutation_ack(payload)

    def get_durable_plan(self, run_id: str) -> DurablePlanView:
        """Read the latest persisted plan through ``GET /runs/{id}/plan``."""
        payload = self._request("GET", f"/runs/{run_id}/plan", run_id=run_id)
        return _parse_durable_plan(payload)

    def get_pending_approval(self, run_id: str) -> PendingApprovalView | None:
        """Fold the Run's event history into the decision it is parked on, if any.

        There is a decision-scoped ``GET /runs/{id}/approvals`` route (the HITL-01 seam), but it
        answers a different question -- every request still unresolved by that separate evidence
        path, not which one this Run's own lifecycle is waiting on. The Gate records every request
        as a ``human.approval_requested`` event and every resolution as a matching
        ``human.approval`` event, so the still-pending set is recovered from the event history the
        CLI already reads. When the Run's own ``wait_for_approval`` transition names the decision it
        parked on, that is the one shown -- a later request for a different call (a fan-out step
        deferring several tool calls at once, R2) must not steal the human's attention from the
        review the Run is actually waiting for. With no park record (or the parked decision already
        answered), the latest still-pending request is shown instead.
        """
        pending: dict[str, PendingApprovalView] = {}
        parked_decision_id: str | None = None
        for event in self._events.read_events(run_id):
            if event.type == _APPROVAL_REQUESTED_EVENT:
                view = _parse_pending_approval(event.payload)
                pending[view.decision_id] = view
            elif event.type == _APPROVAL_RESOLVED_EVENT:
                decision_id = event.payload.get("decision_id")
                if isinstance(decision_id, str):
                    pending.pop(decision_id, None)
            elif (
                event.type == _RUN_TRANSITIONED_EVENT
                and event.payload.get("command") == _WAIT_FOR_APPROVAL_COMMAND
            ):
                policy_decision = event.payload.get("policy_decision")
                decision_id = (
                    policy_decision.get("id") if isinstance(policy_decision, dict) else None
                )
                parked_decision_id = decision_id if isinstance(decision_id, str) else None
        if not pending:
            return None
        if parked_decision_id is not None and parked_decision_id in pending:
            return pending[parked_decision_id]
        return next(reversed(pending.values()))

    def list_experiments(self, run_id: str) -> tuple[ExperimentView, ...]:
        """List a run's experiments through ``GET /runs/{id}/experiments``."""
        payload = self._request("GET", f"/runs/{run_id}/experiments", run_id=run_id)
        return _parse_experiment_list(payload, run_id=run_id)

    def list_mlflow_runs(self, run_id: str) -> tuple[MlflowRunView, ...]:
        """List a run's MLflow tracker runs through ``GET /runs/{id}/mlflow``."""
        payload = self._request("GET", f"/runs/{run_id}/mlflow", run_id=run_id)
        return _parse_mlflow_run_list(payload, run_id=run_id)

    def stream_events(
        self,
        run_id: str,
        *,
        since: int = -1,
        follow: bool = False,
        require_turn_end: bool = False,
        work_ids: tuple[str, ...] = (),
        input_id: str | None = None,
    ) -> Iterator[EventView]:
        """Yield buffered events or follow the API's Server-Sent Event stream."""
        yield from self._events.stream_events(
            run_id,
            since=since,
            follow=follow,
            require_turn_end=require_turn_end,
            work_ids=work_ids,
            input_id=input_id,
        )

    def wait_for_terminal(self, run_id: str, *, since: int = -1) -> EventView:
        """Follow a Run until its durable turn or Run outcome is observed."""
        return self._events.wait_for_terminal(run_id, since=since)

    def wait_for_publication(
        self,
        receipt: PublicationReceiptView,
        *,
        since: int = -1,
    ) -> EventView:
        """Observe the turn associated with a committed publication receipt."""
        return self._events.wait_for_publication(receipt, since=since)

    def wait_for_enqueue(self, receipt: EnqueueReceiptView, *, since: int = -1) -> EventView:
        """Observe the turn associated with one durably accepted control input."""
        return self._events.wait_for_enqueue(receipt, since=since)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str] | None = None,
        run_id: str | None = None,
        approval_action: bool = False,
        params: Mapping[str, str | int] | None = None,
    ) -> object:
        """Send one API request and translate transport errors for the CLI."""
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.ConnectError as error:
            message = f"Cannot connect to the Thymira API at {self.base_url}."
            raise ApiConnectionError(message) from error
        except httpx.TimeoutException as error:
            message = (
                f"The Thymira API did not answer within {self._client.timeout.read} seconds; "
                "the request outcome is unknown."
            )
            raise ApiTimeoutError(message) from error
        except httpx.RequestError as error:
            message = f"Cannot connect to the Thymira API at {self.base_url}."
            raise ApiConnectionError(message) from error

        self._raise_for_status(response, run_id=run_id, approval_action=approval_action)
        try:
            payload: object = response.json()
        except ValueError as error:
            message = "The Thymira API returned an invalid JSON response."
            raise ApiResponseError(message) from error
        return payload

    def _resolve_approval(
        self,
        run_id: str,
        *,
        approved: bool,
        actor: str | None,
        note: str | None,
    ) -> MutationAck:
        """Submit one human approval decision through its canonical endpoint."""
        payload: dict[str, str] = {}
        if actor is not None:
            payload["actor"] = actor
        if note is not None:
            payload["note"] = note
        action = "approve" if approved else "reject"
        response = self._request(
            "POST",
            f"/runs/{run_id}/{action}",
            json=payload,
            run_id=run_id,
            approval_action=True,
        )
        return parse_mutation_ack(response)

    def _resolve_decision(
        self,
        run_id: str,
        decision_id: str,
        *,
        approved: bool,
        actor: str | None,
        note: str | None,
    ) -> MutationAck:
        """Submit one decision-scoped human response as a durable control input."""
        payload: dict[str, str] = {}
        if actor is not None:
            payload["actor"] = actor
        if note is not None:
            payload["note"] = note
        action = "approve" if approved else "reject"
        response = self._request(
            "POST",
            f"/runs/{run_id}/approvals/{decision_id}/{action}",
            json=payload,
            run_id=run_id,
            approval_action=True,
        )
        return parse_mutation_ack(response)

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        run_id: str | None = None,
        approval_action: bool = False,
    ) -> None:
        """Translate an unsuccessful HTTP response into a CLI-specific error."""
        if response.status_code == httpx.codes.UNAUTHORIZED:
            auth_message = f"The Thymira API rejected the credential in ${API_TOKEN_ENV_VAR}."
            raise AuthenticationError(auth_message)
        if response.status_code == httpx.codes.FORBIDDEN:
            auth_message = (
                f"The credential in ${API_TOKEN_ENV_VAR} is not permitted to perform this action."
            )
            raise PermissionDeniedError(auth_message)
        code: str | None = None
        message: str | None = None
        if response.is_error:
            code, message = _problem_fields(response)
        if (
            response.status_code == httpx.codes.NOT_FOUND
            and run_id is not None
            and code in (None, "run_not_found")
        ):
            raise RunNotFoundError(f"Run '{run_id}' was not found.")
        if code == "approval_not_pending" and approval_action:
            raise ApprovalNotPendingError(f"Run '{run_id}' is not awaiting a human approval.")
        if code is not None and response.status_code < _HTTP_SERVER_ERROR:
            raise ApiDomainError(message or f"The API rejected the request ({code}).", code=code)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            detail = f"The Thymira API returned HTTP {response.status_code}."
            if code and message:
                detail = f"{detail} {code}: {message}"
            raise ApiResponseError(detail) from error


def client_from_context(ctx: typer.Context) -> ApiClient:
    """Return an injected client or create one for the current CLI invocation.

    A missing credential is reported here, once, for every command: a clear line naming the
    variable and exit code 2, never a traceback.
    """
    if isinstance(ctx.obj, ApiClient):
        return ctx.obj
    try:
        client = ApiClient.from_environment()
    except MissingCredentialError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=2) from error
    ctx.obj = client
    ctx.call_on_close(client.close)
    return client


def _problem_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    """Extract stable code/message fields from an API problem without trusting its shape."""
    try:
        payload: object = response.json()
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    code = payload.get("code")
    message = payload.get("message")
    if isinstance(code, str) and isinstance(message, str):
        return code, message
    detail = payload.get("detail")
    if isinstance(detail, dict):
        nested_code = detail.get("code")
        nested_message = detail.get("message")
        if isinstance(nested_code, str) and isinstance(nested_message, str):
            return nested_code, nested_message
    return None, message if isinstance(message, str) else None


def _parse_run(payload: object) -> RunView:
    """Validate the response fields rendered by the CLI."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Run response.")
    run_id = _required_string(payload, "id")
    status = _required_string(payload, "status")
    prompt = _required_string(payload, "prompt")
    return RunView(
        id=run_id,
        status=status,
        prompt=prompt,
        created_at=_optional_string(payload, "created_at"),
        started_at=_optional_string(payload, "started_at"),
        agent_count=_sequence_length(payload, "agent_ids"),
        artifact_count=_sequence_length(payload, "artifact_ids"),
        tool_count=_sequence_length(payload, "tool_call_ids"),
        experiment_count=_sequence_length(payload, "experiment_ids"),
        phase=_optional_string(payload, "phase"),
        final_decision=_optional_string(payload, "final_decision"),
    )


def _is_run_record(payload: dict[str, object]) -> bool:
    """Return whether a body is a complete Run record rather than a lifecycle receipt.

    The two shapes are disjoint: a Run record always carries ``id``, ``status`` and ``prompt``,
    and neither receipt carries any of them. Anything that is not a complete Run record keeps the
    strict receipt validation, so a truncated or malformed receipt is still refused.
    """
    return all(isinstance(payload.get(field), str) for field in ("id", "status", "prompt"))


def _parse_create_run_response(payload: object) -> RunView:
    """Validate the committed Run plus publication receipt response envelope.

    An API that publishes the durable envelope must publish a valid receipt with it. An API that
    still answers with the bare Run record is accepted without one; the commands that need the
    receipt (``run --headless``) refuse the response themselves rather than inventing a receipt.
    """
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Run creation response.")
    if "run" not in payload and "receipt" not in payload and _is_run_record(payload):
        return _parse_run(payload)
    run = _parse_run(payload.get("run"))
    receipt = _parse_publication_receipt(payload.get("receipt"))
    if receipt.run_id != run.id:
        raise ApiResponseError("The publication receipt belongs to a different Run.")
    return RunView(
        id=run.id,
        status=run.status,
        prompt=run.prompt,
        created_at=run.created_at,
        started_at=run.started_at,
        agent_count=run.agent_count,
        artifact_count=run.artifact_count,
        tool_count=run.tool_count,
        experiment_count=run.experiment_count,
        phase=run.phase,
        final_decision=run.final_decision,
        publication_receipt=receipt,
    )


def _parse_publication_receipt(payload: object) -> PublicationReceiptView:
    """Validate the receipt proving the initial Run publication became durable."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Run creation response has an invalid 'receipt' field.")
    work_ids = payload.get("work_ids")
    if not isinstance(work_ids, list) or not all(
        isinstance(work_id, str) and work_id for work_id in work_ids
    ):
        raise ApiResponseError("The publication receipt has an invalid 'work_ids' field.")
    committed_at = payload.get("committed_at")
    if not isinstance(committed_at, str) or not committed_at:
        raise ApiResponseError("The publication receipt has an invalid 'committed_at' field.")
    return PublicationReceiptView(
        publication_id=_required_string(payload, "publication_id"),
        run_id=_required_string(payload, "run_id"),
        session_id=_required_string(payload, "session_id"),
        work_ids=tuple(work_ids),
        committed_at=committed_at,
    )


def parse_enqueue_receipt(payload: object) -> EnqueueReceiptView:
    """Validate a durable control-input acknowledgement returned by an API mutation."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid enqueue receipt.")
    accepted = payload.get("accepted")
    duplicate = payload.get("duplicate")
    durable_at = payload.get("durable_at")
    if not isinstance(accepted, bool) or not isinstance(duplicate, bool):
        raise ApiResponseError("The enqueue receipt has invalid acceptance fields.")
    if not isinstance(durable_at, str) or not durable_at:
        raise ApiResponseError("The enqueue receipt has an invalid 'durable_at' field.")
    return EnqueueReceiptView(
        input_id=_required_string(payload, "input_id"),
        run_id=_required_string(payload, "run_id"),
        accepted=accepted,
        duplicate=duplicate,
        durable_at=durable_at,
    )


def parse_mutation_ack(payload: object) -> MutationAck:
    """Validate what a lifecycle mutation acknowledged, whichever shape the API publishes."""
    if isinstance(payload, dict) and _is_run_record(payload):
        return _parse_run(payload)
    return parse_enqueue_receipt(payload)


def _parse_run_page(payload: object) -> RunPage:
    """Validate a paginated list response from the API."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Run page response.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiResponseError("The Run page response has an invalid 'items' field.")
    next_cursor = _optional_string(payload, "next_cursor")
    return RunPage(items=tuple(_parse_run(item) for item in items), next_cursor=next_cursor)


def _parse_event_page(payload: object, *, run_id: str) -> EventPage:
    """Validate one paginated event response from the API."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Event page response.")
    response_run_id = _event_required_string(payload, "run_id")
    if response_run_id != run_id:
        raise ApiResponseError("The Event page response belongs to a different Run.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiResponseError("The Event page response has an invalid 'items' field.")
    next_after_seq = _event_optional_sequence(payload, "next_after_seq")
    has_more = payload.get("has_more")
    if not isinstance(has_more, bool):
        raise ApiResponseError("The Event page response has an invalid 'has_more' field.")
    return EventPage(
        items=tuple(_parse_event(item) for item in items),
        next_after_seq=next_after_seq,
        has_more=has_more,
    )


def _parse_audit(payload: object) -> AuditView:
    """Validate an audit response and keep the raw report for ``--json``."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Audit response.")
    report = payload.get("report")
    if not isinstance(report, dict):
        raise ApiResponseError("The Audit response has an invalid 'report' field.")
    controls = report.get("controls")
    if not isinstance(controls, list):
        raise ApiResponseError("The Audit report has an invalid 'controls' field.")
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        raise ApiResponseError("The Audit report has an invalid 'findings' field.")

    decision_value: str | None = None
    decision_reason: str | None = None
    decision = payload.get("decision")
    if decision is not None:
        if not isinstance(decision, dict):
            raise ApiResponseError("The Audit response has an invalid 'decision' field.")
        decision_value = _required_string(decision, "decision")
        decision_reason = _required_string(decision, "reason")

    return AuditView(
        run_id=_required_string(report, "run_id"),
        status=_required_string(report, "status"),
        controls=tuple(_parse_control(item) for item in controls),
        findings=tuple(_parse_finding(item) for item in findings),
        decision=decision_value,
        decision_reason=decision_reason,
        report_json=json.dumps(report, indent=2),
    )


def _parse_assurance(payload: object) -> AssuranceView:
    """Validate the minimum bundle identity fields before exposing raw JSON to the CLI."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid assurance bundle.")
    run_id = _required_string(payload, "run_id")
    digest = _required_string(payload, "bundle_sha256")
    return AssuranceView(
        run_id=run_id,
        bundle_sha256=digest,
        bundle_json=json.dumps(payload, indent=2),
    )


def _parse_control(payload: object) -> ControlView:
    """Validate one control result from an audit report."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Audit report has an invalid control entry.")
    return ControlView(
        control_id=_required_string(payload, "control_id"),
        status=_required_string(payload, "status"),
        severity=_required_string(payload, "severity"),
        detail=_optional_string(payload, "detail") or "",
    )


def _parse_finding(payload: object) -> FindingView:
    """Validate one audit finding from an audit report."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Audit report has an invalid finding entry.")
    return FindingView(
        id=_required_string(payload, "id"),
        control_id=_required_string(payload, "control_id"),
        title=_required_string(payload, "title"),
        severity=_required_string(payload, "severity"),
    )


def _parse_experiment_list(payload: object, *, run_id: str) -> tuple[ExperimentView, ...]:
    """Validate the paginated experiment list response from the API."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Experiment list response.")
    response_run_id = _required_string(payload, "run_id")
    if response_run_id != run_id:
        raise ApiResponseError("The Experiment list response belongs to a different Run.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiResponseError("The Experiment list response has an invalid 'items' field.")
    return tuple(_parse_experiment(item) for item in items)


def _parse_experiment(payload: object) -> ExperimentView:
    """Validate one experiment's fields rendered by the CLI."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Experiment response.")
    return ExperimentView(
        id=_required_string(payload, "id"),
        name=_required_string(payload, "name"),
        status=_required_string(payload, "status"),
        parameters=_optional_object_dict(payload, "parameters"),
        metrics=_metrics_dict(payload),
        seed=_optional_int(payload, "seed"),
        tracker_run_id=_optional_string(payload, "tracker_run_id"),
        model_artifact_id=_optional_string(payload, "model_artifact_id"),
    )


def _optional_object_dict(payload: dict[object, object], key: str) -> dict[str, object]:
    """Read one optional string-keyed mapping, defaulting to empty."""
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ApiResponseError(f"The Experiment response has an invalid '{key}' field.")
    return value


def _metrics_dict(payload: dict[object, object]) -> dict[str, float]:
    """Read the experiment 'metrics' mapping as string keys to float values."""
    value = payload.get("metrics", {})
    if not isinstance(value, dict):
        raise ApiResponseError("The Experiment response has an invalid 'metrics' field.")
    metrics: dict[str, float] = {}
    for key, metric in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(metric, int | float)
            or isinstance(metric, bool)
        ):
            raise ApiResponseError("The Experiment response has an invalid 'metrics' field.")
        metrics[key] = float(metric)
    return metrics


def _optional_int(payload: dict[object, object], key: str) -> int | None:
    """Read one optional integer field."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiResponseError(f"The Experiment response has an invalid '{key}' field.")
    return value


def _parse_mlflow_run_list(payload: object, *, run_id: str) -> tuple[MlflowRunView, ...]:
    """Validate the MLflow tracker-run list response from the API."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid MLflow run list response.")
    response_run_id = _required_string(payload, "run_id")
    if response_run_id != run_id:
        raise ApiResponseError("The MLflow run list response belongs to a different Run.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiResponseError("The MLflow run list response has an invalid 'items' field.")
    return tuple(_parse_mlflow_run(item) for item in items)


def _parse_mlflow_run(payload: object) -> MlflowRunView:
    """Validate one MLflow tracker run's fields rendered by the CLI."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid MLflow run response.")
    return MlflowRunView(
        tracker_run_id=_required_string(payload, "tracker_run_id"),
        experiment_name=_optional_string(payload, "experiment_name") or "",
        status=_optional_string(payload, "status") or "",
        params=_mlflow_string_mapping(payload, "params"),
        metrics=_mlflow_float_mapping(payload, "metrics"),
    )


def _mlflow_string_mapping(payload: dict[object, object], key: str) -> dict[str, str]:
    """Read one optional string-keyed mapping of string values, defaulting to empty."""
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ApiResponseError(f"The MLflow run response has an invalid '{key}' field.")
    mapping: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str):
            raise ApiResponseError(f"The MLflow run response has an invalid '{key}' field.")
        mapping[name] = str(item)
    return mapping


def _mlflow_float_mapping(payload: dict[object, object], key: str) -> dict[str, float]:
    """Read one optional string-keyed mapping of numeric values, defaulting to empty."""
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ApiResponseError(f"The MLflow run response has an invalid '{key}' field.")
    mapping: dict[str, float] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, int | float) or isinstance(item, bool):
            raise ApiResponseError(f"The MLflow run response has an invalid '{key}' field.")
        mapping[name] = float(item)
    return mapping


def _parse_sse_events(lines: Iterable[str]) -> Iterator[EventView]:
    """Parse complete Server-Sent Event frames from an HTTP response line iterator."""
    event_id: str | None = None
    event_type: str | None = None
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if event_id is not None or event_type is not None or data_lines:
                yield _parse_sse_event(event_id, event_type, data_lines)
            event_id = None
            event_type = None
            data_lines = []
            continue
        if line.startswith("id:"):
            event_id = line.removeprefix("id:").strip()
        elif line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    if event_id is not None or event_type is not None or data_lines:
        yield _parse_sse_event(event_id, event_type, data_lines)


def _is_terminal_event(
    event: EventView,
    *,
    run_id: str | None = None,
    work_ids: tuple[str, ...] = (),
    input_id: str | None = None,
    require_turn_end: bool = False,
) -> bool:
    """Return whether an event durably closes the observed turn or Run."""
    if require_turn_end:
        return (
            event.type == _TURN_ENDED_EVENT
            and _turn_end_reason(
                event,
                run_id=run_id,
                work_ids=work_ids,
                input_id=input_id,
            )
            is not None
        )
    if event.type in {_RUN_COMPLETED_EVENT, _RUN_FAILED_EVENT}:
        return True
    if event.type == _RUN_TRANSITIONED_EVENT and event.payload.get("outcome") is not None:
        return True
    return (
        event.type == _TURN_ENDED_EVENT
        and _turn_end_reason(
            event,
            run_id=run_id,
            work_ids=work_ids,
            input_id=input_id,
        )
        is not None
    )


def is_terminal_event(
    event: EventView,
    *,
    run_id: str | None = None,
    work_ids: tuple[str, ...] = (),
    input_id: str | None = None,
    require_turn_end: bool = False,
) -> bool:
    """Return whether an observed event durably closes a turn or Run."""
    return _is_terminal_event(
        event,
        run_id=run_id,
        work_ids=work_ids,
        input_id=input_id,
        require_turn_end=require_turn_end,
    )


def _supports_reconciliation(headers: Mapping[str, str]) -> bool:
    """Return whether the stream advertises the redacted projection reconciliation contract."""
    return headers.get(_STREAM_GUARD_HEADER, "").casefold() == _STREAM_GUARD_VALUE


def _turn_end_reason(
    event: EventView,
    *,
    run_id: str | None = None,
    work_ids: tuple[str, ...] = (),
    input_id: str | None = None,
) -> str | None:
    """Read the agreed lifecycle turn-end payload, rejecting incomplete observations."""
    if event.type != _TURN_ENDED_EVENT:
        return None
    payload = event.payload
    turn_id = payload.get("turn_id")
    event_run_id = payload.get("run_id")
    claimed_input_ids = _string_list(payload.get("claimed_input_ids"))
    event_work_ids = _string_list(payload.get("work_ids"))
    step_count = payload.get("step_count")
    end_reason = payload.get("end_reason")
    cause = payload.get("cause")
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or not isinstance(event_run_id, str)
        or not event_run_id
        or claimed_input_ids is None
        or event_work_ids is None
        or not isinstance(step_count, int)
        or isinstance(step_count, bool)
        or step_count < 0
        or not isinstance(end_reason, str)
        or end_reason not in _TURN_END_REASONS
        or "cause" not in payload
        or (cause is not None and not _valid_failure_cause(cause))
        or (run_id is not None and event_run_id != run_id)
        or (work_ids and not set(work_ids).issubset(event_work_ids))
        or (input_id is not None and input_id not in claimed_input_ids)
    ):
        return None
    return end_reason


def _string_list(value: object) -> tuple[str, ...] | None:
    """Validate and type a JSON list whose values are non-empty strings."""
    if not isinstance(value, list):
        return None
    result = tuple(item for item in value if isinstance(item, str) and item)
    return result if len(result) == len(value) else None


def _valid_failure_cause(value: object) -> bool:
    """Validate the wire shape of a lifecycle failure cause without importing runtime models."""
    if not isinstance(value, dict):
        return False
    required = ("code", "phase", "exception_type", "message", "effects_may_have_occurred")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required[:-1]):
        return False
    effects = value.get("effects_may_have_occurred")
    if not isinstance(effects, bool):
        return False
    caused_by = value.get("caused_by_id")
    return caused_by is None or (isinstance(caused_by, str) and bool(caused_by))


def terminal_response(event: EventView) -> str:
    """Extract the user-facing response carried by a durable terminal event."""
    for key in ("final_response", "response", "output", "result", "answer"):
        value = event.payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for text_key in ("text", "content", "value"):
                text = value.get(text_key)
                if isinstance(text, str):
                    return text
            return json.dumps(value, sort_keys=True)
    return ""


def terminal_exit_code(event: EventView) -> int:
    """Map a durable terminal reason to a process status for headless callers."""
    reason = (_turn_end_reason(event) or event.type.removeprefix("run.")).casefold()
    if reason in {"completed", "complete", "success", "succeeded"}:
        return 0
    if reason in {"cancelled", "canceled", "interrupted", "sigint", "sigterm"}:
        return 130
    if reason in {"blocked", "denied", "rejected", "aborted"}:
        return 2
    if event.type == _RUN_COMPLETED_EVENT:
        return 0
    return 1


def _deduplicate_events(events: Iterable[EventView]) -> tuple[EventView, ...]:
    """Keep one deterministic event for each sequence and return events in sequence order."""
    by_sequence: dict[int, EventView] = {}
    for event in events:
        # Assignment deliberately lets the later observation replace a stale duplicate.  The
        # cursor itself always moves to the highest sequence delivered, and never backwards.
        by_sequence[event.seq] = event
    return tuple(by_sequence[seq] for seq in sorted(by_sequence))


def _drain_events(pending: dict[int, EventView], cursor: list[int]) -> Iterator[EventView]:
    """Yield the contiguous prefix in ``pending`` after the cursor, advancing it in place."""
    while cursor[0] + 1 in pending:
        next_seq = cursor[0] + 1
        event = pending.pop(next_seq)
        cursor[0] = next_seq
        yield event


def _parse_sse_event(
    event_id: str | None,
    event_type: str | None,
    data_lines: list[str],
) -> EventView:
    """Validate one completed Server-Sent Event frame."""
    if event_id is None or event_type is None or not data_lines:
        raise ApiResponseError("The Thymira API returned an incomplete Server-Sent Event.")
    try:
        payload: object = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as error:
        raise ApiResponseError("The Thymira API returned an invalid Server-Sent Event.") from error
    event = _parse_event(payload)
    if event_id != str(event.seq) or event_type != event.type:
        raise ApiResponseError("The Server-Sent Event envelope does not match its payload.")
    return event


def _parse_event(payload: object) -> EventView:
    """Validate the event fields displayed by the CLI."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid Event response.")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ApiResponseError("The Event response has an invalid 'seq' field.")
    event_type = _event_required_string(payload, "type")
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        raise ApiResponseError("The Event response has an invalid 'actor' field.")
    event_payload = payload.get("payload", {})
    if not isinstance(event_payload, dict):
        raise ApiResponseError("The Event response has an invalid 'payload' field.")
    return EventView(
        seq=seq,
        type=event_type,
        actor=_event_required_string(actor, "id"),
        payload=event_payload,
    )


def _parse_durable_plan(payload: object) -> DurablePlanView:
    """Validate the complete persisted goal/plan board returned by the API."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid durable plan response.")
    run_id = _required_string(payload, "run_id")
    revision = _nonnegative_int(payload, "revision")
    autonomous_rounds = _nonnegative_int(payload, "autonomous_rounds")
    blocked_count = _nonnegative_int(payload, "blocked_count")
    items = _object_list(payload, "items")
    completions = _object_list(payload, "completions")
    rounds = _object_list(payload, "rounds")
    feedback = payload.get("review_feedback")
    if not isinstance(feedback, list) or not all(isinstance(value, str) for value in feedback):
        raise ApiResponseError("The durable plan response has an invalid 'review_feedback' field.")
    repeat_state = payload.get("repeat_state")
    if not isinstance(repeat_state, dict):
        raise ApiResponseError("The durable plan response has an invalid 'repeat_state' field.")

    goal = payload.get("goal")
    blocked_cause = payload.get("blocked_cause")
    if (goal is not None and not isinstance(goal, str)) or (
        blocked_cause is not None and not isinstance(blocked_cause, str)
    ):
        raise ApiResponseError("The durable plan response has invalid optional text fields.")

    artifact_payload = payload.get("artifact")
    artifact = None
    if artifact_payload is not None:
        artifact = _parse_plan_artifact(artifact_payload, run_id=run_id)
    return DurablePlanView(
        run_id=run_id,
        plan_id=_required_string(payload, "plan_id"),
        revision=revision,
        mode=_required_string(payload, "mode"),
        goal=goal,
        goal_status=_required_string(payload, "goal_status"),
        items=items,
        completions=completions,
        artifact=artifact,
        review_feedback=tuple(feedback),
        rounds=rounds,
        autonomous_rounds=autonomous_rounds,
        blocked_cause=blocked_cause,
        blocked_count=blocked_count,
        repeat_state=repeat_state,
    )


def _parse_plan_artifact(payload: object, *, run_id: str) -> PlanArtifactView:
    """Validate a complete content-addressed plan artifact reference and its rendered bytes."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The durable plan response has an invalid 'artifact' field.")
    revision = _nonnegative_int(payload, "revision")
    sha256 = _required_string(payload, "sha256")
    if len(sha256) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in sha256
    ):
        raise ApiResponseError("The durable plan artifact has an invalid 'sha256' field.")
    advisory = payload.get("advisory")
    content = payload.get("content")
    if advisory is not True or not isinstance(content, str):
        raise ApiResponseError("The durable plan artifact has invalid content fields.")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != sha256:
        raise ApiResponseError("The durable plan artifact content does not match its digest.")
    artifact_run_id = _required_string(payload, "run_id")
    if artifact_run_id != run_id:
        raise ApiResponseError("The durable plan artifact belongs to a different Run.")
    return PlanArtifactView(
        artifact_id=_required_string(payload, "artifact_id"),
        run_id=artifact_run_id,
        revision=revision,
        sha256=sha256,
        storage_key=_required_string(payload, "storage_key"),
        advisory=advisory,
        content=content,
    )


def _object_list(payload: dict[object, object], key: str) -> tuple[dict[str, object], ...]:
    """Read a required list of JSON objects while keeping the client runtime-independent."""
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ApiResponseError(f"The durable plan response has an invalid '{key}' field.")
    return tuple(value)


def _nonnegative_int(payload: dict[object, object], key: str) -> int:
    """Read a required non-negative integer from a durable plan response."""
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ApiResponseError(f"The durable plan response has an invalid '{key}' field.")
    return value


def _parse_risk_interview(payload: object) -> RiskInterviewView:
    """Validate the pending-question response while keeping its domain profile server-side."""
    if not isinstance(payload, dict):
        raise ApiResponseError("The Thymira API returned an invalid risk-interview response.")
    run = _parse_run(payload.get("run"))
    profile = payload.get("profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
        raise ApiResponseError("The risk-interview response has an invalid 'profile' field.")
    requires_human_review = payload.get("requires_human_review")
    if not isinstance(requires_human_review, bool):
        raise ApiResponseError(
            "The risk-interview response has an invalid 'requires_human_review' field."
        )
    pending = payload.get("pending_question")
    if pending is None:
        return RiskInterviewView(run, None, requires_human_review)
    if not isinstance(pending, dict):
        message = "The risk-interview response has an invalid 'pending_question' field."
        raise ApiResponseError(message)
    profile_version = pending.get("profile_version")
    question_number = pending.get("question_number")
    if (
        not isinstance(profile_version, int)
        or isinstance(profile_version, bool)
        or profile_version < 1
        or not isinstance(question_number, int)
        or isinstance(question_number, bool)
        or question_number < 1
    ):
        raise ApiResponseError("The risk-interview response has invalid question numbering.")
    return RiskInterviewView(
        run,
        PendingRiskQuestionView(
            field=_required_string(pending, "field"),
            question=_required_string(pending, "question"),
            profile_version=profile_version,
            question_number=question_number,
        ),
        requires_human_review,
    )


def _parse_pending_approval(payload: Mapping[str, object]) -> PendingApprovalView:
    """Validate one ``human.approval_requested`` event payload."""
    decision_id = payload.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        raise ApiResponseError(
            "A human.approval_requested event has an invalid 'decision_id' field."
        )
    rule_id = payload.get("rule_id")
    reason = payload.get("reason")
    summary = payload.get("summary")
    cost_so_far = payload.get("cost_so_far")
    tool = payload.get("tool")
    tool_call: dict[str, object] | None = None
    if isinstance(tool, str):
        arguments = payload.get("arguments")
        tool_call = {"tool": tool, "arguments": arguments if isinstance(arguments, dict) else {}}
    return PendingApprovalView(
        decision_id=decision_id,
        rule_id=rule_id if isinstance(rule_id, str) else None,
        reason=reason if isinstance(reason, str) else None,
        summary=summary if isinstance(summary, str) else "",
        cost_so_far=cost_so_far if isinstance(cost_so_far, dict) else None,
        tool_call=tool_call,
    )


def _event_required_string(payload: dict[object, object], key: str) -> str:
    """Read one required non-empty string from an event payload."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ApiResponseError(f"The Event response has an invalid '{key}' field.")
    return value


def _event_optional_sequence(payload: dict[object, object], key: str) -> int | None:
    """Read one optional non-negative integer event sequence."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ApiResponseError(f"The Event page response has an invalid '{key}' field.")
    return value


def _required_string(payload: dict[object, object], key: str) -> str:
    """Read one required non-empty string from an API payload."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ApiResponseError(f"The Run response has an invalid '{key}' field.")
    return value


def _optional_string(payload: dict[object, object], key: str) -> str | None:
    """Read one optional string from an API payload."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiResponseError(f"The Run response has an invalid '{key}' field.")
    return value


def _sequence_length(payload: dict[object, object], key: str) -> int:
    """Return the length of an optional JSON array."""
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ApiResponseError(f"The Run response has an invalid '{key}' field.")
    return len(value)
