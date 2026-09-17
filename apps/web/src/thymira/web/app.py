"""The console application: static assets and a credential-neutral pass-through to the API.

The console is a client in the same sense as the CLI: it owns no Run state and imports no runtime
member, only HTTP. The browser keeps the operator's bearer token and presents it on every call;
this process forwards the ``Authorization`` header exactly as it arrived and never adds, stores or
logs one. That is what keeps F13.3 intact through a second local port: a request that reaches this
server from loopback carries no more authority than the same request sent to the API directly.

Three browser-facing defences surround that rule. A host allowlist refuses a DNS-rebinding page
that points its own name at this address. A state-changing request whose ``Origin`` is another
site is refused before it is forwarded. And every response carries a same-origin
Content-Security-Policy, so the console's own pages can load from and talk to this server only.

Request bodies are streamed through rather than buffered, bounded per route: an ordinary request
is a small JSON object, while a dataset upload may be as large as the runtime will register.
"""

from __future__ import annotations

import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC_DIR = Path(__file__).resolve().parent / "static"
"""The console's HTML, CSS and JavaScript, shipped inside the wheel next to this module."""

API_PREFIX = "/api"
"""Path prefix under which the console forwards requests to the Thymira API."""

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
"""Host header values the console answers by default; anything else is a rebinding attempt."""

MAX_REQUEST_BODY_BYTES = 1024 * 1024
"""Largest ordinary request body forwarded; every API request body is a small JSON object."""

MAX_UPLOAD_BODY_BYTES = 256 * 1024 * 1024
"""Largest dataset upload forwarded; the runtime registers nothing larger."""

FORWARDED_REQUEST_HEADERS = frozenset(
    {"accept", "authorization", "content-type", "idempotency-key", "last-event-id"}
)
"""The only request headers forwarded upstream. Cookies and proxy headers never cross."""

FORWARDED_RESPONSE_HEADERS = frozenset(
    {"content-type", "retry-after", "www-authenticate", "x-thymira-redacted-stream"}
)
"""The only API response headers relayed to the browser."""

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)
"""Same-origin policy for every console response; ``data:`` images carry artifact previews."""

_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
)
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _SECURITY_HEADERS)
_API_ROOTS = frozenset({"healthz", "project", "runs", "settings", "tools"})
_SEGMENT = re.compile(r"[A-Za-z0-9_.~-]+")
_DOT_SEGMENTS = frozenset({".", ".."})
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_FORWARDED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
_BODYLESS_STATUSES = frozenset({204, 304})
_UPLOAD_SEGMENTS = 3
_UPSTREAM_TIMEOUT = httpx.Timeout(30.0, connect=5.0, read=None)

# Windows resolves MIME types from the registry, where ``.js`` is frequently ``text/plain``; a
# module script served that way is refused by the browser under ``nosniff``.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


class _BodyTooLargeError(Exception):
    """Raised inside a streamed request body once it passes its bound."""


class SecurityHeadersMiddleware:
    """Stamp the console's browser security headers onto every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Replace any same-named header with the console's value on each response start."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_HEADER_NAMES
                ]
                headers.extend(_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def validate_api_url(api_url: str) -> str:
    """Return ``api_url`` without a trailing slash when it is a usable API base URL.

    Raises:
        ValueError: The URL is not ``http``/``https``, has no host, or carries a query or
            fragment that every forwarded request would silently inherit.
    """
    try:
        parsed = httpx.URL(api_url)
    except httpx.InvalidURL as exc:
        raise ValueError(str(exc)) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("the API URL must be an http:// or https:// URL with a host")
    if parsed.query or parsed.fragment:
        raise ValueError("the API URL must not carry a query string or fragment")
    return str(parsed).rstrip("/")


def is_forwardable_path(path: str) -> bool:
    """Return whether ``path`` (below ``/api/``) names a Thymira API route the console forwards.

    Only the API's own route roots are forwarded, one plain segment at a time. An empty, ``.`` or
    ``..`` segment, or any character outside the unreserved set, is refused, so a crafted path
    cannot reach a different upstream resource than the one it spells.
    """
    segments = path.split("/")
    if segments[0] not in _API_ROOTS:
        return False
    return all(
        _SEGMENT.fullmatch(segment) is not None and segment not in _DOT_SEGMENTS
        for segment in segments
    )


def request_body_limit(method: str, path: str) -> int:
    """Return the body bound for one forwarded request: large only for a dataset upload."""
    segments = path.split("/")
    is_upload = (
        method == "PUT"
        and len(segments) == _UPLOAD_SEGMENTS
        and segments[0] == "project"
        and segments[1] == "datasets"
    )
    return MAX_UPLOAD_BODY_BYTES if is_upload else MAX_REQUEST_BODY_BYTES


def _problem(status_code: int, code: str, message: str) -> JSONResponse:
    """Answer in the API's own error envelope, so the browser client handles a single shape."""
    return JSONResponse(
        {"code": code, "message": message, "details": {}},
        status_code=status_code,
        media_type="application/problem+json",
    )


def _too_large(limit: int) -> JSONResponse:
    return _problem(413, "request_too_large", f"This request body is limited to {limit} bytes.")


def _forwarded_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep the allowlisted request headers, exactly as the browser sent them."""
    return {
        name: value for name, value in headers.items() if name.lower() in FORWARDED_REQUEST_HEADERS
    }


def _forwarded_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Keep the allowlisted API response headers."""
    return {
        name: value for name, value in headers.items() if name.lower() in FORWARDED_RESPONSE_HEADERS
    }


def _is_cross_origin(request: Request) -> bool:
    """Return whether a request declares an ``Origin`` other than this console's own."""
    origin = request.headers.get("origin")
    return origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}"


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    return int(raw) if raw is not None and raw.isdigit() else None


async def _bounded_body(request: Request, limit: int) -> AsyncIterator[bytes]:
    """Stream the request body upstream, stopping once it passes ``limit``."""
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLargeError
        yield chunk


async def _relay(upstream: httpx.Response) -> Response:
    """Relay the API's status, allowlisted headers and body, streaming it as it arrives."""
    headers = _forwarded_response_headers(upstream.headers)
    if upstream.status_code in _BODYLESS_STATUSES:
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=headers)
    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )


def _refusal(request: Request, path: str, limit: int) -> JSONResponse | None:
    """Return why the console refuses to forward a request before any byte goes upstream."""
    if not is_forwardable_path(path):
        return _problem(404, "not_an_api_route", "The console forwards Thymira API routes only.")
    if request.method not in _SAFE_METHODS and _is_cross_origin(request):
        return _problem(
            403,
            "cross_origin_refused",
            "A request from another origin may not change Run state through the console.",
        )
    declared = _declared_length(request)
    if declared is not None and declared > limit:
        return _too_large(limit)
    return None


async def _forward(request: Request) -> Response:
    """Forward one ``/api/*`` request to the Thymira API without adding any authority."""
    path = request.path_params["path"]
    limit = request_body_limit(request.method, path)
    refusal = _refusal(request, path, limit)
    if refusal is not None:
        return refusal
    client: httpx.AsyncClient = request.app.state.api_client
    content = None if request.method in _SAFE_METHODS else _bounded_body(request, limit)
    upstream_request = client.build_request(
        request.method,
        f"/{path}",
        params=tuple(request.query_params.multi_items()),
        headers=_forwarded_request_headers(request.headers),
        content=content,
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except _BodyTooLargeError:
        return _too_large(limit)
    except httpx.TimeoutException:
        return _problem(504, "api_timeout", "The Thymira API did not answer in time.")
    except httpx.RequestError:
        return _problem(
            502,
            "api_unreachable",
            f"The Thymira API at {client.base_url} is not reachable.",
        )
    return await _relay(upstream)


async def _index(request: Request) -> FileResponse:
    """Serve the console shell; routing inside it is client-side, under the URL fragment."""
    del request
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


def create_app(
    api_url: str,
    *,
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """Build the console for one Thymira API.

    Args:
        api_url: Base URL of the Thymira API every ``/api/*`` request is forwarded to.
        allowed_hosts: ``Host`` header values the console answers; any other is refused.
        transport: Optional httpx transport for the upstream client, for in-process tests.

    Raises:
        ValueError: ``api_url`` is not a usable API base URL (see :func:`validate_api_url`).
    """
    base_url = validate_api_url(api_url)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=False,
        ) as client:
            app.state.api_client = client
            yield

    routes = [
        Route("/", _index, methods=["GET"]),
        Route(f"{API_PREFIX}/{{path:path}}", _forward, methods=_FORWARDED_METHODS),
        Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts)),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


__all__ = [
    "API_PREFIX",
    "CONTENT_SECURITY_POLICY",
    "DEFAULT_ALLOWED_HOSTS",
    "FORWARDED_REQUEST_HEADERS",
    "FORWARDED_RESPONSE_HEADERS",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_UPLOAD_BODY_BYTES",
    "STATIC_DIR",
    "SecurityHeadersMiddleware",
    "create_app",
    "is_forwardable_path",
    "request_body_limit",
    "validate_api_url",
]
