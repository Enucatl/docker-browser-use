"""Tests for the stub health HTTP handler."""

from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

from browser_use_agent.health import HealthHandler, serve


def test_health_endpoint_ok() -> None:
    """GET /health returns 200 and a JSON ok payload."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        conn = HTTPConnection(str(host), int(port), timeout=2)
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'"status":"ok"' in body
        conn.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_unknown_path_is_404() -> None:
    """Non-health paths return 404."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        conn = HTTPConnection(str(host), int(port), timeout=2)
        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()
        assert response.status == 404
        conn.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_serve_is_callable() -> None:
    """serve() is importable for the container CMD entrypoint."""
    assert callable(serve)
