"""Import-boundary tests for `thymira.agents` (THY-33).

Routing types and agent specifications must stay independent of the Tool Manager at import
time; only `AgentRunner`, `build_agent_tools`, and delegation (which itself instantiates
`AgentRunner`) need it. `thymira.mira.agents.spec.AuditAgentSpec` (`GOV-01`) imports
`thymira.agents.llm.routing` directly, so it exercises the same package-initialisation path
(the import-boundary limitation recorded at `MIRA-02`) and is covered here too.

These assertions need a fresh interpreter: once anything has imported `thymira.tools` in this
process, `sys.modules` stays populated for the rest of the test session.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import thymira.agents


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    """Run `script` in a fresh, isolated interpreter and return the completed process."""
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )


_NOT_LOADED_ASSERTIONS = (
    "assert 'thymira.agents.runner' not in sys.modules",
    "assert 'thymira.agents.tool_bridge' not in sys.modules",
    "assert not any(name.startswith('thymira.tools') for name in sys.modules)",
)


def test_importing_agents_llm_routing_does_not_load_the_tool_modules() -> None:
    """A bare import of the routing types must not pull in `AgentRunner`/`thymira.tools`."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.agents.llm.routing import ModelTier, Role, TaskKind",
            *_NOT_LOADED_ASSERTIONS,
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_importing_agent_spec_does_not_load_the_tool_modules() -> None:
    """`AgentSpec` (THY-01) carries no tool dependency; importing it must stay light."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.agents import AgentSpec",
            *_NOT_LOADED_ASSERTIONS,
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_importing_audit_agent_spec_does_not_load_the_tool_modules() -> None:
    """MIRA's `AuditAgentSpec` imports P2's routing package; that must no longer be eager."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.mira.agents import AuditAgentSpec",
            *_NOT_LOADED_ASSERTIONS,
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_agent_runner_import_still_exposes_the_tool_bridge_path() -> None:
    """`AgentRunner` stays importable and still pulls in the tool bridge it depends on."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.agents import AgentRunner",
            "assert AgentRunner is not None",
            "assert 'thymira.agents.tool_bridge' in sys.modules",
            "assert any(name.startswith('thymira.tools') for name in sys.modules)",
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_build_agent_tools_import_still_exposes_the_tool_manager() -> None:
    """`build_agent_tools` stays importable and still pulls in the Tool Manager it wraps."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.agents import build_agent_tools",
            "assert build_agent_tools is not None",
            "assert any(name.startswith('thymira.tools') for name in sys.modules)",
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_delegator_import_still_exposes_the_runner_it_wraps() -> None:
    """`Delegator` instantiates `AgentRunner`; importing it still pulls the runner in."""
    script = "\n".join(
        (
            "import sys",
            "from thymira.agents import Delegator",
            "assert Delegator is not None",
            "assert 'thymira.agents.runner' in sys.modules",
        )
    )

    result = _run_isolated(script)

    assert result.returncode == 0, result.stderr


def test_unknown_attribute_still_raises_attribute_error() -> None:
    """The lazy `__getattr__` fallback must not swallow a genuine typo into an import error."""
    with pytest.raises(AttributeError, match="not_a_real_export"):
        thymira.agents.not_a_real_export  # noqa: B018 -- intentional attribute-error probe
