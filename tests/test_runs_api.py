"""Tests for the agent run lifecycle API (T010)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from browser_use_agent.api.app import create_app
from browser_use_agent.config import AppSettings
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import AgentEvent, Run
from browser_use_agent.runs import RunStatus


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
        pytest.skip("Docker is required for runs API tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-runs-{uuid.uuid4().hex[:8]}"
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
    """FastAPI TestClient bound to a migrated Postgres engine."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    settings = AppSettings(host="127.0.0.1", port=8000, database=None)
    app = create_app(settings, engine=engine)
    # Avoid starting the Browser Use worker in lifecycle API tests (no CDP).
    app.state.run_worker = None
    with TestClient(app) as client:
        yield client

    engine.dispose()


def test_create_run_persists_and_emits_audit(api_client: TestClient, postgres_url: str) -> None:
    """POST /api/runs stores a run and writes task_received / run_created events."""
    response = api_client.post(
        "/api/runs",
        json={"goal": "Open example.com and summarize the homepage", "profile_id": "personal"},
    )
    assert response.status_code == 201
    body = response.json()
    run_id = uuid.UUID(body["id"])
    assert body["goal"].startswith("Open example.com")
    assert body["status"] == RunStatus.QUEUED.value
    assert body["profile_id"] == "personal"
    assert body["cost"] is None

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.goal.startswith("Open example.com")
        assert run.status == RunStatus.QUEUED.value

        events = list(
            session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.seq)
            ).all()
        )
        assert [event.event_type for event in events] == ["task_received", "run_created"]
        assert events[0].event_hash
        assert events[1].prev_hash == events[0].event_hash
    finally:
        session.close()
        engine.dispose()


def test_list_and_get_run(api_client: TestClient) -> None:
    """GET list and get return created runs."""
    created = api_client.post("/api/runs", json={"goal": "list me"}).json()
    run_id = created["id"]

    listed = api_client.get("/api/runs")
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()]
    assert run_id in ids

    fetched = api_client.get(f"/api/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == run_id
    assert fetched.json()["goal"] == "list me"


def test_get_missing_run_is_404(api_client: TestClient) -> None:
    """Unknown run ids return 404."""
    response = api_client.get(f"/api/runs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_stop_run_sets_cancelled(api_client: TestClient) -> None:
    """POST /api/runs/{id}/stop marks a queued run cancelled."""
    created = api_client.post("/api/runs", json={"goal": "stop me"}).json()
    run_id = created["id"]

    stopped = api_client.post(f"/api/runs/{run_id}/stop")
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["status"] == RunStatus.CANCELLED.value
    assert body["finished_at"] is not None

    again = api_client.post(f"/api/runs/{run_id}/stop")
    assert again.status_code == 200
    assert again.json()["status"] == RunStatus.CANCELLED.value
