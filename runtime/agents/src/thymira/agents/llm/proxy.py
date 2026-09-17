"""Runtime-owned resolution for a LiteLLM endpoint.

An endpoint is an operator setting, not model or project content.  The caller must declare the
exact endpoints it permits before one can be selected.  This small functional seam is shared by
the installed provider factories and the manually injected ``routed_model`` path. Direct dispatch
is permitted only when LiteLLM cannot inherit an ambient endpoint override; an invalid explicit
endpoint raises immediately.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from thymira.agents.llm.base import LLMConfigurationError

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

_AMBIENT_ENDPOINT_ENV_VARS = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
"""LiteLLM environment overrides that can replace a direct OpenAI destination."""


def resolve_proxy_endpoint(
    proxy_endpoint: str | None,
    *,
    declared_safe_endpoints: Collection[str] = (),
    litellm_sdk: Any | None = None,
) -> str | None:
    """Resolve one exact endpoint from a runtime-declared allowlist.

    ``None`` means that the caller requested direct LiteLLM dispatch. A direct call is permitted
    only when LiteLLM has no ambient endpoint override; otherwise the mutable SDK global or
    ``OPENAI_*`` environment values would bypass this boundary. A present value is never
    substituted, normalised or replaced with a default: it must be an exact member of
    ``declared_safe_endpoints`` and use HTTPS (or HTTP on the loopback interface for a local
    gateway). Exact matching keeps a caller from changing the destination through a path, query,
    or spelling variant that was not declared by the runtime.

    Raises:
        LLMConfigurationError: If an explicit endpoint is empty, not declared, or not a safe URL,
            or if direct dispatch would inherit an ambient endpoint override.
    """
    if proxy_endpoint is None:
        if any(os.getenv(variable, "").strip() for variable in _AMBIENT_ENDPOINT_ENV_VARS):
            raise LLMConfigurationError(
                "direct LLM dispatch refuses ambient OPENAI endpoint configuration; "
                "pass an explicitly declared proxy endpoint"
            )
        if litellm_sdk is not None and getattr(litellm_sdk, "api_base", None):
            raise LLMConfigurationError(
                "direct LLM dispatch refuses the mutable LiteLLM api_base; "
                "pass an explicitly declared proxy endpoint"
            )
        return None
    if (
        not proxy_endpoint
        or proxy_endpoint != proxy_endpoint.strip()
        or any(character.isspace() for character in proxy_endpoint)
    ):
        raise LLMConfigurationError("the explicit LLM proxy endpoint must not contain whitespace")
    if proxy_endpoint not in declared_safe_endpoints:
        raise LLMConfigurationError(
            "the explicit LLM proxy endpoint is not in the runtime-declared safe endpoint set"
        )

    try:
        parsed = urlsplit(proxy_endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise LLMConfigurationError("the explicit LLM proxy endpoint is not a valid URL") from exc

    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is None and parsed.netloc.endswith(":"))
    ):
        raise LLMConfigurationError(
            "the explicit LLM proxy endpoint must have no credentials, query, or fragment"
        )
    if parsed.scheme == "https":
        return proxy_endpoint
    if parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}:
        return proxy_endpoint
    raise LLMConfigurationError(
        "the explicit LLM proxy endpoint must use HTTPS; HTTP is allowed only on loopback"
    )


def with_proxy_endpoint(kwargs: Mapping[str, Any], proxy_endpoint: str | None) -> dict[str, Any]:
    """Return LiteLLM call kwargs with the resolved endpoint, when one was selected."""
    resolved = dict(kwargs)
    if proxy_endpoint is not None:
        resolved["api_base"] = proxy_endpoint
    return resolved


__all__ = ["resolve_proxy_endpoint", "with_proxy_endpoint"]
