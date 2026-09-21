"""Tests for WebSocket run event streaming (T011)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.websockets import WebSocketDisconnect

from browser_use_agent.api.app import create_app
from browser_use_agent.api.events_bus import (
    MAX_WS_METADATA_BYTES,
    bound_payload,
    reset_event_bus_for_tests,
)
from browser_use_agent.audit.writer import set_append_hook
from browser_use_agent.config import AppSettings
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.security.redaction import REDACTED


def _docker_available() -> bool:
    """Return True when the Docker CLI can talk to a daemon."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """Yield a SQLAlchemy URL for an empty Postgres 18 database."""
    existing = os.environ.get("TEST_DATABASE_URL")
    if existing:
        yield existing
        return

    if not _docker_available():
        pytest.skip("Docker is required for WS event tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-ws-{uuid.uuid4().hex[:8]}"
    password = "testpass"
    user = "browser_use"
    dbname = "browser_use"

    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={dbname}",
            "-P",
            "postgres:18",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"Could not start postgres:18 container: {run.stderr.strip()}")

    port_proc = subprocess.run(
        ["docker", "port", name, "5432/tcp"],
        check=False,
        capture_output=True,
        text=True,
    )
    if port_proc.returncode != 0 or not port_proc.stdout.strip():
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)
        pytest.skip(f"Could not resolve published Postgres port: {port_proc.stderr.strip()}")

    host_port = port_proc.stdout.strip().rsplit(":", 1)[-1]
    url = f"postgresql+psycopg://{user}:{password}@127.0.0.1:{host_port}/{dbname}"
    deadline = time.time() + 60
    last_error: Exception | None = None
    try:
        while time.time() < deadline:
            try:
                engine = create_engine(url, pool_pre_ping=True)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                engine.dispose()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        else:
            pytest.fail(f"Postgres container did not become ready: {last_error}")

        yield url
    finally:
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)


@pytest.fixture
def api_client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """FastAPI TestClient with migrated DB and a fresh event bus."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    reset_event_bus_for_tests()
    settings = AppSettings(host="127.0.0.1", port=8000, database=None, auth_required=False)
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        yield client

    set_append_hook(None)
    engine.dispose()


def _recv_until(ws: object, predicate: object, *, limit: int = 40) -> dict:
    """Receive JSON until predicate matches or fail.

    Args:
        ws: Starlette TestClient WebSocket session.
        predicate: Callable ``dict -> bool``.
        limit: Max messages to read.

    Returns:
        Matching message dict.
    """
    for _ in range(limit):
        message = ws.receive_json()  # type: ignore[attr-defined]
        if predicate(message):  # type: ignore[operator]
            return message
    pytest.fail("Timed out waiting for matching WebSocket message")


def test_ws_replays_then_receives_live_append(api_client: TestClient) -> None:
    """Client gets replayed create events, then a live event after stop."""
    created = api_client.post("/api/runs", json={"goal": "stream me"}).json()
    run_id = created["id"]

    with api_client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        start = ws.receive_json()
        assert start["type"] == "replay_start"
        assert start["run_id"] == run_id

        replayed: list[dict] = []
        while True:
            message = ws.receive_json()
            if message["type"] == "replay_end":
                assert message["seq"] == 2
                break
            assert message["type"] == "event"
            assert message["source"] == "replay"
            replayed.append(message)

        assert [item["event_type"] for item in replayed] == ["task_received", "run_created"]
        assert "password" not in str(replayed[0].get("payload", {})).lower()

        stopped = api_client.post(f"/api/runs/{run_id}/stop")
        assert stopped.status_code == 200

        live = _recv_until(
            ws,
            lambda m: m.get("type") == "event" and m.get("event_type") == "run_cancelled",
        )
        assert live["source"] == "live"
        assert live["seq"] == 3
        assert live["payload"]["status"] == "cancelled"


def test_ws_replay_after_seq_skips_earlier(api_client: TestClient) -> None:
    """Reconnect with after_seq skips already-seen events."""
    created = api_client.post("/api/runs", json={"goal": "reconnect"}).json()
    run_id = created["id"]
    api_client.post(f"/api/runs/{run_id}/stop")

    with api_client.websocket_connect(f"/api/runs/{run_id}/events?after_seq=2") as ws:
        assert ws.receive_json()["type"] == "replay_start"
        event = ws.receive_json()
        assert event["type"] == "event"
        assert event["event_type"] == "run_cancelled"
        assert event["seq"] == 3
        end = ws.receive_json()
        assert end["type"] == "replay_end"
        assert end["seq"] == 3


def test_ws_redacts_secrets_in_live_payload(api_client: TestClient) -> None:
    """Live WS payload uses AuditWriter redaction (no raw secrets)."""
    from sqlalchemy.orm import sessionmaker

    from browser_use_agent.audit.writer import AuditWriter
    from browser_use_agent.db.models import Run

    created = api_client.post("/api/runs", json={"goal": "secret check"}).json()
    run_id = uuid.UUID(created["id"])

    with api_client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        while ws.receive_json()["type"] != "replay_end":
            pass

        factory: sessionmaker = api_client.app.state.session_factory
        session = factory()
        try:
            assert session.get(Run, run_id) is not None
            AuditWriter(session).append(
                run_id,
                "step_progress",
                {"note": "ok", "password": "super-secret-value"},
                actor="agent",
            )
            session.commit()
        finally:
            session.close()

        live = _recv_until(
            ws,
            lambda m: m.get("type") == "event" and m.get("event_type") == "step_progress",
        )
        assert live["source"] == "live"
        assert live["payload"]["password"] == REDACTED
        assert "super-secret-value" not in str(live)


def test_ws_auth_required_rejects_anonymous(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTH_REQUIRED rejects WS and REST without Remote-User."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    reset_event_bus_for_tests()
    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=True,
        csrf_trusted_origins=(),
    )
    app = create_app(settings, engine=engine)
    try:
        with TestClient(app) as client:
            denied = client.post("/api/runs", json={"goal": "auth"})
            assert denied.status_code == 401

            created = client.post(
                "/api/runs",
                json={"goal": "auth"},
                headers={"Remote-User": "admin"},
            )
            assert created.status_code == 201
            run_id = created.json()["id"]

            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/api/runs/{run_id}/events"):
                    pass
            assert exc_info.value.code == 4401

            with client.websocket_connect(
                f"/api/runs/{run_id}/events",
                headers={"Remote-User": "alice"},
            ) as ws:
                assert ws.receive_json()["type"] == "replay_start"
    finally:
        set_append_hook(None)
        engine.dispose()


def test_bound_payload_keeps_artifact_refs() -> None:
    """Oversized payloads truncate but retain artifact identifiers."""
    huge = {"dom": "x" * (MAX_WS_METADATA_BYTES + 100), "artifact_id": str(uuid.uuid4())}
    payload, truncated = bound_payload(huge)
    assert truncated is True
    assert payload["artifact_id"] == huge["artifact_id"]
    assert payload.get("_truncated") is True
