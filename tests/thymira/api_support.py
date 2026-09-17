"""Shared helpers for tests that drive the authenticated Thymira API.

Every API composition needs a :class:`~thymira.api.permissions.PrincipalResolver`: the permissive
mode F13.3 forbids no longer exists, so a test builds a real credential and authenticates its
client instead of disabling the control. A suite that disabled authentication would prove nothing
about it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from thymira.api.credential import ProcessCredential, mint_api_token
from thymira.api.permissions import principal_for_role

if TYPE_CHECKING:
    from fastapi import FastAPI

DEFAULT_ACTOR_ID = "tester"
DEFAULT_ROLE = "admin"


def api_credential(
    role: str = DEFAULT_ROLE,
    actor_id: str = DEFAULT_ACTOR_ID,
) -> tuple[str, ProcessCredential]:
    """Mint a real per-process credential for one role, and return its token beside it."""
    token = mint_api_token()
    return token, ProcessCredential(token, principal_for_role(actor_id, role))


TEST_TOKEN, TEST_CREDENTIAL = api_credential()
"""One shared admin credential for the modules that do not test identity separation.

Minted once per test session, exactly the way the server mints its own; the modules that *do*
test identity separation (``test_api_auth.py``) build their own multi-principal resolver instead.
"""


def bearer(token: str) -> dict[str, str]:
    """Build the Authorization header carrying a bearer token."""
    return {"Authorization": f"Bearer {token}"}


def authenticated_client(app: FastAPI, token: str = TEST_TOKEN) -> TestClient:
    """Build a TestClient whose every request carries ``token``.

    ``TestClient`` merges these default headers into each request, including the ``stream()``
    used by the SSE follow path, so one construction authenticates a whole module.
    """
    return TestClient(app, headers=bearer(token))


__all__ = [
    "DEFAULT_ACTOR_ID",
    "DEFAULT_ROLE",
    "TEST_CREDENTIAL",
    "TEST_TOKEN",
    "api_credential",
    "authenticated_client",
    "bearer",
]
