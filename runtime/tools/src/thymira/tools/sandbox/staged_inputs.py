"""Backend staging for runtime-owned subprocess inputs."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from thymira.tools.sandbox.base import StagedInput, validate_staged_inputs

if TYPE_CHECKING:
    from collections.abc import Iterator


def _is_link(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""
    return path.is_symlink() or path.is_junction()


def _assert_parent_chain(root: Path, path: Path) -> None:
    """Refuse links in a host staging path before opening a file for writing."""
    current = path.parent
    root = root.resolve()
    while current != root:
        if _is_link(current):
            raise ValueError(f"staged input path traverses a link: {current.name}")
        if current == current.parent:
            raise ValueError("staged input path escaped its workspace")
        current = current.parent


@contextmanager
def stage_inputs_on_host(
    workspace: Path, inputs: tuple[StagedInput, ...] | list[StagedInput] | None
) -> Iterator[tuple[StagedInput, ...]]:
    """Stage inputs for a backend that intentionally binds the host workspace.

    Quota execution never enters this context: its caller writes an equivalent manifest into a
    private temporary source mount and charges it in the quota volume before the worker starts.
    """
    values = validate_staged_inputs(inputs)
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"sandbox workspace does not exist: {workspace}")
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for item in values:
            target = root.joinpath(*item.relative_path.split("/"))
            _assert_parent_chain(root, target)
            if target.exists() or target.is_symlink():
                raise ValueError(
                    f"staged input collides with an existing workspace path: {item.relative_path}"
                )
            missing: list[Path] = []
            parent = target.parent
            while parent != root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            if parent != root:
                _assert_parent_chain(root, target)
            for directory in reversed(missing):
                directory.mkdir()
                created_dirs.append(directory)
            with target.open("xb") as handle:
                handle.write(item.content)
            created_files.append(target)
        yield values
    finally:
        for target in reversed(created_files):
            with suppress(OSError):
                target.unlink(missing_ok=True)
        for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
            with suppress(OSError):
                directory.rmdir()


__all__ = ["stage_inputs_on_host"]
