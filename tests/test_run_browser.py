"""Opt-in Chrome check for the execution workspace.

Run with BROWSER_UI_CDP_URL=http://<test-browser>:9222 and
``uv run pytest -q tests/test_run_browser.py``.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import niquests
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_web_ui import api_client as api_client
from test_web_ui import postgres_url as postgres_url
from websockets.sync.client import connect

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import Run
from browser_use_agent.services import runs as run_service


@pytest.mark.skipif(not os.environ.get("BROWSER_UI_CDP_URL"), reason="Set a test Chrome CDP URL")
def test_workspace_in_chrome(
    api_client: TestClient,
    postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real DOM behavior in a disposable tab and isolated test database."""
    cdp_url = os.environ["BROWSER_UI_CDP_URL"].rstrip("/")
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "fs")
    engine = create_engine(postgres_url)
    with Session(engine) as session:
        run = run_service.create_run(session, "Inspect the requested page")
        run_id = run.id
        writer = AuditWriter(session)
        writer.append(run.id, "action_completed", {"kind": "click"})
        # A valid one-pixel PNG is enough to exercise the authenticated image path.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
        )
        result = FilesystemArtifactStore(tmp_path).put(
            png, media_type="image/png", kind="screenshot", run_id=run_id, session=session
        )
        writer.append(run_id, "screenshot_captured", {"artifact_id": str(result.artifact_id)})
        run.status = "succeeded"
        session.commit()

    class Handler(BaseHTTPRequestHandler):
        """Expose the TestClient over HTTP to the separate Chrome process."""

        def do_GET(self) -> None:
            """Proxy test app GET requests, substituting the shared live browser."""
            if self.path.startswith("/vnc/"):
                content, status, media = b"<p>Shared live browser fixture</p>", 200, "text/html"
            else:
                response = api_client.get(self.path)
                content, status = response.content, response.status_code
                media = response.headers.get("content-type", "text/plain")
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args: object) -> None:
            """Keep routine HTTP traffic out of test output."""

    # The local interface used to reach Chrome is also reachable by that browser.
    address = urlsplit(cdp_url)
    with socket.create_connection((address.hostname, address.port or 80)) as connection:
        local_host = connection.getsockname()[0]
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_url = f"http://{local_host}:{server.server_port}/runs/{run_id}"
    target = niquests.put(f"{cdp_url}/json/new?about:blank", timeout=5).json()
    websocket_url = (
        urlsplit(target["webSocketDebuggerUrl"])._replace(netloc=address.netloc).geturl()
    )
    try:
        with connect(websocket_url) as ws:
            sequence = 0
            errors = []

            def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                """Run one CDP command and retain uncaught browser exceptions."""
                nonlocal sequence
                sequence += 1
                ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(ws.recv(timeout=15))
                    if message.get("method") == "Runtime.exceptionThrown":
                        errors.append(message["params"])
                    if message.get("id") == sequence:
                        assert "error" not in message, message
                        return message.get("result", {})

            def evaluate(expression: str) -> Any:
                """Evaluate JavaScript and fail on an exception."""
                result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                assert "exceptionDetails" not in result, result
                return result.get("result", {}).get("value")

            def navigate(width: int) -> None:
                """Load the run at the requested viewport width."""
                call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": 941,
                        "deviceScaleFactor": 1,
                        "mobile": width < 600,
                    },
                )
                call("Page.navigate", {"url": page_url})
                for _ in range(100):
                    if evaluate(
                        'document.readyState === "complete" &&!!document.getElementById("run-data")'
                    ):
                        break
                    time.sleep(0.05)
                assert evaluate('document.querySelector("#selection-title").textContent')

            call("Runtime.enable")
            call("Page.enable")
            call(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                window.__sockets = [];
                window.WebSocket = class {
                    static OPEN = 1;
                    readyState = 1;
                    constructor(url) {
                        this.url = url; window.__sockets.push(this);
                        setTimeout(() => this.onopen?.(), 0);
                    }
                    close() {}
                };
                const timeout = window.setTimeout;
                window.setTimeout = (fn, delay, ...args) => {
                    if (delay === 5000) { window.__poll = fn; return 0; }
                    if (delay === 1500) { window.__reconnect = fn; return 0; }
                    return timeout(fn, delay, ...args);
                };
            """
                },
            )
            for width in (1672, 390):
                navigate(width)
                assert evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert evaluate('document.querySelector("iframe") === null')
                assert evaluate("window.__sockets.length") == 0
                assert evaluate('document.querySelector("#browser-screenshot").naturalWidth') == 1
            evaluate('document.querySelector("#artifacts-tab").click()')
            assert evaluate('!document.querySelector("#artifacts-panel").hidden')
            evaluate('document.querySelector(".artifact-preview").click()')
            assert evaluate('!document.querySelector("#browser-screenshot").hidden')
            evaluate('document.querySelector("#event-list .event-select").click()')
            assert evaluate('!document.querySelector("#selection-content").hidden')
            evaluate(
                'document.querySelector("#event-filter").value="error";'
                'document.querySelector("#event-filter").dispatchEvent(new Event("change"))'
            )
            assert evaluate(
                'Array.from(document.querySelectorAll("#event-list > li")).every(row => row.hidden)'
            )
            evaluate(
                'document.querySelector("#timeline-tab").click();'
                'document.querySelector("#timeline a").click()'
            )
            assert evaluate('document.querySelector("#event-filter").value') == "all"
            evaluate(
                'document.querySelector("#timeline-tab").dispatchEvent('
                'new KeyboardEvent("keydown", {key:"ArrowRight", bubbles:true}))'
            )
            assert evaluate("document.activeElement.id") == "artifacts-tab"
            evaluate('document.querySelector("#browser-screenshot").src="/missing.png"')
            for _ in range(100):
                if evaluate('!document.querySelector("#browser-empty").hidden'):
                    break
                time.sleep(0.05)
            assert evaluate('document.querySelector("#browser-screenshot").hidden')
            evaluate('document.querySelector(".artifact-preview").click()')
            assert evaluate('document.querySelector("#browser-empty").hidden')

            with Session(engine) as session:
                session.get(Run, run_id).status = "running"
                session.commit()
            navigate(1672)
            assert evaluate('!!document.querySelector("iframe")')
            event = {
                "type": "event",
                "seq": 99,
                "event_type": "action_completed",
                "occurred_at": "2026-09-28T00:00:00Z",
                "payload": {"message": "<img src=x>"},
                "display": {
                    "title": "Clicked",
                    "summary": "<img src=x>",
                    "category": "browser",
                    "tone": "success",
                    "details": {},
                },
            }
            for seq in (99, 99, 98, *range(100, 205)):
                event["seq"] = seq
                evaluate(
                    f"window.__sockets[0].onmessage({{data:JSON.stringify({json.dumps(event)})}})"
                )
            evaluate('document.querySelector("#timeline li:last-child a").focus()')
            event["seq"] = 205
            evaluate(f"window.__sockets[0].onmessage({{data:JSON.stringify({json.dumps(event)})}})")
            assert evaluate("document.activeElement.dataset.eventSeq") == "204"
            rows = 'document.querySelectorAll("#event-list > li")'
            assert evaluate(f"Array.from({rows}).map(row => Number(row.dataset.seq))") == list(
                range(106, 206)
            )
            assert evaluate('document.querySelectorAll("#timeline > li").length') == 100
            assert evaluate('document.querySelector("#event-list img") === null')
            assert evaluate('document.querySelector("#activity-update").textContent').startswith(
                "Clicked"
            )
            evaluate(
                'document.querySelector("#artifacts-tab").click();'
                'document.querySelector(".artifact-preview").click()'
            )
            assert evaluate('document.querySelector("#live-browser").hidden')
            evaluate('document.querySelector("#return-to-browser").click()')
            assert evaluate(
                '!document.querySelector("#live-browser").hidden &&'
                'document.querySelector("#browser-screenshot").hidden'
            )
            evaluate(
                'document.querySelector("#browser-screenshot").dispatchEvent(new Event("error"))'
            )
            assert evaluate('document.querySelector("#browser-empty").hidden')
            evaluate("window.__sockets[0].onclose(); window.__reconnect()")
            assert evaluate("window.__sockets[1].url").endswith("after_seq=205")
            with Session(engine) as session:
                session.get(Run, run_id).status = "succeeded"
                session.commit()
            evaluate("window.__poll()")
            for _ in range(100):
                if evaluate('document.querySelector("#run-status")?.textContent === "succeeded"'):
                    break
                time.sleep(0.05)
            assert evaluate('document.querySelector("#run-status").textContent') == "succeeded"
            assert evaluate('document.querySelector("iframe") === null')
            assert not errors
    finally:
        niquests.get(f"{cdp_url}/json/close/{target['id']}", timeout=5)
        server.shutdown()
        server.server_close()
        engine.dispose()
