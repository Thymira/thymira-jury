"""Fail-closed redaction at the API's JSON response boundary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from thymira.events import ExportRedactionError, canonical_json, redact_export

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_GUARDED_STREAM_HEADER = b"x-thymira-redacted-stream"
_GUARDED_STREAM_VALUE = b"event-projection-v1"


class ExportRedactionMiddleware:
    """Redact JSON responses and reject every unclassified HTTP surface.

    The one permitted non-JSON response is the events route's ``text/event-stream`` carrying the
    ``x-thymira-redacted-stream: event-projection-v1`` sentinel. That route calls its projection
    redactor for each frame. A route cannot bypass this boundary by changing a response's media
    type: every other HTTP response is replaced with a generic failure before its body is forwarded.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer JSON safely, pass the one guarded stream through, and fail closed otherwise."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []
        capture = False
        empty_status = False
        discard = False

        async def capture_send(message: Message) -> None:
            nonlocal capture, discard, empty_status, start
            if message["type"] == "http.response.start":
                start = message
                mode = _response_mode(message)
                if mode == "stream":
                    await send(message)
                elif mode in {"json", "empty"}:
                    capture = True
                    empty_status = mode == "empty"
                else:
                    discard = True
                    await _send_failure(send)
                return
            if discard:
                return
            if message["type"] != "http.response.body" or not capture or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                body = b"".join(chunks)
                if empty_status:
                    if body or _declared_content_length(start) not in (None, 0):
                        await _send_failure(send)
                    else:
                        await send(start)
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await self._finish_json(start, body, send)

        await self._app(scope, receive, capture_send)

    async def _finish_json(self, start: Message, body: bytes, send: Send) -> None:
        """Redact one buffered response or return a generic failure without its source bytes."""
        try:
            safe = redact_export(json.loads(body))
            rendered = canonical_json(safe).encode("utf-8")
            await send(_with_content_length(start, rendered))
            await send({"type": "http.response.body", "body": rendered, "more_body": False})
        except (
            ExportRedactionError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ):
            failure = canonical_json(
                {
                    "code": "export_redaction_failed",
                    "message": "The response could not be safely redacted.",
                    "details": {},
                }
            ).encode("utf-8")
            await _send_failure(send, failure)


async def _send_failure(send: Send, body: bytes | None = None) -> None:
    """Send the generic response used for unsupported or unsafe export surfaces."""
    if body is None:
        body = canonical_json(
            {
                "code": "export_redaction_failed",
                "message": "The response could not be safely redacted.",
                "details": {},
            }
        ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _response_mode(message: Message) -> str:
    """Classify an HTTP response as JSON, an explicitly guarded stream, or unsafe."""
    status = message.get("status", 200)
    if status in (204, 304):
        return "empty"
    headers = {key.lower(): value for key, value in message.get("headers", [])}
    content_type = headers.get(b"content-type", b"").split(b";", 1)[0].lower()
    if content_type == b"application/json" or content_type.endswith(b"+json"):
        return "json"
    if (
        content_type == b"text/event-stream"
        and headers.get(_GUARDED_STREAM_HEADER) == _GUARDED_STREAM_VALUE
    ):
        return "stream"
    return "reject"


def _declared_content_length(message: Message) -> int | None:
    """Return a response's declared byte length, rejecting malformed declarations."""
    raw = next(
        (value for key, value in message.get("headers", []) if key.lower() == b"content-length"),
        None,
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return -1
    return value if value >= 0 else -1


def _with_content_length(start: Message, body: bytes) -> Message:
    """Replace a transformed response's stale content length with its safe length."""
    headers = [
        (key, value)
        for key, value in start.get("headers", [])
        if key.lower() not in (b"content-length", b"transfer-encoding")
    ]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    return {**start, "headers": headers}


__all__ = ["ExportRedactionMiddleware"]
