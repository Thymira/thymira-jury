"""Portable tool commands must reject paths outside the mounted workspace."""

from __future__ import annotations

import pytest

from thymira.tools.builtins.subprocess_command import (
    python_workspace_command,
    workspace_relative_path,
)


@pytest.mark.parametrize("build", [python_workspace_command, workspace_relative_path])
def test_subprocess_paths_cannot_traverse_outside_workspace(tmp_path, build):
    workspace = tmp_path / "workspace"
    outside = workspace / ".." / "outside.py"

    with pytest.raises(ValueError, match="contained in the workspace"):
        build(workspace, outside)
