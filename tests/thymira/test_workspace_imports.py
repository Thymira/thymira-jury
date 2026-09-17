from __future__ import annotations

import importlib

import pytest

MEMBERS = [
    "thymira.schemas",
    "thymira.events",
    "thymira.core",
    "thymira.state",
    "thymira.observability",
    "thymira.tools",
    "thymira.policies",
    "thymira.agents",
    "thymira.thy",
    "thymira.mira",
    "thymira.api",
    "thymira.cli",
    "thymira.web",
]


@pytest.mark.parametrize("module", MEMBERS)
def test_workspace_member_is_importable(module: str) -> None:
    assert importlib.import_module(module).__name__ == module


def test_sandbox_contract_enums_are_public_exports() -> None:
    schemas = importlib.import_module("thymira.schemas")

    assert schemas.SandboxMode.WORKSPACE_WRITE.value == "workspace_write"
    assert schemas.SandboxEnforcement.PARTIAL.value == "partial"
