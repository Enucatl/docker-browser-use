"""Auth helpers for the controller API (stub until Authelia trust in T012)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import WebSocket
from starlette.datastructures import Headers

# Temporary local-dev identity header; T012 will prefer Authelia Remote-User.
DEV_USER_HEADER = "x-browser-use-dev-user"
REMOTE_USER_HEADER = "remote-user"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Authenticated (or stub) caller identity for API/WS gates.

    Attributes:
        user: Principal name (Authelia ``Remote-User`` or stub value).
        source: How identity was obtained (``remote-user``, ``dev-header``,
            ``anonymous-stub``).
    """

    user: str
    source: str


def _header(headers: Headers, name: str) -> str | None:
    """Return a non-empty header value, or ``None``."""
    value = headers.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_identity(headers: Headers, *, auth_required: bool) -> RequestIdentity | None:
    """Resolve caller identity from headers.

    Compatible with upcoming T012 Authelia trust:

    - When ``auth_required`` is true, only ``Remote-User`` is accepted.
    - When false (local/tests), ``Remote-User``, then ``X-Browser-Use-Dev-User``,
      then a documented anonymous stub are accepted.

    Args:
        headers: Request or WebSocket headers.
        auth_required: When true, anonymous/dev stubs are rejected.

    Returns:
        Identity when allowed, otherwise ``None`` (caller should reject).
    """
    remote = _header(headers, REMOTE_USER_HEADER)
    if remote is not None:
        return RequestIdentity(user=remote, source="remote-user")

    if auth_required:
        return None

    dev = _header(headers, DEV_USER_HEADER)
    if dev is not None:
        return RequestIdentity(user=dev, source="dev-header")

    return RequestIdentity(user="anonymous", source="anonymous-stub")


async def accept_websocket_identity(
    websocket: WebSocket,
    *,
    auth_required: bool,
) -> RequestIdentity | None:
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
    await websocket.accept()
    return identity
