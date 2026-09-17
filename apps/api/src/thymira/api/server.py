"""Local development server for the Thymira MVP."""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from thymira.api.app import create_app
from thymira.api.credential import (
    API_TOKEN_ENV_VAR,
    ApiCredentialError,
    resolve_process_credential,
)
from thymira.api.deps import build_default_deps
from thymira.api.env import EnvFileError, load_env_file

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI

    from thymira.api.permissions import PrincipalResolver

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 5
_MAX_PORT = 65535
_LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


def local_state_root(workspace: Path, state_root: Path | None = None) -> Path:
    """Return the runtime state directory this workspace's local API writes under."""
    if state_root is not None:
        return Path(state_root).resolve()
    return Path(workspace).resolve() / ".thymira" / "runtime"


def build_local_app(
    workspace: Path,
    *,
    state_root: Path | None = None,
    principal_resolver: PrincipalResolver | None = None,
) -> FastAPI:
    """Build the local MVP API with JSON persistence and inline dispatch.

    A credential is always configured. When the caller supplies no resolver this establishes the
    process credential itself -- an exported ``THYMIRA_API_TOKEN``, or a freshly minted token
    written to ``<state_root>/api-token`` -- so no path through this entry point can serve an
    anonymous caller (F13.3).
    """
    workspace_path = Path(workspace).resolve()
    root = local_state_root(workspace_path, state_root)
    resolver = principal_resolver
    if resolver is None:
        resolver, _ = resolve_process_credential(root)
    return create_app(
        build_default_deps(root, workspace=workspace_path, principal_resolver=resolver)
    )


def _port(value: str) -> int:
    """Parse a TCP port and reject values outside the valid user-port range."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the local API server."""
    parser = argparse.ArgumentParser(
        prog="thymira-api",
        description="Run the local Thymira MVP API for a configured workspace.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Project workspace containing .thymira/config.yaml.",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help="Directory for local runtime state (default: <workspace>/.thymira/runtime).",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST, help="Bind host (default: 127.0.0.1).")
    parser.add_argument(
        "--port",
        default=_DEFAULT_PORT,
        type=_port,
        help="Bind TCP port (default: 8000).",
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="info",
        help="Uvicorn log level (default: info).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Env file to load before starting (default: the nearest .env at or above the "
            "working directory, or $THYMIRA_ENV_FILE). Variables already set in the environment "
            "are never overridden."
        ),
    )
    parser.add_argument(
        "--replace-credential",
        action="store_true",
        help=(
            "Replace an API token file a stopped or crashed server left behind, instead of "
            "refusing to start. The default stays fail-closed so a second server cannot "
            "accidentally invalidate a live one; pass this only as a deliberate restart recovery."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local API server and return its process exit status.

    The env file is loaded here rather than in `create_app`, and before the app is built: the
    composition root reads `THYMIRA_*` while it wires the runtime, so a variable that arrived
    after that point would be read too late.

    A clean exit -- uvicorn returning normally, or a ``KeyboardInterrupt`` -- removes the token
    this process minted (never a token that came from ``$THYMIRA_API_TOKEN``, for which there is
    no path to remove), so a later start with no leftover file needs no operator recovery. A
    crash still leaves the file behind; ``--replace-credential`` is the documented recovery for
    that case.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        loaded = load_env_file(args.env_file)
    except EnvFileError as exc:
        parser.error(str(exc))
    if loaded is not None:
        # A console script announcing which file it read; the path only, never a value.
        # `flush` is load-bearing: stdout is block-buffered when it is a pipe (a log file, a
        # container's collector), and this process then runs until it is killed, so without it
        # the one line that says whether the env file was found never appears.
        print(f"loaded environment from {loaded}", flush=True)  # noqa: T201  # start-up feedback
    root = local_state_root(args.workspace, args.state_root)
    try:
        credential, token_path = resolve_process_credential(
            root, replace_existing=args.replace_credential
        )
    except (ApiCredentialError, OSError) as exc:
        parser.error(f"cannot establish the API credential: {exc}")
    if token_path is None:
        message = f"API authentication uses ${API_TOKEN_ENV_VAR}"
    else:
        message = f"minted an API token; read it from {token_path}"
    # The path only, never a value: this line reaches a console, a log file and a container's
    # collector, and the token must reach none of them.
    print(message, flush=True)  # noqa: T201  # start-up feedback
    try:
        app = build_local_app(
            args.workspace,
            state_root=args.state_root,
            principal_resolver=credential,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(f"cannot configure local API: {exc}")
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
        )
    finally:
        _remove_minted_token(token_path)
    return 0


def _remove_minted_token(token_path: Path | None) -> None:
    """Remove the token file this process minted so a later start finds nothing left behind.

    Only the exact path :func:`resolve_process_credential` returned is ever removed, and only
    when it minted one: ``None`` means the credential came from ``$THYMIRA_API_TOKEN``, and there
    is nothing on disk to clean up. Best-effort -- a removal failure must not turn a clean shutdown
    into a crash -- so an ``OSError`` here is swallowed; a token a later start cannot then reuse
    without ``--replace-credential`` is exactly the documented recovery path.
    """
    if token_path is None:
        return
    with contextlib.suppress(OSError):
        token_path.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - exercised through the console script.
    raise SystemExit(main())


__all__ = ["build_local_app", "local_state_root", "main"]
