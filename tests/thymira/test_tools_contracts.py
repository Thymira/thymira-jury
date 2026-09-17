"""Declared file-tool contracts checked against real behaviour (F3.6)."""

from __future__ import annotations

import ast
import inspect
from typing import TYPE_CHECKING, Any, Protocol, cast

from thymira.schemas import new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation
from thymira.tools.builtins import files as files_module
from thymira.tools.builtins import search as search_module
from thymira.tools.builtins.file_contracts import FileToolContract
from thymira.tools.builtins.output_bounds import MAX_READ_BYTES
from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.builtins.search import GLOB_CAP, GREP_CAP

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.tools.models import ToolResult


class _ContractedTool(Protocol):
    """The subset of a file tool this module needs: real `Tool`, plus its `contract`."""

    name: str
    contract: FileToolContract
    arguments_model: type[Any] | None

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult: ...


_FILE_TOOL_NAMES = ("read_file", "write_file", "edit_file", "glob", "grep", "list_files")

_FORBIDDEN_MODULES = frozenset(
    {"socket", "urllib", "http", "requests", "httpx", "subprocess", "asyncio.subprocess"}
)


def _invocation(tmp_path: Path) -> ToolInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def _snapshot(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }


def _tool_by_name(name: str) -> _ContractedTool:
    """A file tool from a fresh, standalone registry (its own, unshared ReadLedger)."""
    registry = builtins_registry()
    return cast("_ContractedTool", registry.get(name))


def _check_paging(tool: _ContractedTool, contract: FileToolContract) -> None:
    model = tool.arguments_model
    fields = set(model.model_fields) if model is not None else set()
    observed = {"offset", "limit"} <= fields
    assert observed == contract.paging, f"{tool.name}: paging declared {contract.paging}"


def _check_read_file(tmp_path: Path) -> None:
    tool = _tool_by_name("read_file")
    contract = tool.contract
    invocation = _invocation(tmp_path)
    before = _snapshot(invocation.workspace)
    raw_content = "line one\nline two\n"
    (invocation.workspace / "a.txt").write_text(raw_content, encoding="utf-8")

    whole = tool.execute(invocation, {"path": "a.txt"})
    assert whole.stdout == raw_content, "line_numbering must be 'none': no added prefix"

    oversized = "x" * (MAX_READ_BYTES + 1000)
    (invocation.workspace / "big.txt").write_text(oversized, encoding="utf-8")
    bounded = tool.execute(invocation, {"path": "big.txt"})
    assert contract.bounded_output
    assert "bounded" in bounded.stdout or "truncated" in bounded.stdout

    windowed = tool.execute(invocation, {"path": "a.txt", "offset": 1, "limit": 1})
    assert contract.reports_total
    assert "of 2" in windowed.stdout

    assert not contract.discovery_artifact
    assert whole.artifact_ids == ()
    assert not contract.mutates_workspace
    after = _snapshot(invocation.workspace)
    assert {k: v for k, v in after.items() if k in before} == before
    assert not contract.requires_prior_read  # a bare read with nothing read first just worked
    _check_paging(tool, contract)


def _check_write_file(tmp_path: Path) -> None:
    tool = _tool_by_name("write_file")
    contract = tool.contract
    invocation = _invocation(tmp_path)
    before = _snapshot(invocation.workspace)

    result = tool.execute(
        invocation, {"path": "new.txt", "content": "hello", "description": "Write it"}
    )
    assert result.success
    assert contract.mutates_workspace
    after = _snapshot(invocation.workspace)
    assert after != before

    assert not contract.paging
    assert not contract.reports_total
    assert not contract.bounded_output
    assert not contract.discovery_artifact

    assert contract.requires_prior_read
    (invocation.workspace / "existing.txt").write_text("old", encoding="utf-8")
    from thymira.tools.models import ToolExecutionError

    try:
        tool.execute(
            invocation, {"path": "existing.txt", "content": "new", "description": "Overwrite"}
        )
    except ToolExecutionError:
        pass
    else:
        raise AssertionError("write_file over an unread existing file must be refused")


def _check_edit_file(tmp_path: Path) -> None:
    # One shared registry: edit_file's freshness check reads the same ReadLedger read_file
    # writes into, and two independently built registries would each mint their own.
    registry = builtins_registry()
    tool = cast("_ContractedTool", registry.get("edit_file"))
    contract = tool.contract
    invocation = _invocation(tmp_path)
    read_tool = cast("_ContractedTool", registry.get("read_file"))
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert contract.requires_prior_read
    from thymira.tools.models import ToolExecutionError

    try:
        tool.execute(
            invocation,
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
        )
    except ToolExecutionError:
        pass
    else:
        raise AssertionError("edit_file on an unread file must be refused")

    read_tool.execute(invocation, {"path": "a.py"})
    before = _snapshot(invocation.workspace)
    result = tool.execute(
        invocation,
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
    )
    assert result.success
    assert contract.mutates_workspace
    after = _snapshot(invocation.workspace)
    assert after != before
    assert not contract.paging
    assert not contract.bounded_output
    assert not contract.discovery_artifact


def _check_glob(tmp_path: Path) -> None:
    tool = _tool_by_name("glob")
    contract = tool.contract
    invocation = _invocation(tmp_path)
    for i in range(GLOB_CAP + 5):
        (invocation.workspace / f"f{i:04d}.txt").write_text("", encoding="utf-8")

    before = _snapshot(invocation.workspace)
    result = tool.execute(invocation, {"pattern": "*.txt"})
    after = _snapshot(invocation.workspace)

    assert not contract.mutates_workspace
    assert after == before
    assert contract.reports_total
    assert f"of {GLOB_CAP + 5}" in result.stdout
    assert contract.bounded_output
    assert len(result.stdout.splitlines()) < GLOB_CAP + 5 + 1
    assert contract.discovery_artifact
    assert len(result.artifact_ids) == 1
    assert not contract.paging
    assert not contract.requires_prior_read


def _check_grep(tmp_path: Path) -> None:
    tool = _tool_by_name("grep")
    contract = tool.contract
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("hit\n" * (GREP_CAP + 5), encoding="utf-8")

    before = _snapshot(invocation.workspace)
    result = tool.execute(invocation, {"pattern": "hit"})
    after = _snapshot(invocation.workspace)

    assert not contract.mutates_workspace
    assert after == before
    assert contract.reports_total
    assert f"of {GREP_CAP + 5}" in result.stdout
    assert contract.bounded_output
    assert contract.discovery_artifact
    assert len(result.artifact_ids) == 1
    assert not contract.paging
    assert not contract.requires_prior_read


def _check_list_files(tmp_path: Path) -> None:
    tool = _tool_by_name("list_files")
    contract = tool.contract
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")
    (invocation.workspace / "b.txt").write_text("", encoding="utf-8")

    before = _snapshot(invocation.workspace)
    result = tool.execute(invocation, {"path": "."})
    after = _snapshot(invocation.workspace)

    assert not contract.mutates_workspace
    assert after == before
    assert contract.reports_total
    assert '"total": 2' in result.stdout
    assert contract.discovery_artifact
    assert len(result.artifact_ids) == 1
    assert not contract.paging
    assert not contract.requires_prior_read


_CHECKS = {
    "read_file": _check_read_file,
    "write_file": _check_write_file,
    "edit_file": _check_edit_file,
    "glob": _check_glob,
    "grep": _check_grep,
    "list_files": _check_list_files,
}


def test_every_file_tool_declares_a_contract_that_matches_what_it_does(tmp_path: Path) -> None:
    for name in _FILE_TOOL_NAMES:
        subdir = tmp_path / name
        subdir.mkdir()
        _CHECKS[name](subdir)


def test_file_tools_never_execute_code_or_touch_the_network() -> None:
    """Walk the module AST rather than trust the declared ``executes_code``/``network`` bits."""
    for module in (files_module, search_module):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = imported & {name.split(".")[0] for name in _FORBIDDEN_MODULES}
        assert not forbidden, f"{module.__name__} imports {forbidden}"

    registry = builtins_registry()
    for name in _FILE_TOOL_NAMES:
        tool = cast("_ContractedTool", registry.get(name))
        assert tool.contract.executes_code is False
        assert tool.contract.network is False


def test_a_new_file_tool_cannot_be_registered_without_a_contract() -> None:
    """Every exported ``Tool``-shaped dataclass in files.py/search.py carries a contract."""
    checked = 0
    for module in (files_module, search_module):
        for attr_name in module.__all__:
            attr = getattr(module, attr_name)
            if not (inspect.isclass(attr) and hasattr(attr, "execute")):
                continue
            instance = attr()
            assert hasattr(instance, "contract"), f"{attr_name} has no declared contract"
            assert isinstance(instance.contract, FileToolContract)
            checked += 1
    assert checked == len(_FILE_TOOL_NAMES), "expected exactly the six file tools to be checked"


# The production registry is the inventory. Keep this explicit empty set so the test fails with
# the exact newly introduced tool name if an effectful registration loses its required rationale.
_EFFECTFUL_TOOLS_WITHOUT_REQUIRED_DESCRIPTION: frozenset[str] = frozenset()


def test_every_effectful_tool_requires_a_call_description() -> None:
    registry = builtins_registry()
    offenders: set[str] = set()
    for tool in registry:
        if not tool.capability.side_effects:
            continue
        model = tool.arguments_model
        required: set[str] = set()
        if model is not None:
            required = {
                field_name for field_name, info in model.model_fields.items() if info.is_required()
            }
        if "description" not in required:
            offenders.add(tool.name)
    assert offenders == _EFFECTFUL_TOOLS_WITHOUT_REQUIRED_DESCRIPTION
