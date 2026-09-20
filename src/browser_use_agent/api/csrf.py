"""Host and Origin (CSRF) hardening for the controller API.

Trusted hosts and origins are derived from the Traefik public hostname
(``browser-use.${DOCKER_DOMAIN}``). Origin checks apply to unsafe HTTP methods
and WebSocket upgrades when ``AUTH_REQUIRED=true`` and trusted origins are
configured — mitigating cross-site requests that would otherwise ride an
Authelia session cookie through Traefik forward-auth.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from fastapi import WebSocket
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# Methods that can change server state (CSRF-relevant for cookie sessions).
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _normalize_origin(value: str) -> str | None:
    """Normalize an Origin or absolute URL to ``scheme://host[:port]``.

    Args:
        value: Origin header or Referer URL.

    Returns:
        Canonical origin, or ``None`` when unparsable.
    """
    stripped = value.strip()
    if not stripped or stripped == "null":
        return None
    parsed = urlparse(stripped)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def origin_is_trusted(origin_header: str | None, trusted: tuple[str, ...]) -> bool:
    """Return whether an Origin header matches a trusted origin.

    Args:
        origin_header: Raw ``Origin`` value, or ``None``.
        trusted: Allowed origins (``https://host`` form).

    Returns:
        True when the origin is present and listed in ``trusted``.
    """
    if not trusted:
        return True
    normalized = _normalize_origin(origin_header) if origin_header else None
    if normalized is None:
        return False
    allowed = {_normalize_origin(item) for item in trusted}
    allowed.discard(None)
    return normalized in allowed


def referer_origin_is_trusted(referer: str | None, trusted: tuple[str, ...]) -> bool:
    """Return whether a Referer URL's origin matches the trusted list.

    Args:
        referer: Raw ``Referer`` header, or ``None``.
        trusted: Allowed origins.

    Returns:
        True when Referer parses to a trusted origin.
    """
    if not trusted or not referer:
        return False
    return origin_is_trusted(referer, trusted)


def check_csrf_headers(
    *,
    method: str,
    origin: str | None,
    referer: str | None,
    trusted_origins: tuple[str, ...],
    enforce: bool,
) -> bool:
    """Validate Origin/Referer for unsafe methods when enforcement is on.

    Safe methods (GET/HEAD/OPTIONS) always pass. When ``enforce`` is false or
    ``trusted_origins`` is empty, checks are skipped. Otherwise require a
    matching ``Origin``, falling back to ``Referer`` when Origin is absent
    (some older clients).

    Args:
        method: HTTP method.
        origin: ``Origin`` header value.
        referer: ``Referer`` header value.
        trusted_origins: Allowed browser origins.
        enforce: When false, always allow.

    Returns:
        True when the request may proceed.
    """
    if not enforce or not trusted_origins:
        return True
    if method.upper() not in UNSAFE_METHODS:
        return True
    if origin_is_trusted(origin, trusted_origins):
        return True
    if origin is None and referer_origin_is_trusted(referer, trusted_origins):
        return True
    return False


def check_websocket_origin(
    websocket: WebSocket,
    *,
    trusted_origins: tuple[str, ...],
    enforce: bool,
) -> bool:
    """Validate the WebSocket upgrade ``Origin`` when enforcement is on.

    Missing Origin is allowed so non-browser clients (tests, scripts) can
    connect with ``Remote-User``; browsers always send Origin.

    Args:
        websocket: Incoming WebSocket (not yet accepted).
        trusted_origins: Allowed browser origins.
        enforce: When false or trusted list empty, always allow.

    Returns:
        True when the upgrade may proceed.
    """
    if not enforce or not trusted_origins:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return origin_is_trusted(origin, trusted_origins)


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Reject unsafe HTTP requests whose Origin/Referer is not trusted."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_origins: tuple[str, ...],
        enforce: bool,
    ) -> None:
        """Initialize middleware.

        Args:
            app: Downstream ASGI app.
            trusted_origins: Allowed ``https://host`` origins.
            enforce: Enable checks (typically when ``AUTH_REQUIRED=true``).
        """
        super().__init__(app)
        self.trusted_origins = trusted_origins
        self.enforce = enforce

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Enforce Origin/Referer on unsafe methods.

        Args:
            request: Incoming HTTP request.
            call_next: Next middleware or route.

        Returns:
            Downstream response, or 403 when the origin is not trusted.
        """
        if not check_csrf_headers(
            method=request.method,
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            trusted_origins=self.trusted_origins,
            enforce=self.enforce,
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin not trusted"},
            )
        return await call_next(request)
