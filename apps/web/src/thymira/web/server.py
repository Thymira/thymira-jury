"""``thymira-web``: serve the local web console in front of one running Thymira API."""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

import uvicorn

from thymira.web.app import DEFAULT_ALLOWED_HOSTS, create_app, validate_api_url

if TYPE_CHECKING:
    from collections.abc import Sequence

API_URL_ENV_VAR = "THYMIRA_API_URL"
"""The same variable the CLI reads to find the API."""

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
_MAX_PORT = 65535
_LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})  # noqa: S104  # recognised, never bound here


def allowed_hosts_for(bind_host: str) -> tuple[str, ...]:
    """Return the ``Host`` header values the console answers when bound to ``bind_host``.

    Loopback names are always answered. A concrete bind address is added so the console is
    reachable under the name it was started with; a wildcard bind adds nothing, because the
    console cannot know which of the machine's names a legitimate browser will use.
    """
    hosts = list(DEFAULT_ALLOWED_HOSTS)
    if bind_host not in _WILDCARD_HOSTS and bind_host not in hosts:
        hosts.append(bind_host)
    return tuple(hosts)


def _port(value: str) -> int:
    """Parse a TCP port and reject values outside the valid range."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError(f"port must be between 1 and {_MAX_PORT}")
    return port


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the web console."""
    parser = argparse.ArgumentParser(
        prog="thymira-web",
        description=(
            "Serve the Thymira web console in front of a running Thymira API. The console "
            "stores no credential: the browser asks for the API token and sends it itself."
        ),
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get(API_URL_ENV_VAR, DEFAULT_API_URL),
        help=f"Thymira API base URL (default: ${API_URL_ENV_VAR} or {DEFAULT_API_URL}).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})."
    )
    parser.add_argument(
        "--port", type=_port, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})."
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default="info",
        help="Uvicorn log level (default: info).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the web console and return its process exit status."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        api_url = validate_api_url(args.api_url)
    except ValueError as exc:
        parser.error(f"invalid --api-url: {exc}")
    app = create_app(api_url, allowed_hosts=allowed_hosts_for(args.host))
    print(  # noqa: T201  # start-up feedback from a console script
        f"Thymira console on http://{args.host}:{args.port} (API {api_url})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console script.
    raise SystemExit(main())


__all__ = ["API_URL_ENV_VAR", "DEFAULT_API_URL", "allowed_hosts_for", "main"]
