"""Minimal HTTP health endpoint for the compose stub image."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    """Serve `/health` and reject other paths."""

    def do_GET(self) -> None:
        """Respond to health checks; return 404 elsewhere."""
        if self.path.split("?", 1)[0] == "/health":
            body = b'{"status":"ok"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default access logging for the stub server."""


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Bind and serve the stub health endpoint until interrupted.

    Args:
        host: Bind address.
        port: Bind port (Traefik loadbalancer target).
    """
    server = ThreadingHTTPServer((host, port), HealthHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
