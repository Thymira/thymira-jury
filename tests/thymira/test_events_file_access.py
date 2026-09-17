"""Tests for the events-owned private-file publication seam."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.events import create_private_file

if TYPE_CHECKING:
    from pathlib import Path


def test_create_private_file_secures_before_publishing_bytes(tmp_path: Path) -> None:
    """An exclusive file is private before its caller-provided bytes are written."""
    path = tmp_path / "api-token"

    evidence = create_private_file(path, b"token-value")

    assert path.read_bytes() == b"token-value"
    assert evidence.path == path
    if evidence.platform != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_create_private_file_refuses_overwrite(tmp_path: Path) -> None:
    """A publication target cannot silently replace an existing credential file."""
    path = tmp_path / "api-token"
    path.write_bytes(b"original")

    with pytest.raises(FileExistsError):
        create_private_file(path, b"replacement")

    assert path.read_bytes() == b"original"
