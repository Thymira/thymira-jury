"""Helpers for testing repository scripts that are not importable packages."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script(relative_path: str) -> ModuleType:
    """Import a script file (e.g. 'scripts/ty_ratchet.py') as a module."""
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None, path
    assert spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
