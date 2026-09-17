"""Unit tests for the frozen HTTP boundary models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from thymira.api import ApiError, CreateRunRequest, EventPage
from thymira.schemas import new_id


def test_create_run_request_keeps_cli_prompt_only_compatibility() -> None:
    request = CreateRunRequest(prompt="Analyze this dataset")

    assert request.prompt == "Analyze this dataset"
    assert request.project_id is None
    assert request.session_id is None
    assert request.client == "api"


def test_create_run_request_rejects_empty_prompt_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateRunRequest(prompt="")

    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate({"prompt": "Analyze", "unexpected": "value"})


def test_event_page_validates_run_and_pagination_fields() -> None:
    page = EventPage(run_id=new_id("run"), next_after_seq=10, has_more=True)

    assert page.items == ()
    assert page.next_after_seq == 10
    assert page.has_more is True

    with pytest.raises(ValidationError):
        EventPage(run_id="not-a-run", next_after_seq=-1)


def test_api_error_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ApiError.model_validate({"code": "run_not_found", "message": "missing", "extra": True})
