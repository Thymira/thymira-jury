"""Load policies from YAML/JSON files or from the packaged defaults."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from thymira.policies.models import Policy

DEFAULTS_PACKAGE = "thymira.policies.defaults"


def policy_from_dict(data: dict[str, Any]) -> Policy:
    """Validate a plain mapping (parsed YAML/JSON) into a :class:`Policy`."""
    return Policy.model_validate(data)


def load_policy(path: Path) -> Policy:
    """Load a policy file; ``.yaml``/``.yml`` and ``.json`` are supported."""
    suffix = Path(path).suffix.lower()
    if suffix not in {".yaml", ".yml", ".json"}:
        msg = f"unsupported policy file type: {suffix!r} (use .yaml, .yml or .json)"
        raise ValueError(msg)
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        msg = f"{path}: a policy file must contain a mapping at the top level"
        raise TypeError(msg)
    return policy_from_dict(data)


def load_default_policy(name: str = "base") -> Policy:
    """Load one of the packaged policies (``base``, ``credit_risk``)."""
    resource = resources.files(DEFAULTS_PACKAGE).joinpath(f"{name}.yaml")
    if not resource.is_file():
        msg = f"no packaged policy named {name!r}"
        raise FileNotFoundError(msg)
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    return policy_from_dict(data)


def load_policy_stack(*names: str) -> Policy:
    """Load ``base`` and merge the named overlays on top, in order (later overlays first)."""
    policy = load_default_policy("base")
    for name in names:
        policy = policy.merged_with(load_default_policy(name))
    return policy
