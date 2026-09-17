"""Acceptance test for the CLI THY activity view (THY-30).

Real boundary: the Typer ``status`` command, the ``ApiClient`` event parsing, and the pure fold in
``thymira.cli.thy_view`` (phases, delegation tree, per-agent tokens/cost). Faked boundary: the
Thymira API, through a deterministic ``httpx.MockTransport`` that serves one run and its recorded
event history. No network, no runtime import — the CLI folds only what it reads over HTTP.

The recorded log is the shape a real THY run emits: THY's own ``plan``/``synthesize``
``model.selected`` calls bracket the run, each delegated agent is an ``agent.started`` ->
``model.selected`` -> ``agent.completed`` -> ``agent.message`` sequence, and every
``agent.completed`` carries the step's ``input_tokens``/``output_tokens``/``cost_usd`` (recorded by
the runner, THY-30), while ``model.response_chunk`` covers every provider call. The view must keep
the agent settlement subtotal distinct from the all-request token total so it never presents a
partial dollar amount as whole-run billing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient, EventView
from thymira.cli.thy_view import fold_thy_activity

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"

_Payload = dict[str, object]

# One data agent (one model call) and one experiment agent (two model calls); the token counts are
# distinct so a mis-attribution or double-count shows up immediately in the totals.
_DATA_USAGE: _Payload = {"input_tokens": 100, "output_tokens": 40, "cost_usd": 0.0020}
_EXPERIMENT_USAGE: _Payload = {"input_tokens": 300, "output_tokens": 120, "cost_usd": 0.0060}
_TOTAL_TOKENS = 100 + 40 + 300 + 120
_TOTAL_COST = 0.0020 + 0.0060
_PROVIDER_INPUT = 580
_PROVIDER_OUTPUT = 195
_PROVIDER_CACHED_INPUT = 145
_PROVIDER_REASONING_OUTPUT = 25
_PROVIDER_REQUESTS = 6


def _response(
    index: int,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> _Payload:
    """One terminal provider response payload with independently visible usage."""
    return {
        "request_id": f"request_{index:032x}",
        "response_id": f"response_{index:032x}",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "terminal": {"outcome": "success", "chunk_count": 1},
    }


# A full THY run in event order: inspect -> plan -> execute (two agents) -> summarize.
_EVENTS: list[tuple[int, str, _Payload]] = [
    (0, "run.started", {}),
    (1, "model.selected", {"role": "mira", "task": "classify", "model": "m"}),
    (2, "model.response_chunk", _response(1, input_tokens=70, output_tokens=5)),
    (3, "model.selected", {"role": "thy", "task": "plan", "model": "m"}),
    (
        4,
        "model.response_chunk",
        _response(2, input_tokens=50, output_tokens=10, cached_input_tokens=20),
    ),
    (5, "policy.decision", {"decision": "PASS"}),
    (6, "agent.started", {"agent": "data", "task_id": "task_1"}),
    (7, "model.selected", {"role": "agent", "task": "analyze", "model": "m"}),
    (
        8,
        "model.response_chunk",
        _response(
            3,
            input_tokens=100,
            output_tokens=40,
            cached_input_tokens=30,
            reasoning_output_tokens=5,
        ),
    ),
    (9, "agent.completed", {"agent": "data", "status": "completed", **_DATA_USAGE}),
    (10, "agent.message", {"parent_agent": "thy", "agent": "data", "depth": 1}),
    (11, "agent.started", {"agent": "experiment", "task_id": "task_2"}),
    (12, "model.selected", {"role": "agent", "task": "code", "model": "m"}),
    (
        13,
        "model.response_chunk",
        _response(
            4,
            input_tokens=150,
            output_tokens=60,
            cached_input_tokens=40,
            reasoning_output_tokens=10,
        ),
    ),
    (14, "model.selected", {"role": "agent", "task": "code", "model": "m"}),
    (
        15,
        "model.response_chunk",
        _response(
            5,
            input_tokens=150,
            output_tokens=60,
            cached_input_tokens=40,
            reasoning_output_tokens=10,
        ),
    ),
    (
        16,
        "agent.completed",
        {"agent": "experiment", "status": "completed", **_EXPERIMENT_USAGE},
    ),
    (17, "agent.message", {"parent_agent": "thy", "agent": "experiment", "depth": 1}),
    (18, "model.selected", {"role": "thy", "task": "synthesize", "model": "m"}),
    (
        19,
        "model.response_chunk",
        _response(6, input_tokens=60, output_tokens=20, cached_input_tokens=15),
    ),
]
_ONLY_STARTED: list[tuple[int, str, _Payload]] = [(0, "run.started", {})]


def _envelope(seq: int, event_type: str, payload: _Payload) -> _Payload:
    """One API event envelope carrying a redacted payload, as the events route returns it."""
    return {
        "event_id": f"event_{seq:032x}",
        "run_id": RUN_ID,
        "seq": seq,
        "type": event_type,
        "schema_version": "0.3",
        "ts": "2026-08-22T10:14:05Z",
        "actor": {"kind": "system", "id": "system", "role": None, "authenticated": True},
        "surface": "log_only",
        "producer": "test",
        "producer_version": "test-v1",
        "payload": payload,
        "prev_hash": "0" * 64,
        "hash": "1" * 64,
    }


def _envelopes(events: list[tuple[int, str, _Payload]]) -> list[_Payload]:
    return [_envelope(seq, event_type, payload) for seq, event_type, payload in events]


def _event_views(events: list[tuple[int, str, _Payload]]) -> list[EventView]:
    return [
        EventView(seq=seq, type=event_type, actor="system", payload=payload)
        for seq, event_type, payload in events
    ]


def _run_payload() -> _Payload:
    return {
        "id": RUN_ID,
        "status": "RUNNING",
        "prompt": "Assess credit-risk applicants and recommend a model.",
        "created_at": "2026-08-22T10:14:05Z",
        "started_at": "2026-08-22T10:14:06Z",
        "agent_ids": ["agent_a", "agent_b"],
        "artifact_ids": ["artifact_a"],
    }


def _handler(events: list[tuple[int, str, _Payload]]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve ``GET /runs/{id}`` and one buffered page of ``GET /runs/{id}/events``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/runs/{RUN_ID}/events":
            return httpx.Response(
                200,
                json={
                    "run_id": RUN_ID,
                    "items": _envelopes(events),
                    "next_after_seq": None,
                    "has_more": False,
                },
                request=request,
            )
        return httpx.Response(200, json=_run_payload(), request=request)

    return handler


def _invoke(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_status_separates_agent_settlements_from_all_provider_tokens() -> None:
    """The status never presents an agent-only dollar subtotal as whole-run billing."""
    result = _invoke(["status", RUN_ID], _handler(_EVENTS))

    assert result.exit_code == 0
    output = result.output

    # The run status is still shown, with the activity view below it.
    assert "Run status" in output
    assert "THY activity" in output
    # Each of the four workflow phases the log evidences is rendered.
    for phase in ("inspect", "plan", "execute", "summarize"):
        assert phase in output, f"missing phase {phase!r} in:\n{output}"

    # The delegation tree: THY delegated to both specialist agents.
    assert "Delegation tree" in output
    assert "thy" in output
    assert "data" in output
    assert "experiment" in output

    # A per-agent token/cost line for each agent, and a scoped subtotal that matches the sum.
    assert "Settled agent usage" in output
    assert "140" in output  # data: 100 + 40 tokens
    assert "$0.0020" in output  # data cost
    assert "420" in output  # experiment: 300 + 120 tokens
    assert "$0.0060" in output  # experiment cost
    assert "Agent subtotal" in output
    assert str(_TOTAL_TOKENS) in output  # 560
    assert f"${_TOTAL_COST:.4f}" in output  # $0.0080

    # Provider tokens include MIRA/THY orchestration as well as all delegated agent calls.
    assert "All provider request tokens" in output
    for value in (
        _PROVIDER_REQUESTS,
        _PROVIDER_INPUT,
        _PROVIDER_OUTPUT,
        _PROVIDER_INPUT + _PROVIDER_OUTPUT,
        _PROVIDER_CACHED_INPUT,
        _PROVIDER_REASONING_OUTPUT,
    ):
        assert str(value) in output
    assert "excludes THY/MIRA/preflight calls" in output
    assert "whole-run price" in output


def test_fold_totals_match_the_summed_per_agent_usage() -> None:
    """The agent subtotal and broader provider token total retain their exact scopes."""
    view = fold_thy_activity(_event_views(_EVENTS))

    assert [phase.name for phase in view.phases] == ["inspect", "plan", "execute", "summarize"]
    assert {usage.name for usage in view.agent_usage} == {"data", "experiment"}

    summed_tokens = sum(usage.total_tokens for usage in view.agent_usage)
    # Every agent here is priced, so no part of the sum is unmeasured; the None case is pinned by
    # test_an_unpriced_step_renders_as_unknown_instead_of_zero below.
    summed_cost = sum(usage.cost_usd or 0.0 for usage in view.agent_usage)
    assert view.total.total_tokens == summed_tokens == _TOTAL_TOKENS
    assert view.total.cost_usd == pytest.approx(summed_cost)
    assert view.total.cost_usd == pytest.approx(_TOTAL_COST)

    provider = view.provider_usage
    assert provider is not None
    assert provider.request_count == _PROVIDER_REQUESTS
    assert provider.input_tokens == _PROVIDER_INPUT
    assert provider.output_tokens == _PROVIDER_OUTPUT
    assert provider.total_tokens == _PROVIDER_INPUT + _PROVIDER_OUTPUT
    assert provider.cached_input_tokens == _PROVIDER_CACHED_INPUT
    assert provider.reasoning_output_tokens == _PROVIDER_REASONING_OUTPUT

    # The tree is rooted at THY with both agents as its children, each carrying its own usage.
    assert len(view.tree) == 1
    root = view.tree[0]
    assert root.name == "thy"
    assert {child.name for child in root.children} == {"data", "experiment"}
    experiment = next(child for child in root.children if child.name == "experiment")
    assert experiment.usage.total_tokens == 420


def test_execute_phase_reflects_every_delegated_model_call() -> None:
    """The execute phase counts all three sub-agent calls; plan and summarize count one each."""
    view = fold_thy_activity(_event_views(_EVENTS))
    by_name = {phase.name: phase for phase in view.phases}

    assert by_name["plan"].model_calls == 1
    assert by_name["execute"].model_calls == 3
    assert by_name["summarize"].model_calls == 1


def test_status_omits_activity_when_the_run_has_no_thy_events() -> None:
    """A run whose log holds nothing THY-specific shows the status only, no activity section."""
    result = _invoke(["status", RUN_ID], _handler(_ONLY_STARTED))

    assert result.exit_code == 0
    assert "Run status" in result.output
    assert "THY activity" not in result.output

    view = fold_thy_activity(_event_views(_ONLY_STARTED))
    assert not view.has_activity
    assert view.tree == ()
    assert view.agent_usage == ()


def test_an_unpriced_step_renders_as_unknown_instead_of_zero() -> None:
    """A null ``cost_usd`` stays unmeasured through the fold and the rendered table.

    The runner records ``null`` when the gateway priced no step of a call, precisely so nothing
    downstream claims the work was free. Folding it to ``0.0`` -- which this view did -- put a
    confident dollar figure in front of the one reader who cannot check it, and silently undid the
    distinction the ledger, the runner and the Gate all keep.
    """
    unpriced: list[tuple[int, str, _Payload]] = [
        (0, "run.started", {}),
        (1, "agent.started", {"agent": "data", "task_id": "task_1"}),
        (2, "agent.completed", {"agent": "data", "status": "completed", **_DATA_USAGE}),
        (
            3,
            "agent.completed",
            {"agent": "data", "status": "completed", "input_tokens": 10, "output_tokens": 5},
        ),
        (
            4,
            "agent.completed",
            {
                "agent": "experiment",
                "status": "completed",
                "input_tokens": 7,
                "output_tokens": 3,
                "cost_usd": None,
            },
        ),
    ]
    view = fold_thy_activity(_event_views(unpriced))
    by_name = {usage.name: usage for usage in view.agent_usage}

    # An absent key contributes nothing and is not a failed measurement.
    assert by_name["data"].cost_usd == pytest.approx(0.0020)
    # An explicit null is a failed measurement, and it poisons the agent and the grand total.
    assert by_name["experiment"].cost_usd is None
    assert view.total.cost_usd is None

    result = _invoke(["status", RUN_ID], _handler(unpriced))
    assert result.exit_code == 0
    assert "unknown" in result.output
