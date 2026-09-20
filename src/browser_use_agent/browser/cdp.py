"""CDP health checks and WebSocket URL rewriting for the nginx front proxy."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.error import URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class CdpUnavailableError(RuntimeError):
    """Raised when the CDP endpoint is unreachable within the wait budget."""


def rewrite_cdp_websocket_url(http_cdp_url: str, websocket_url: str) -> str:
    """Rewrite Chromium's loopback ``webSocketDebuggerUrl`` to the peer CDP URL.

    Modern Chromium advertises ``ws://127.0.0.1:<loopback>/devtools/...``. Peers
    must connect through the container nginx front on ``browser:9222`` instead.

    Args:
        http_cdp_url: Peer-facing HTTP CDP base (e.g. ``http://browser:9222``).
        websocket_url: Raw ``webSocketDebuggerUrl`` from ``/json/version``.

    Returns:
        WebSocket URL using the HTTP CDP host/port and the original path/query.
    """
    http = urlparse(http_cdp_url)
    ws = urlparse(websocket_url)
    scheme = "wss" if http.scheme == "https" else "ws"
    return urlunparse((scheme, http.netloc, ws.path, "", ws.query, ""))


def _fetch_json_version(cdp_url: str, *, timeout: float = 5.0) -> dict[str, object]:
    """GET ``/json/version`` from the CDP HTTP endpoint.

    Args:
        cdp_url: Peer-facing HTTP CDP base URL.
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON object from Chromium.

    Raises:
        URLError: On transport failure.
        TimeoutError: On timeout.
        json.JSONDecodeError: On invalid JSON.
    """
    base = cdp_url.rstrip("/")
    url = f"{base}/json/version"
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_version(cdp_url: str, *, timeout: float = 5.0) -> dict[str, object]:
    """Fetch Chromium ``/json/version`` or raise.

    Args:
        cdp_url: Peer-facing HTTP CDP base URL.
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON version payload.
    """
    return _fetch_json_version(cdp_url, timeout=timeout)


def resolve_cdp_websocket_url(cdp_url: str, *, timeout: float = 5.0) -> str:
    """Resolve a peer-reachable CDP WebSocket URL from the HTTP endpoint.

    Args:
        cdp_url: Peer-facing HTTP CDP base URL.
        timeout: Socket timeout in seconds.

    Returns:
        Rewritten ``ws://`` / ``wss://`` debugger URL.
    """
    payload = cdp_version(cdp_url, timeout=timeout)
    raw = payload.get("webSocketDebuggerUrl")
    if not isinstance(raw, str) or not raw:
        raise CdpUnavailableError(f"CDP /json/version missing webSocketDebuggerUrl: {payload!r}")
    return rewrite_cdp_websocket_url(cdp_url, raw)


async def wait_for_cdp(
    cdp_url: str,
    *,
    timeout: float,
    poll_interval: float = 0.5,
) -> dict[str, object]:
    """Poll until CDP responds or the timeout elapses.

    Args:
        cdp_url: Peer-facing HTTP CDP base URL.
        timeout: Maximum wait in seconds.
        poll_interval: Delay between attempts.

    Returns:
        Successful ``/json/version`` payload.

    Raises:
        CdpUnavailableError: When CDP never becomes ready.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while True:
        try:
            payload = await asyncio.to_thread(cdp_version, cdp_url, timeout=min(5.0, timeout))
            logger.debug("CDP ready at %s: browser=%s", cdp_url, payload.get("Browser"))
            return payload
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(poll_interval)
    raise CdpUnavailableError(
        f"CDP unavailable at {cdp_url} within {timeout}s (last error: {last_error})"
    )
