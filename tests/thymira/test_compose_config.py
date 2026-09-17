"""compose.yaml must never inject a whole project `.env` file into the runtime container.

`env_file:` hands Docker the raw file before either Python loader (`thymira.api.env`,
`thymira.cli.env`) ever runs, so their PATH/LD_PRELOAD/THYMIRA_SANDBOX_* refusals never see it and
never fire -- a checked-in `.env` could steer the in-container interpreter or its sandbox backend
regardless of what the loaders otherwise refuse (F6.2). This is the third producer of that
guarantee, alongside the two loaders exercised in `test_api_env.py` / `test_cli_env.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "compose.yaml"

_BOOTSTRAP_LIKE_PREFIXES = ("THYMIRA_SANDBOX_",)
_BOOTSTRAP_LIKE_NAMES = ("PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH")


def _load_services() -> dict[str, Any]:
    document = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    return document["services"]


def test_no_service_bulk_loads_the_project_env_file() -> None:
    """`env_file:` would hand Docker the whole `.env` before either Python loader runs."""
    services = _load_services()

    for name, service in services.items():
        assert "env_file" not in service, (
            f"service {name!r} uses env_file, bypassing thymira.api.env / thymira.cli.env's "
            "PATH/LD_PRELOAD/THYMIRA_SANDBOX_* refusals entirely"
        )


def test_every_forwarded_variable_is_named_explicitly() -> None:
    """Only variables compose.yaml names by name may ever reach the container's environment."""
    services = _load_services()

    for name, service in services.items():
        environment = service.get("environment", [])
        forwarded_names = {entry.split("=", 1)[0] for entry in environment}
        blocked = {
            forwarded
            for forwarded in forwarded_names
            if forwarded in _BOOTSTRAP_LIKE_NAMES
            or any(forwarded.startswith(prefix) for prefix in _BOOTSTRAP_LIKE_PREFIXES)
        }
        assert not blocked, f"service {name!r} explicitly forwards refused name(s): {blocked}"
