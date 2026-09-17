"""Langfuse tracing at Thymira's own seams: one observation per boundary, redacted (ADR-0012).

The trace is a **non-authoritative mirror**. `events.jsonl` remains the record — hash-chained,
verifiable, local (ADR-0010) — while a trace may be sampled, dropped or turned off; nothing here
may ever change what a Run does. Two rules follow, and both are structural rather than
conventional:

*Nothing leaves the process un-redacted.* Every `input`, `output` and `metadata` value passes
through `thymira.events.redact_export` inside this module, at one choke point. Call sites hand
over primitives — a rendered prompt,
a response text, tool arguments — never a model's reasoning, so chain-of-thought has no path in.
The SDK's own `mask=` hook is registered with the same function as a second, non-authoritative
line of defence.

*Nothing here raises on its own account.* Every Langfuse and OpenTelemetry call is wrapped: a
backend that is down, slow or misconfigured degrades to a no-op handle and a DEBUG log, never to
a failed Run. Exceptions raised by the *body* of a helper propagate unchanged — a
`UsageLimitExceededError` out of a model call must still reach its caller — the observation is
merely closed first.

Tracing is off unless `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both present in the
environment (`LANGFUSE_TRACING_ENABLED=false` disables it even then). Disabled, `langfuse` is
never imported: the import is lazy, inside :func:`configure`, the same way `LiteLLMProvider`
loads LiteLLM. Everything else the SDK needs — `LANGFUSE_BASE_URL`,
`LANGFUSE_TRACING_ENVIRONMENT`, `LANGFUSE_RELEASE` — it reads from the environment itself, so
this module never restates a setting it does not have to. `LANGFUSE_SAMPLE_RATE` is the one
exception, and :func:`_sample_rate` says why.

Langfuse is given an **isolated** `TracerProvider`. `thymira.api.telemetry` builds its own
provider and deliberately never registers it globally; a bare `Langfuse()` would claim that free
global, and handing it the API's provider would fan Langfuse's spans into the API's OTLP exporter
as well. A private provider avoids both, and OpenTelemetry's *context* is still shared, which is
all that nesting needs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, Protocol

from thymira.events import redact, redact_export

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

__all__ = [
    "Handle",
    "agent",
    "bind_context",
    "configure",
    "evidence",
    "flush",
    "generation",
    "guardrail",
    "is_enabled",
    "phase",
    "reset",
    "run_trace",
    "score_run",
    "shutdown",
    "tool",
    "trace_url",
    "update_current",
    "update_current_generation",
]

_logger = logging.getLogger(__name__)

PUBLIC_KEY_ENV_VAR = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY_ENV_VAR = "LANGFUSE_SECRET_KEY"  # noqa: S105  # the variable name, not a secret
TRACING_ENABLED_ENV_VAR = "LANGFUSE_TRACING_ENABLED"
SAMPLE_RATE_ENV_VAR = "LANGFUSE_SAMPLE_RATE"

_ObservationType = Literal["span", "agent", "tool", "chain", "evaluator", "guardrail", "generation"]
_ScoreDataType = Literal["NUMERIC", "BOOLEAN", "CATEGORICAL"]


class Handle(Protocol):
    """What a helper yields: the one thing a call site may do with an open observation."""

    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        failed: bool = False,
        reason: str | None = None,
    ) -> None:
        """Record this observation's result. Redaction happens on the way through.

        `failed` marks the observation `ERROR` so a reviewer can filter for it in the trace list
        instead of having to open each Run and read the outputs; `reason` is its status message.
        """
        ...


class _NoopHandle:
    """The handle every helper yields when tracing is off or the SDK refused the call."""

    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        failed: bool = False,
        reason: str | None = None,
    ) -> None:
        """Discard the update."""


class _SpanHandle:
    """A handle backed by one live Langfuse observation."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def update(
        self,
        *,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
        failed: bool = False,
        reason: str | None = None,
    ) -> None:
        """Redact and forward the result to the SDK, never raising on the SDK's account."""
        fields: dict[str, Any] = {}
        if output is not None:
            fields["output"] = _redacted(output)
        if metadata is not None:
            fields["metadata"] = _redacted(dict(metadata))
        if failed:
            fields["level"] = "ERROR"
        if reason is not None:
            fields["status_message"] = redact(reason)
        if not fields:
            return
        try:
            self._span.update(**fields)
        except Exception:  # a mirror never fails the Run it observes
            _logger.debug("langfuse observation update failed", exc_info=True)


_NOOP: Handle = _NoopHandle()


class _State:
    """The configured client, or the absence of one. Replaced wholesale by `configure`."""

    __slots__ = ("client",)

    def __init__(self, client: Any | None = None) -> None:
        self.client = client


_state = _State()


def _redacted(value: Any) -> Any:
    """Apply the export redaction chain, degrading to a marker if it somehow fails.

    The canonical event log retains PII for reconstruction; Langfuse is an export-like mirror, so
    it accepts only JSON-shaped data and refuses anything that cannot be classified safely. An
    unsupported value becomes a marker rather than being coerced through ``repr``, which could
    carry personal data outside the classifier.
    """
    try:
        return redact_export(_scrubbable(value))
    except Exception:  # unredactable data is dropped, never sent through
        _logger.debug("redaction failed; the value was dropped", exc_info=True)
        return "[REDACTION-FAILED]"


_REDACTABLE = (str, bool, int, float)
"""Primitive values that can safely cross the trace boundary unchanged."""


def _scrubbable(value: Any) -> Any:
    """Keep only JSON-shaped values eligible for trace export redaction."""
    if value is None or isinstance(value, _REDACTABLE):
        result = value
    elif isinstance(value, bytes | bytearray | memoryview):
        raise TypeError("binary trace values are not JSON-shaped")
    elif isinstance(value, Mapping):
        result = {key: _scrubbable(item) for key, item in value.items()}
    elif isinstance(value, list):
        result = [_scrubbable(item) for item in value]
    elif isinstance(value, tuple):  # a NamedTuple cannot be rebuilt from an iterable
        result = tuple(_scrubbable(item) for item in value)
    elif isinstance(value, set | frozenset):
        raise TypeError("set trace values are not JSON-shaped")
    else:
        raise TypeError(f"unsupported trace value: {type(value).__name__}")
    return result


def _mask(*, data: Any, **_: Any) -> Any:
    """The SDK's `mask=` hook: the same redaction, applied again at attribute-creation time."""
    return _redacted(data)


def _keys_present() -> bool:
    """Both API keys set and tracing not hard-disabled — the whole enablement rule."""
    if os.environ.get(TRACING_ENABLED_ENV_VAR, "true").strip().lower() == "false":
        return False
    return bool(
        os.environ.get(PUBLIC_KEY_ENV_VAR, "").strip()
        and os.environ.get(SECRET_KEY_ENV_VAR, "").strip()
    )


def _sample_rate() -> float:
    """Read `LANGFUSE_SAMPLE_RATE`, degrading to "keep everything" rather than to nothing.

    The SDK reads this variable too, but it only ever reaches a sampler inside the provider the
    SDK builds for itself, and that build is skipped whenever a `tracer_provider` is supplied —
    which this module always does, for the reasons in the module docstring. Left to the SDK the
    setting is therefore silently dead: a bare `TracerProvider` samples everything. Applying it
    here is what makes the documented knob work.

    An unparseable or out-of-range value keeps every trace instead of raising. Sampling is an
    operator's cost control, and the failure that matters is the one where a typo quietly stops
    the telemetry a reviewer is relying on.
    """
    raw = os.environ.get(SAMPLE_RATE_ENV_VAR, "").strip()
    if not raw:
        return 1.0
    try:
        rate = float(raw)
    except ValueError:
        _logger.debug("%s=%r is not a number; keeping every trace", SAMPLE_RATE_ENV_VAR, raw)
        return 1.0
    if not 0.0 <= rate <= 1.0:
        _logger.debug("%s=%r is outside [0, 1]; keeping every trace", SAMPLE_RATE_ENV_VAR, raw)
        return 1.0
    return rate


def _isolated_provider() -> Any:
    """Build Langfuse's private `TracerProvider`, carrying the sampler the SDK would have set."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415  # only while enabled
    from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415  # same
        ParentBased,
        TraceIdRatioBased,
    )

    rate = _sample_rate()
    if rate >= 1.0:
        return TracerProvider()
    # `ParentBased` so a sampled Run keeps every observation below it: a half-recorded Run is
    # worse than none, and the root's decision is the only one that should be made by chance.
    return TracerProvider(sampler=ParentBased(TraceIdRatioBased(rate)))


def configure(*, client: Any | None = None) -> None:
    """Build the Langfuse client, or leave tracing off. Idempotent; replaces any previous state.

    Args:
        client: An already-built client. Tests inject one wired to an in-memory exporter; passing
            it skips both the environment gate and the import, so a test never depends on ambient
            credentials. Production passes nothing.

    The client's own `_tracing_enabled` flag is deliberately not consulted: the SDK leaves it
    `True` and merely swaps in a `NoOpTracer` when keys are missing, so reading it would report a
    disabled client as enabled.
    """
    reset()
    if client is not None:
        _state.client = client
        return
    if not _keys_present():
        return
    try:
        from langfuse import Langfuse  # noqa: PLC0415  # never imported while tracing is off

        _state.client = Langfuse(tracer_provider=_isolated_provider(), mask=_mask)
    except Exception:  # an unusable backend leaves tracing off, nothing more
        _logger.debug("langfuse could not be configured; tracing stays off", exc_info=True)
        _state.client = None


def reset() -> None:
    """Forget the configured client. Tests call this in teardown so no state leaks between them."""
    _state.client = None


def is_enabled() -> bool:
    """Whether a client is configured. Call sites guard expensive trace inputs with this."""
    return _state.client is not None


def flush() -> None:
    """Send everything buffered. Never on a request path — it blocks until the queue drains."""
    client = _state.client
    if client is None:
        return
    try:
        client.flush()
    except Exception:  # a failed flush loses telemetry, not work
        _logger.debug("langfuse flush failed", exc_info=True)


def shutdown() -> None:
    """Flush and stop the SDK's background threads. The SDK also does this from `atexit`."""
    client = _state.client
    if client is None:
        return
    try:
        client.shutdown()
    except Exception:  # shutdown is best effort by definition
        _logger.debug("langfuse shutdown failed", exc_info=True)


def _mark_failed(entered: Any, exc: BaseException, what: str) -> None:
    """Record *that* an observation failed, without letting the exception's text leave the process.

    Closing an observation with the live exception is what OpenTelemetry expects, and it is the
    one thing this module must not do. `use_span` defaults to `record_exception=True` and
    `set_status_on_exception=True` (`opentelemetry/trace/__init__.py`), the SDK never overrides
    them — `record_exception` appears nowhere in `langfuse` — and the resulting `str(exc)` and
    full stacktrace are attached as span *events*, with `f"{type(exc).__name__}: {exc}"` as the
    status message. Neither is redacted by anything: this module's `_redacted` and the SDK's
    `mask=` hook both reach `input`, `output` and `metadata` only, and the exporter copies
    `events=` and `status=` verbatim. A pydantic `ValidationError` out of a structured-output
    parse would be enough on its own to ship the model's raw answer.

    Only the exception's class name is safe to send, so that is all that is sent, and the caller
    then closes the observation as if it had returned.
    """
    if entered is None:
        return
    update = getattr(entered, "update", None)
    if update is None:  # `propagate_attributes` yields no observation to mark
        return
    try:
        update(level="ERROR", status_message=type(exc).__name__)
    except Exception:  # a mirror never fails the Run it observes
        _logger.debug("langfuse %s could not be marked failed", what, exc_info=True)


@contextmanager
def _quiet(factory: Callable[[], AbstractContextManager[Any]], what: str) -> Iterator[Any]:
    """Enter a context manager that is allowed to fail, yielding None when it does.

    The asymmetry is the point: anything the *telemetry* raises — building the context manager,
    entering it, leaving it — is logged and swallowed, while anything the *body* raises passes
    straight through, with the context manager closed first.

    A body that raises still closes its observation *normally*, after :func:`_mark_failed` has
    recorded the failure in redacted form. Handing the exception to `__exit__` would be the
    natural thing to do and would leak its text; see that function.
    """
    manager: AbstractContextManager[Any] | None = None
    entered: Any = None
    try:
        manager = factory()
        entered = manager.__enter__()
    except Exception:  # see the docstring: telemetry never fails the caller
        _logger.debug("langfuse %s could not be opened", what, exc_info=True)
        manager = None
        entered = None
    try:
        yield entered
    except BaseException as exc:
        if manager is not None:
            _mark_failed(entered, exc, what)
            try:
                manager.__exit__(None, None, None)
            except Exception:  # must not mask the body's own exception
                _logger.debug("langfuse %s could not be closed", what, exc_info=True)
        raise
    if manager is not None:
        try:
            manager.__exit__(None, None, None)
        except Exception:  # a lost observation is not a failed Run
            _logger.debug("langfuse %s could not be closed", what, exc_info=True)


@contextmanager
def _observe(
    as_type: _ObservationType,
    name: str,
    *,
    input_: Any = None,
    metadata: Mapping[str, Any] | None = None,
    trace_context: Mapping[str, str] | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, Any] | None = None,
) -> Iterator[Handle]:
    """Open one observation of `as_type`, or yield the no-op handle. The single SDK choke point."""
    client = _state.client
    if client is None:
        yield _NOOP
        return
    fields: dict[str, Any] = {"as_type": as_type, "name": name}
    if input_ is not None:
        fields["input"] = _redacted(input_)
    if metadata is not None:
        fields["metadata"] = _redacted(dict(metadata))
    if trace_context is not None:
        fields["trace_context"] = dict(trace_context)
    if model is not None:
        fields["model"] = model
    if model_parameters is not None:
        fields["model_parameters"] = _redacted(dict(model_parameters))
    with _quiet(lambda: client.start_as_current_observation(**fields), name) as span:
        yield _NOOP if span is None else _SpanHandle(span)


@contextmanager
def run_trace(
    *,
    run_id: str,
    session_id: str,
    project_id: str,
    prompt: str,
    resumed: bool = False,
    version: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[Handle]:
    """The root: one Run is one trace, holding everything that Run did.

    The trace id is derived from `run_id`, so the same Run always maps to the same trace and the
    root is detached from whatever request span happens to be ambient. `session_id` groups the
    Runs of one Session, and the project is a tag. `user_id` is deliberately not set: the only
    identity Thymira has is `Actor.id`, which in header mode is a caller-supplied string that may
    be an email — Langfuse offers no guidance for pseudonymising it, so it stays out.

    A Run parked for human review is executed twice, so its trace holds two roots. The second is
    named `run-resumed` rather than `run`, because the agent-graph view groups nodes by name and
    two roots called `run` render as one node that ran twice — which is not what happened. The
    resumed episode also does not repeat the prompt as its input: it continues from a checkpoint,
    it does not re-read the request.

    `version` and `metadata` are propagated rather than set on the root alone, because Langfuse
    only counts an observation in an aggregation over an attribute if that observation carries it:
    a cost-by-version figure computed from a root that holds the version and generations that do
    not would be zero. `version` is the workspace commit the Run executed, so "did this regress
    after the last deploy" is a comparison rather than an archaeology exercise.
    """
    client = _state.client
    if client is None:
        yield _NOOP
        return
    trace_context: Mapping[str, str] | None = None
    try:
        trace_context = {"trace_id": client.create_trace_id(seed=run_id)}
    except Exception:  # a random trace id is still a usable trace
        _logger.debug("langfuse trace id could not be derived from %r", run_id, exc_info=True)
    name = "run-resumed" if resumed else "run"
    with (
        _observe(
            "span",
            name,
            input_=None if resumed else redact(prompt),
            trace_context=trace_context,
        ) as handle,
        _quiet(
            lambda: _propagate_attributes(
                session_id=session_id,
                tags=[project_id],
                version=version,
                metadata=None if metadata is None else _redacted(dict(metadata)),
            ),
            "trace attributes",
        ),
    ):
        yield handle


def _propagate_attributes(
    *,
    session_id: str,
    tags: list[str],
    version: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AbstractContextManager[Any]:
    """Bind the trace-level attributes every observation below the root inherits.

    The SDK coerces every propagated metadata value to a string and caps it at 200 characters, so
    this carries correlating identifiers — a commit, a config hash — and never a payload.
    """
    from langfuse import propagate_attributes  # noqa: PLC0415  # only reached while enabled

    return propagate_attributes(
        session_id=redact(session_id),
        tags=[redact(tag) for tag in tags],
        trace_name="run",
        version=None if version is None else redact(version),
        metadata=None if metadata is None else _redacted(dict(metadata)),
    )


def phase(name: str) -> AbstractContextManager[Handle]:
    """One orchestrator's turn inside a Run — `thy` or `mira`."""
    return _observe("agent", name)


def agent(*, name: str, objective: str) -> AbstractContextManager[Handle]:
    """One sub-agent's whole step: its model calls and its tool calls hang below this."""
    return _observe("agent", name, input_=objective)


def generation(
    *,
    role: str,
    task: str,
    input_: Any,
    model: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    model_parameters: Mapping[str, Any] | None = None,
) -> AbstractContextManager[Handle]:
    """One model request. Named `{role}:{task}` — the routed work, never the model id.

    `model` is the id the router *chose*, set here so a call that fails before it answers is still
    attributable to a model; :func:`update_current_generation` later overwrites it with the id that
    actually answered, which can differ. `metadata` carries what was decided before the call — the
    routing tier and reason, and the tools the model was offered — so a reader can see the choices
    the model faced, not only the one it made.

    `model_parameters` is a field of its own in the SDK, not a corner of `metadata`: Langfuse
    serialises it onto a separate attribute, shows it in a dedicated panel, and can match a
    pricing tier on one of its keys. A setting that changes both the answer and the bill belongs
    there rather than in a JSON blob nothing can key on.
    """
    return _observe(
        "generation",
        f"{role}:{task}",
        input_=input_,
        metadata=metadata,
        model=model,
        model_parameters=model_parameters,
    )


def tool(*, name: str, arguments: Mapping[str, Any]) -> AbstractContextManager[Handle]:
    """One tool call through the Tool Manager, as a sibling of the generation that asked for it."""
    return _observe("tool", name, input_=dict(arguments))


def evidence(name: str) -> AbstractContextManager[Handle]:
    """One deterministic evidence step — MIRA's preflight and its controls call no model.

    Typed `evaluator` rather than `chain`. Langfuse documents `chain` as "a link between different
    application steps, like passing context from a retriever to a LLM call", which these are not:
    they link nothing and feed no model. `evaluator` — "assess relevance, correctness or
    helpfulness" — is the closest documented fit rather than an exact one, since MIRA audits a
    whole Run's methodology and recomputes its artifacts, not one model's output. The type only
    affects filtering, so the point of the choice is that a reviewer filtering for evaluators finds
    the audit and a reviewer filtering for chains is not told a lie.
    """
    return _observe("evaluator", name)


def guardrail(name: str) -> AbstractContextManager[Handle]:
    """One Policy Engine decision — the authorization itself; the span only records it."""
    return _observe("guardrail", name)


def update_current(*, output: Any = None, metadata: Mapping[str, Any] | None = None) -> None:
    """Record a result on whatever observation is open, from code that did not open it.

    `phase("thy")` is opened in `thymira.core.graph.compose`, but only `ThySubgraph.invoke` sees
    what THY produced — its adapter returns the runtime state unchanged, so the composing node has
    nothing true to report. Rather than have the opener guess, the code that knows writes it.
    """
    client = _state.client
    if client is None:
        return
    fields: dict[str, Any] = {}
    if output is not None:
        fields["output"] = _redacted(output)
    if metadata is not None:
        fields["metadata"] = _redacted(dict(metadata))
    if not fields:
        return
    try:
        client.update_current_span(**fields)
    except Exception:  # a mirror never fails the Run it observes
        _logger.debug("langfuse observation update failed", exc_info=True)


def update_current_generation(
    *,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None,
    output: Any,
    extra: Mapping[str, Any] | None = None,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    model_parameters: Mapping[str, Any] | None = None,
) -> None:
    """Record what the model that actually answered cost, on the open generation.

    `model` is the id the provider reports, not the router's synthetic `routed:{role}:{task}`
    binding. Cost is ingested rather than inferred — Langfuse can only price a model it has a
    definition for, and Thymira's ids are configuration — and an unknown price is left unset
    instead of being reported as zero.

    `extra` carries whatever else the provider recorded about the call — the reasoning effort it
    applied, say — so a setting that changes both the answer and the bill is visible beside them
    rather than only in the process environment.
    """
    client = _state.client
    if client is None:
        return
    try:
        client.update_current_generation(
            model=model,
            usage_details=usage_details(
                input_tokens,
                output_tokens,
                cached_input_tokens=cached_input_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
            ),
            cost_details=cost_details(cost_usd),
            output=_redacted(output),
            metadata=_redacted({"provider": provider, **(extra or {})}),
            **(
                {}
                if model_parameters is None
                else {"model_parameters": _redacted(dict(model_parameters))}
            ),
        )
    except Exception:  # accounting that did not reach the mirror changes nothing
        _logger.debug("langfuse generation update failed", exc_info=True)


def usage_details(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> dict[str, int]:
    """Map Thymira's token counts onto Langfuse's mutually exclusive usage buckets.

    Providers report *inclusive* counts: OpenAI's `prompt_tokens` already contains its
    `cached_tokens`, and `completion_tokens` already contains its `reasoning_tokens`. Langfuse
    stores a flat `usage_details` unchanged — normalisation happens only for its own wrappers and
    for OpenTelemetry's `gen_ai.usage.*` — and requires every token to be counted in exactly one
    key: `input` excludes `input_*`, `output` excludes `output_*`. Overlapping buckets double-count
    and the inferred cost then overstates the bill, so the two nested counts are subtracted from
    the bucket that contained them rather than added beside it.

    A bucket that is zero is left out entirely: an empty key would claim the provider reported a
    breakdown of nothing, when what happened is that it reported no breakdown.
    """
    cached = max(0, int(cached_input_tokens))
    reasoning = max(0, int(reasoning_output_tokens))
    details = {
        "input": max(0, int(input_tokens) - cached),
        "output": max(0, int(output_tokens) - reasoning),
    }
    if cached:
        details["input_cached_tokens"] = cached
    if reasoning:
        details["output_reasoning_tokens"] = reasoning
    return details


def cost_details(cost_usd: float | None) -> dict[str, float] | None:
    """Map an estimated USD cost onto Langfuse's cost buckets; unknown stays unknown."""
    return None if cost_usd is None else {"total": float(cost_usd)}


def score_run(
    *,
    run_id: str,
    name: str,
    value: float | str,
    data_type: _ScoreDataType,
    comment: str | None = None,
) -> None:
    """Attach one score to a Run's trace, addressed by the Run rather than by ambient context.

    A score is a **mirror**, like every other observation here: it is written only after the
    authoritative event is in `events.jsonl`, and nothing in Thymira reads one back. What it buys
    is the one thing the event log cannot do — Langfuse filters on a score, charts it, trends it
    across Runs and raises an alert from it — so "the BLOCK rate rose this week" becomes a query
    instead of a script over every `events.jsonl` on disk.

    The trace id is derived from `run_id` exactly as :func:`run_trace` derives it, so a score can
    be attached from outside the trace's own context: MIRA audits a Run whose graph has already
    finished, and an out-of-band approval resolves one that stopped hours ago.
    """
    client = _state.client
    if client is None:
        return
    try:
        client.create_score(
            name=redact(name),
            value=value,
            trace_id=client.create_trace_id(seed=run_id),
            data_type=data_type,
            comment=None if comment is None else redact(comment),
        )
    except Exception:  # a score that did not reach the mirror changes no decision
        _logger.debug("langfuse score %r could not be recorded", name, exc_info=True)


def trace_url(run_id: str) -> str | None:
    """Where a reviewer can read this Run's trace, or `None` when tracing is off.

    The Run and its trace already share an id derivation, so the link needs nothing stored: it is
    recomputed on demand. It cannot be built by a client, though — the URL contains the Langfuse
    project, which is known only to whoever holds the API keys — so the runtime has to hand it out.
    """
    client = _state.client
    if client is None:
        return None
    try:
        return client.get_trace_url(trace_id=client.create_trace_id(seed=run_id))
    except Exception:  # a missing link is a missing convenience, nothing more
        _logger.debug("langfuse trace url could not be derived for %r", run_id, exc_info=True)
        return None


def bind_context[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Carry the current OpenTelemetry context into a raw worker thread.

    LangGraph already runs every node through `contextvars.copy_context()`, so the ambient context
    survives `graph.invoke` and its subgraphs unaided. THY's parallel Execute wave is the one
    place that submits to a bare `ThreadPoolExecutor`, which does not, and without this its
    sub-agents would start traces of their own instead of nesting under the Run.
    """
    if not is_enabled():
        return fn
    from opentelemetry import context as otel_context  # noqa: PLC0415  # only reached while enabled

    captured = otel_context.get_current()

    def _run(*args: P.args, **kwargs: P.kwargs) -> T:
        # The attach/detach pair is isolated for the same reason every other call here is: a
        # worker whose context could not be bound must still do its work, unnested rather than
        # undone.
        token = None
        try:
            token = otel_context.attach(captured)
        except Exception:  # noqa: BLE001  # an unbound worker traces badly, it does not fail
            _logger.debug("the OpenTelemetry context could not be bound to a worker thread")
        try:
            return fn(*args, **kwargs)
        finally:
            if token is not None:
                try:
                    otel_context.detach(token)
                except Exception:  # noqa: BLE001  # never mask the wrapped call's own outcome
                    _logger.debug("the OpenTelemetry context could not be detached")

    return _run
