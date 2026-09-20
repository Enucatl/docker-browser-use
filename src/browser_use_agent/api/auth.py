"""Authelia ``Remote-User`` trust and request identity for the controller API.

Identity headers (``Remote-User``, ``Remote-Groups``, ``Remote-Name``,
``Remote-Email``) are injected by Traefik after Authelia forward-auth. They are
spoofable if a client can reach the controller without Traefik; production
relies on Compose keeping the public path Authelia-gated (``ports: []``,
``traefik_proxy`` only via the labeled router). See ``docs/auth.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket, status
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# Authelia / Traefik forward-auth response headers (case-insensitive).
REMOTE_USER_HEADER = "remote-user"
REMOTE_GROUPS_HEADER = "remote-groups"
REMOTE_NAME_HEADER = "remote-name"
REMOTE_EMAIL_HEADER = "remote-email"

# Local-dev only; ignored when ``AUTH_REQUIRED=true``.
DEV_USER_HEADER = "x-browser-use-dev-user"

# Paths that never require identity (Docker healthcheck).
AUTH_EXEMPT_PATHS = frozenset({"/healthz"})


@dataclass(frozen=True, slots=True)
class User:
    """Caller identity derived from Authelia headers or a local-dev stub.

    Attributes:
        username: Principal from ``Remote-User`` (or stub).
        groups: Groups from ``Remote-Groups`` (comma-separated upstream).
        name: Display name from ``Remote-Name``, if present.
        email: Email from ``Remote-Email``, if present.
        source: How identity was obtained (``remote-user``, ``dev-header``,
            ``anonymous-stub``).
    """

    username: str
    groups: tuple[str, ...] = ()
    name: str | None = None
    email: str | None = None
    source: str = "remote-user"

    @property
    def user(self) -> str:
        """Alias for ``username`` (WS/docs compatibility)."""
        return self.username


# Backward-compatible name used by the T011 WebSocket stub.
RequestIdentity = User


def _header(headers: Headers, name: str) -> str | None:
    """Return a non-empty header value, or ``None``.

    Args:
        headers: Request or WebSocket headers.
        name: Header name (matched case-insensitively).

    Returns:
        Stripped value, or ``None`` when missing/blank.
    """
    value = headers.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_groups(raw: str | None) -> tuple[str, ...]:
    """Split Authelia ``Remote-Groups`` into a tuple of group names.

    Args:
        raw: Comma-separated groups header, or ``None``.

    Returns:
        Ordered unique-preserving group names.
    """
    if raw is None:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def user_from_authelia_headers(headers: Headers) -> User | None:
    """Build a :class:`User` from Authelia forward-auth headers only.

    Args:
        headers: Incoming HTTP or WebSocket headers.

    Returns:
        User when ``Remote-User`` is present, otherwise ``None``.
    """
    remote = _header(headers, REMOTE_USER_HEADER)
    if remote is None:
        return None
    return User(
        username=remote,
        groups=_parse_groups(_header(headers, REMOTE_GROUPS_HEADER)),
        name=_header(headers, REMOTE_NAME_HEADER),
        email=_header(headers, REMOTE_EMAIL_HEADER),
        source="remote-user",
    )


def resolve_identity(headers: Headers, *, auth_required: bool) -> User | None:
    """Resolve caller identity from headers under the current auth policy.

    - When ``auth_required`` is true, only Authelia ``Remote-User`` is accepted.
    - When false (local/tests), ``Remote-User``, then ``X-Browser-Use-Dev-User``,
      then a documented anonymous stub are accepted.

    Args:
        headers: Request or WebSocket headers.
        auth_required: When true, anonymous/dev stubs are rejected.

    Returns:
        Identity when allowed, otherwise ``None`` (caller should reject).
    """
    authelia = user_from_authelia_headers(headers)
    if authelia is not None:
        return authelia

    if auth_required:
        return None

    dev = _header(headers, DEV_USER_HEADER)
    if dev is not None:
        return User(username=dev, source="dev-header")

    return User(username="anonymous", source="anonymous-stub")


def get_request_user(request: Request) -> User | None:
    """Return identity attached by :class:`RemoteUserAuthMiddleware`, if any.

    Args:
        request: Current HTTP request.

    Returns:
        Attached :class:`User`, or ``None`` when unset.
    """
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    """FastAPI dependency: require a resolved identity on the request.

    Args:
        request: Current HTTP request (``state.user`` set by middleware).

    Returns:
        Authenticated :class:`User`.

    Raises:
        HTTPException: 401 when identity is missing.
    """
    user = get_request_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


async def accept_websocket_identity(
    websocket: WebSocket,
    *,
    auth_required: bool,
) -> User | None:
    """Accept a WebSocket only when identity resolves under current policy.

    Closes with code ``4401`` when identity is missing and auth is required.
    Acceptance happens here so rejected clients never enter the event loop.

    Args:
        websocket: Incoming WebSocket (not yet accepted).
        auth_required: Mirror of :attr:`AppSettings.auth_required`.

    Returns:
        Resolved identity after ``accept``, or ``None`` when closed/rejected.
    """
    identity = resolve_identity(websocket.headers, auth_required=auth_required)
    if identity is None:
        await websocket.close(code=4401, reason="Authentication required")
        return None
    websocket.state.user = identity
    await websocket.accept()
    return identity


class RemoteUserAuthMiddleware(BaseHTTPMiddleware):
    """Attach Authelia identity to ``request.state.user`` and enforce auth.

    Exempts ``/healthz`` so Docker healthchecks work without Traefik headers.
    When ``auth_required`` is false, still attaches a stub identity so handlers
    can read ``request.state.user`` uniformly.
    """

    def __init__(self, app: ASGIApp, *, auth_required: bool) -> None:
        """Initialize middleware.

        Args:
            app: Downstream ASGI app.
            auth_required: Reject missing ``Remote-User`` when true.
        """
        super().__init__(app)
        self.auth_required = auth_required

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Resolve identity, attach it, or return 401.

        Args:
            request: Incoming HTTP request.
            call_next: Next middleware or route.

        Returns:
            Downstream response, or 401 JSON when auth fails.
        """
        if request.url.path in AUTH_EXEMPT_PATHS:
            request.state.user = None
            return await call_next(request)

        identity = resolve_identity(request.headers, auth_required=self.auth_required)
        if identity is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
            )
        request.state.user = identity
        return await call_next(request)
