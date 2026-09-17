"""Load reviewed MIRA preflight packs from local JSON data."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from thymira.mira.preflight.models import ReviewedPack

DEFAULTS_PACKAGE = "thymira.mira.preflight.defaults"


def pack_from_dict(data: dict[str, Any]) -> ReviewedPack:
    """Validate a parsed reviewed-pack mapping."""
    return ReviewedPack.model_validate(data)


def load_pack(path: Path) -> ReviewedPack:
    """Load one reviewed pack from a JSON file."""
    if Path(path).suffix.lower() != ".json":
        msg = f"unsupported pack file type: {Path(path).suffix!r} (use .json)"
        raise ValueError(msg)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"{path}: a pack file must contain a mapping at the top level"
        raise TypeError(msg)
    return pack_from_dict(data)


def load_default_pack(name: str) -> ReviewedPack:
    """Load one packaged reviewed pack by its stable name."""
    resource = resources.files(DEFAULTS_PACKAGE).joinpath(f"{name}.json")
    if not resource.is_file():
        raise FileNotFoundError(f"no packaged MIRA pack named {name!r}")
    data = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"pack {name!r} must contain a mapping at the top level")
    return pack_from_dict(data)


def load_default_packs() -> tuple[ReviewedPack, ...]:
    """Load the bounded methodology and credit-governance pack set."""
    return (
        load_default_pack("methodology-base"),
        load_default_pack("credit-governance"),
    )
