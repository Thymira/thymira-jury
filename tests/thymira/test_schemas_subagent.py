"""The closed subagent stop-reason vocabulary and the uniform `SubagentResult` (F7.1/F7.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thymira.schemas import EventType, StopReason, SubagentResult, new_id

_KEY = "a" * 64


def _fields(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": new_id("run"),
        "task_id": new_id("task"),
        "agent_id": new_id("agent"),
        "agent": "data",
        "parent_agent": "thy",
        "objective": "profile the dataset",
        "delegation_depth": 1,
        "delegation_key": _KEY,
        "stop_reason": StopReason.FAILED,
        "diagnostics": "disk full",
        "diagnostics_limit": 2000,
    }
    payload.update(overrides)
    return payload


def test_the_stop_reason_vocabulary_is_exactly_six_members() -> None:
    assert {reason.value for reason in StopReason} == {
        "completed",
        "stopped",
        "out-of-room",
        "declined",
        "failed",
        "abnormal",
    }


def test_an_unknown_stop_reason_is_rejected_at_the_model_boundary() -> None:
    """An unknown value is refused, never coerced to a neighbour and never defaulted."""
    with pytest.raises(ValidationError):
        SubagentResult.model_validate(_fields(stop_reason="cancelled"))
    # The hyphen is the spelling the criterion fixes; the underscore is a different value.
    with pytest.raises(ValidationError):
        SubagentResult.model_validate(_fields(stop_reason="out_of_room"))
    with pytest.raises(ValueError, match="cancelled"):
        StopReason("cancelled")


def test_a_stop_reason_has_no_default_so_a_settlement_cannot_omit_it() -> None:
    payload = _fields()
    del payload["stop_reason"]
    with pytest.raises(ValidationError, match="stop_reason"):
        SubagentResult.model_validate(payload)


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SubagentResult.model_validate(_fields(exit_code=0))


def test_a_completed_settlement_carries_its_result_and_schema_and_a_non_completed_one_carries_neither() -> (  # noqa: E501  # one behavioral node, one name
    None
):
    completed = SubagentResult.model_validate(
        _fields(
            stop_reason=StopReason.COMPLETED,
            result_json='{"row_count":42}',
            result_schema="tests.thymira.fixtures_agent_output:DataProfileOutput",
            diagnostics=None,
        )
    )
    assert completed.result_json == '{"row_count":42}'
    assert completed.result_schema == "tests.thymira.fixtures_agent_output:DataProfileOutput"

    with pytest.raises(ValidationError, match="result_json"):
        SubagentResult.model_validate(_fields(stop_reason=StopReason.COMPLETED))
    with pytest.raises(ValidationError, match="result_json"):
        SubagentResult.model_validate(
            _fields(stop_reason=StopReason.FAILED, result_json="{}", result_schema="a:B")
        )
    with pytest.raises(ValidationError, match="result_schema"):
        SubagentResult.model_validate(
            _fields(stop_reason=StopReason.COMPLETED, result_json="{}", diagnostics=None)
        )


def test_a_malformed_delegation_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="delegation_key"):
        SubagentResult.model_validate(_fields(delegation_key="not-a-digest"))
    with pytest.raises(ValidationError, match="delegation_key"):
        SubagentResult.model_validate(_fields(delegation_key=_KEY.upper()))


def test_a_negative_depth_or_diagnostics_limit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="delegation_depth"):
        SubagentResult.model_validate(_fields(delegation_depth=-1))
    with pytest.raises(ValidationError, match="diagnostics_limit"):
        SubagentResult.model_validate(_fields(diagnostics_limit=-1))


def test_the_settlement_event_type_is_part_of_the_closed_vocabulary() -> None:
    assert EventType.SUBAGENT_SETTLED.value == "subagent.settled"


def test_a_settlement_round_trips_through_json() -> None:
    result = SubagentResult.model_validate(_fields())
    assert SubagentResult.model_validate(result.to_json_dict()) == result
