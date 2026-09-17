"""`thymira.observability`: the disabled path, redaction, failure isolation and the usage mapping.

The span *tree* a whole Run produces is pinned in `test_observability_integration.py`; this file
covers the guarantees that hold with no backend at all, because those are the ones every other
test in the suite silently depends on.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from thymira import observability
from thymira.observability import tracing
from thymira.observability.tracing import _isolated_provider, _mask, _sample_rate

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_tracing() -> Iterator[None]:
    """No test may leave a client behind: the module state is process-wide."""
    observability.reset()
    yield
    observability.reset()


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tracing.PUBLIC_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(tracing.SECRET_KEY_ENV_VAR, raising=False)
    observability.configure()


class _RecordingClient:
    """A stand-in for the Langfuse client that records what it was asked to do."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.opened: list[dict[str, Any]] = []
        self.generation_updates: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = 0

    @contextmanager
    def start_as_current_observation(self, **fields: Any) -> Iterator[_RecordingSpan]:
        if self.fail:
            raise RuntimeError("the backend is unreachable")
        self.opened.append(fields)
        yield _RecordingSpan()

    def update_current_generation(self, **fields: Any) -> None:
        if self.fail:
            raise RuntimeError("the backend is unreachable")
        self.generation_updates.append(fields)

    def create_trace_id(self, *, seed: str | None = None) -> str:
        return (seed or "x").ljust(32, "0")[:32]

    def create_score(self, **fields: Any) -> None:
        if self.fail:
            raise RuntimeError("the backend is unreachable")
        self.scores.append(fields)

    def get_trace_url(self, *, trace_id: str) -> str:
        if self.fail:
            raise RuntimeError("the backend is unreachable")
        return f"https://langfuse.test/project/p1/traces/{trace_id}"

    def flush(self) -> None:
        self.flushed += 1


class _RecordingSpan:
    """Records its updates, and remembers the most recent instance for the assertions below."""

    last: _RecordingSpan

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        _RecordingSpan.last = self

    def update(self, **fields: Any) -> None:
        self.updates.append(fields)


class _RefusingSpan:
    """A span whose every update fails, so the failure marker's own isolation is exercised."""

    def update(self, **fields: Any) -> None:
        """Refuse, the way an unreachable backend does."""
        del fields
        raise RuntimeError("the backend is unreachable")


class _RefusingClient(_RecordingClient):
    """A client whose observations open normally and then refuse every update."""

    @contextmanager
    def start_as_current_observation(self, **fields: Any) -> Iterator[_RefusingSpan]:
        """Open an observation whose updates will fail."""
        self.opened.append(fields)
        yield _RefusingSpan()


# --------------------------------------------------------------------------- the disabled path


def test_missing_keys_leave_tracing_off_without_importing_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconfigured, the SDK is never even imported — it must not cost an import to not use it."""
    monkeypatch.delenv(tracing.PUBLIC_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(tracing.SECRET_KEY_ENV_VAR, raising=False)
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)

    observability.configure()

    assert observability.is_enabled() is False
    assert "langfuse" not in sys.modules


def test_one_key_alone_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tracing.PUBLIC_KEY_ENV_VAR, "pk-lf-only")
    monkeypatch.delenv(tracing.SECRET_KEY_ENV_VAR, raising=False)

    observability.configure()

    assert observability.is_enabled() is False


def test_tracing_enabled_false_wins_over_present_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill switch is honoured here, not left to the SDK, which only no-ops its tracer."""
    monkeypatch.setenv(tracing.PUBLIC_KEY_ENV_VAR, "pk-lf-x")
    monkeypatch.setenv(tracing.SECRET_KEY_ENV_VAR, "sk-lf-x")
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV_VAR, "false")

    observability.configure()

    assert observability.is_enabled() is False


def test_the_suite_cannot_build_a_live_client_even_with_real_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the guard in `tests/conftest.py`, now that `.env` is loadable and keys are real.

    Without it, a developer who exports `LANGFUSE_*` would ship a trace of every test that builds
    an app — `create_app` calls `configure` — to their own Langfuse project.
    """
    monkeypatch.setenv(tracing.PUBLIC_KEY_ENV_VAR, "pk-lf-looks-real")
    monkeypatch.setenv(tracing.SECRET_KEY_ENV_VAR, "sk-lf-looks-real")

    observability.configure()

    assert observability.is_enabled() is False


@pytest.mark.usefixtures("disabled")
def test_every_helper_is_a_no_op_when_disabled() -> None:
    """Each seam still runs its body, yields a handle and returns the body's value."""
    with observability.run_trace(
        run_id="run_1", session_id="session_1", project_id="project_1", prompt="hello"
    ) as root:
        root.update(output="done")
        with observability.phase("thy"), observability.agent(name="data", objective="profile"):
            with observability.generation(role="agent", task="analyze", input_=None):
                observability.update_current_generation(
                    model="m",
                    provider="p",
                    input_tokens=1,
                    output_tokens=2,
                    cost_usd=None,
                    output="ok",
                )
            with observability.tool(name="run_python", arguments={"code": "1"}) as call:
                call.update(output="1")
        with observability.evidence("deterministic-controls"), observability.guardrail("policy"):
            pass
    observability.flush()
    observability.shutdown()


@pytest.mark.usefixtures("disabled")
def test_bind_context_returns_the_function_unchanged_when_disabled() -> None:
    def work(value: int) -> int:
        return value * 2

    assert observability.bind_context(work) is work


# --------------------------------------------------------------------------- redaction


def test_the_mask_is_the_event_log_s_own_redaction() -> None:
    """One redaction boundary, not two: the SDK hook runs exactly what `EventLog.append` runs."""
    masked = _mask(data={"note": "write to alice@example.com", "key": ["sk-abcdefghijklmnopqrst"]})

    assert "alice@example.com" not in str(masked)
    assert "[REDACTED:EMAIL]" in masked["note"]


def test_open_and_update_redact_before_the_client_sees_anything() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.tool(name="query", arguments={"who": "alice@example.com"}) as call:
        call.update(output="mailed alice@example.com")

    (opened,) = client.opened
    assert "alice@example.com" not in str(opened["input"])
    assert "alice@example.com" not in str(opened)


def test_run_trace_redacts_the_prompt_and_derives_the_trace_id_from_the_run() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.run_trace(
        run_id="run_abc",
        session_id="session_1",
        project_id="project_1",
        prompt="email bob@example.com about the model",
    ):
        pass

    (opened,) = client.opened
    assert opened["name"] == "run"
    assert "bob@example.com" not in opened["input"]
    assert opened["trace_context"] == {"trace_id": client.create_trace_id(seed="run_abc")}


def test_a_resumed_run_is_a_second_named_root_in_the_same_trace() -> None:
    """A Run parked for review runs twice; two roots called `run` would render as one node."""
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.run_trace(
        run_id="run_abc", session_id="s", project_id="p", prompt="the prompt", resumed=True
    ):
        pass

    (opened,) = client.opened
    assert opened["name"] == "run-resumed"
    assert opened["trace_context"] == {"trace_id": client.create_trace_id(seed="run_abc")}
    # The resumed episode continues from a checkpoint; it does not re-read the request.
    assert "input" not in opened


# --------------------------------------------------------------------------- failure isolation


def test_a_failed_observation_is_marked_so_a_reviewer_can_filter_for_it() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.agent(name="data", objective="profile") as handle:
        handle.update(output={"status": "failed"}, failed=True, reason="exhausted max_turns=3")

    (update,) = _RecordingSpan.last.updates
    assert update["level"] == "ERROR"
    assert update["status_message"] == "exhausted max_turns=3"
    assert update["output"] == {"status": "failed"}


def test_a_failure_reason_is_redacted_like_everything_else() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.agent(name="data", objective="profile") as handle:
        handle.update(failed=True, reason="auth failed for analyst@example.com")

    (update,) = _RecordingSpan.last.updates
    assert "analyst@example.com" not in update["status_message"]


def test_a_broken_backend_never_fails_the_run() -> None:
    """A mirror that cannot record must still let the work it observes run and return."""
    observability.configure(client=_RecordingClient(fail=True))

    with observability.agent(name="data", objective="profile") as handle:
        handle.update(output="ignored")
        result = "the body ran"
        observability.update_current_generation(
            model="m", provider="p", input_tokens=1, output_tokens=1, cost_usd=None, output="x"
        )

    assert result == "the body ran"


def test_an_exception_from_the_body_still_propagates() -> None:
    """Only telemetry failures are swallowed — a budget breach must reach its caller unchanged."""
    observability.configure(client=_RecordingClient())

    with pytest.raises(ValueError, match="budget"), observability.agent(name="a", objective="o"):
        raise ValueError("budget exceeded")


def test_a_body_s_exception_reaches_the_span_as_a_class_name_and_nothing_else() -> None:
    """Handing the exception to `__exit__` is what OpenTelemetry expects, and what would leak.

    `use_span` defaults to recording the exception, which attaches `str(exc)` and a full
    stacktrace as span events that no redaction reaches. Only the class name is safe.
    """
    observability.configure(client=_RecordingClient())

    with pytest.raises(ValueError, match="came back"), observability.agent(name="a", objective="o"):
        raise ValueError("sk-proj-AbCdEf0123456789 came back from the model")

    assert _RecordingSpan.last.updates == [{"level": "ERROR", "status_message": "ValueError"}]


def test_marking_a_failure_never_masks_the_body_s_own_exception() -> None:
    """The marker is telemetry like everything else here: its own failure changes nothing."""
    observability.configure(client=_RefusingClient())

    with pytest.raises(ValueError, match="budget"), observability.agent(name="a", objective="o"):
        raise ValueError("budget exceeded")


def test_configure_leaves_tracing_off_when_the_client_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured backend degrades to no tracing, never to a failed import at boot."""
    monkeypatch.setenv(tracing.PUBLIC_KEY_ENV_VAR, "pk-lf-x")
    monkeypatch.setenv(tracing.SECRET_KEY_ENV_VAR, "sk-lf-x")
    monkeypatch.delenv(tracing.TRACING_ENABLED_ENV_VAR, raising=False)

    def explode(**_: Any) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr("langfuse.Langfuse", explode)
    observability.configure()

    assert observability.is_enabled() is False


# --------------------------------------------------------------------------- usage and cost


def test_usage_details_use_langfuse_s_mutually_exclusive_buckets() -> None:
    assert observability.usage_details(10, 20) == {"input": 10, "output": 20}


@pytest.mark.parametrize(
    ("cost_usd", "expected"),
    [(0.003, {"total": 0.003}), (0.0, {"total": 0.0}), (None, None)],
)
def test_an_unknown_price_stays_unknown(cost_usd: float | None, expected: object) -> None:
    """A model with no published price reports no cost, never a cost of zero."""
    assert observability.cost_details(cost_usd) == expected


def test_the_generation_records_the_model_that_answered() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.generation(role="thy", task="plan", input_=[{"role": "user", "c": "x"}]):
        observability.update_current_generation(
            model="anthropic/frontier-1",
            provider="anthropic",
            input_tokens=11,
            output_tokens=22,
            cost_usd=0.5,
            output="answer",
        )

    (opened,) = client.opened
    (update,) = client.generation_updates
    assert opened["as_type"] == "generation"
    assert opened["name"] == "thy:plan"
    assert update["model"] == "anthropic/frontier-1"
    assert update["usage_details"] == {"input": 11, "output": 22}
    assert update["cost_details"] == {"total": 0.5}
    assert update["metadata"] == {"provider": "anthropic"}


def test_a_generation_carries_whatever_else_the_provider_recorded() -> None:
    """A reasoning effort changes the answer and the bill, so it belongs beside them."""
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.generation(role="thy", task="plan", input_=None):
        observability.update_current_generation(
            model="m",
            provider="anthropic",
            input_tokens=1,
            output_tokens=1,
            cost_usd=None,
            output="answer",
            extra={"reasoning_effort": "high", "reasoning_effort_applied": True},
        )

    (update,) = client.generation_updates
    assert update["metadata"] == {
        "provider": "anthropic",
        "reasoning_effort": "high",
        "reasoning_effort_applied": True,
    }


def test_observation_types_match_the_seam_each_helper_wraps() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.phase("thy"):
        pass
    with observability.agent(name="data", objective="o"):
        pass
    with observability.tool(name="run_python", arguments={}):
        pass
    with observability.evidence("deterministic-controls"):
        pass
    with observability.guardrail("policy-review-findings"):
        pass

    assert [(entry["name"], entry["as_type"]) for entry in client.opened] == [
        ("thy", "agent"),
        ("data", "agent"),
        ("run_python", "tool"),
        ("deterministic-controls", "evaluator"),
        ("policy-review-findings", "guardrail"),
    ]


# --------------------------------------------------------------------------- sampling


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.25", 0.25), ("1", 1.0), ("0", 0.0), ("", 1.0), ("half", 1.0), ("1.5", 1.0), ("-1", 1.0)],
)
def test_an_unusable_sample_rate_keeps_every_trace(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: float
) -> None:
    """A typo in an operator's cost control must not silently stop the telemetry."""
    monkeypatch.setenv(tracing.SAMPLE_RATE_ENV_VAR, value)

    assert _sample_rate() == expected


def test_an_unset_sample_rate_keeps_every_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tracing.SAMPLE_RATE_ENV_VAR, raising=False)

    assert _sample_rate() == 1.0


def test_the_provider_carries_the_sampler_the_sdk_would_have_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK builds a sampler from `LANGFUSE_SAMPLE_RATE` only for a provider it builds itself.

    Thymira always supplies its own, so the documented variable reaches nothing unless this
    module applies it: a bare `TracerProvider` samples every trace.
    """
    monkeypatch.setenv(tracing.SAMPLE_RATE_ENV_VAR, "0.25")

    description = _isolated_provider().sampler.get_description()

    assert "0.25" in description


def test_an_unsampled_provider_keeps_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tracing.SAMPLE_RATE_ENV_VAR, raising=False)

    assert "ratio" not in _isolated_provider().sampler.get_description().lower()


# --------------------------------------------------------------------------- exclusive usage


def test_nested_counts_are_subtracted_from_the_totals_that_contain_them() -> None:
    """Langfuse stores a flat `usage_details` unchanged and prices every key as its own bucket.

    Providers report inclusive counts, so adding the cache hits beside the prompt total would
    count them twice and overstate the inferred cost.
    """
    details = observability.usage_details(
        17903, 188, cached_input_tokens=17817, reasoning_output_tokens=15
    )

    assert details == {
        "input": 86,
        "output": 173,
        "input_cached_tokens": 17817,
        "output_reasoning_tokens": 15,
    }
    assert details["input"] + details["input_cached_tokens"] == 17903
    assert details["output"] + details["output_reasoning_tokens"] == 188


def test_a_provider_that_reports_no_breakdown_gets_no_empty_buckets() -> None:
    """An `input_cached_tokens: 0` would claim a breakdown of nothing was reported."""
    assert observability.usage_details(10, 5) == {"input": 10, "output": 5}


def test_a_nested_count_larger_than_its_total_never_goes_negative() -> None:
    """Providers disagree with themselves occasionally; a negative bucket would be a lie."""
    assert observability.usage_details(10, 5, cached_input_tokens=99) == {
        "input": 0,
        "output": 5,
        "input_cached_tokens": 99,
    }


# --------------------------------------------------------------------------- scores and links


def test_a_score_is_addressed_by_the_run_not_by_the_open_observation() -> None:
    """MIRA audits a Run whose graph has finished, so a score cannot depend on ambient context."""
    client = _RecordingClient()
    observability.configure(client=client)

    observability.score_run(
        run_id="run_1", name="policy.decision", value="BLOCK", data_type="CATEGORICAL"
    )

    (score,) = client.scores
    assert score["name"] == "policy.decision"
    assert score["value"] == "BLOCK"
    assert score["data_type"] == "CATEGORICAL"
    assert score["trace_id"] == client.create_trace_id(seed="run_1")


def test_a_score_comment_is_redacted_like_every_other_field() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    observability.score_run(
        run_id="run_1",
        name="mira.control.A1",
        value=1.0,
        data_type="NUMERIC",
        comment="raised by alice@example.com",
    )

    assert "alice@example.com" not in str(client.scores)


def test_a_score_never_fails_the_run_that_produced_it() -> None:
    """The Gate records a score right after the authoritative event; neither may raise."""
    observability.configure(client=_RecordingClient(fail=True))

    observability.score_run(
        run_id="run_1", name="policy.decision", value="PASS", data_type="CATEGORICAL"
    )


def test_the_trace_url_is_derived_from_the_run() -> None:
    client = _RecordingClient()
    observability.configure(client=client)

    assert observability.trace_url("run_1") == (
        f"https://langfuse.test/project/p1/traces/{client.create_trace_id(seed='run_1')}"
    )


@pytest.mark.usefixtures("disabled")
def test_there_is_no_trace_url_when_there_is_no_tracing() -> None:
    assert observability.trace_url("run_1") is None


def test_an_unreachable_backend_yields_no_trace_url_rather_than_an_error() -> None:
    observability.configure(client=_RecordingClient(fail=True))

    assert observability.trace_url("run_1") is None


# --------------------------------------------------------------------------- egress hardening


class _Leaky:
    """An object whose `repr` carries what a call site should never have handed over."""

    def __repr__(self) -> str:
        """Expose a secret and an address, the way a real model or dataclass repr would."""
        return "Leaky(owner='alice@example.com', key='sk-proj-AbCdEf0123456789AbCdEf0123456789')"


def test_an_object_the_redaction_cannot_descend_into_is_dropped() -> None:
    """An unsupported object is rejected before its representation can cross the trace boundary.

    `redact_value` returns an unknown type unchanged — right for the event log, where a datetime
    must survive canonicalisation as itself. The trace projection has no safe representation for
    an unknown value, so it emits an explicit failure marker instead of calling `repr`.
    """
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.tool(name="query", arguments={"row": _Leaky()}) as call:
        call.update(output=[_Leaky()])

    sent = str(client.opened) + str(_RecordingSpan.last.updates)
    assert "alice@example.com" not in sent
    assert "sk-proj-AbCdEf0123456789AbCdEf0123456789" not in sent
    assert "[REDACTION-FAILED]" in sent


def test_the_shapes_the_event_log_relies_on_still_pass_through_as_themselves() -> None:
    """Scrubbing must not turn a number into text: the same values reach `events.jsonl`."""
    client = _RecordingClient()
    observability.configure(client=client)

    with observability.tool(name="t", arguments={"n": 3, "x": 1.5, "ok": True, "none": None}):
        pass

    assert client.opened[0]["input"] == {"n": 3, "x": 1.5, "ok": True, "none": None}
