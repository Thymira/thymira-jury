"""Thymira web console: a browser client of the Thymira API that owns no Run state.

``create_app`` serves the console's static assets and forwards ``/api/*`` to one configured API
without adding a credential; the ``thymira-web`` console script runs it locally.
"""

from thymira.web.app import (
    API_PREFIX,
    CONTENT_SECURITY_POLICY,
    FORWARDED_REQUEST_HEADERS,
    FORWARDED_RESPONSE_HEADERS,
    create_app,
    is_forwardable_path,
    validate_api_url,
)

__all__ = [
    "API_PREFIX",
    "CONTENT_SECURITY_POLICY",
    "FORWARDED_REQUEST_HEADERS",
    "FORWARDED_RESPONSE_HEADERS",
    "create_app",
    "is_forwardable_path",
    "validate_api_url",
]
