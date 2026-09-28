"""Regression checks for the browser execution workspace and HTML controls."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.api.app import create_app
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.config import AppSettings
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import Artifact, CostEntry, HumanApproval, Run
from browser_use_agent.runs import RunStatus
from browser_use_agent.services import runs as run_service


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
        pytest.skip("Docker is required for web UI tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-webui-{uuid.uuid4().hex[:8]}"
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
    """FastAPI TestClient bound to a migrated Postgres engine (auth off)."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=False,
        allowed_hosts=(),
        csrf_trusted_origins=(),
    )
    app = create_app(settings, engine=engine)
    # UI smoke tests do not need a live Browser Use worker (would hang without CDP).
    app.state.run_worker = None
    with TestClient(app) as client:
        yield client

    engine.dispose()


@pytest.fixture
def auth_client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Client with AUTH_REQUIRED (Authelia Remote-User only)."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=True,
        allowed_hosts=("testserver", "localhost", "127.0.0.1"),
        csrf_trusted_origins=("https://browser-use.example.test",),
    )
    app = create_app(settings, engine=engine)
    app.state.run_worker = None
    with TestClient(app) as client:
        yield client

    engine.dispose()


def test_home_renders_new_run_form(api_client: TestClient) -> None:
    """GET / serves the new-run form for an authenticated (or stub) user."""
    response = api_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "New run" in response.text
    assert 'name="goal"' in response.text
    assert "anonymous" in response.text


def test_history_lists_prior_runs(api_client: TestClient, postgres_url: str) -> None:
    """History page shows runs persisted in Postgres."""
    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = factory()
    try:
        run = run_service.create_run(session, "history smoke goal")
        session.commit()
        run_id = str(run.id)
    finally:
        session.close()
        engine.dispose()

    response = api_client.get("/history")
    assert response.status_code == 200
    assert "history smoke goal" in response.text
    assert run_id in response.text


def test_create_run_form_redirects_to_detail(api_client: TestClient) -> None:
    """POST /runs creates a run and redirects to the detail page."""
    response = api_client.post("/runs", data={"goal": "UI start me"}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/runs/")

    detail = api_client.get(location)
    assert detail.status_code == 200
    assert "UI start me" in detail.text
    assert "Activity" in detail.text
    assert "/vnc/" in detail.text
    assert (
        "Take control" in detail.text or "Release control" in detail.text or "Cancel" in detail.text
    )


def test_run_detail_pause_resume_cancel(api_client: TestClient, postgres_url: str) -> None:
    """Pause / resume / cancel form posts update run status."""
    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = factory()
    try:
        run = run_service.create_run(session, "control me")
        run.status = RunStatus.RUNNING.value
        run.started_at = datetime.now(UTC)
        session.commit()
        run_id = run.id
    finally:
        session.close()
        engine.dispose()

    paused = api_client.post(f"/runs/{run_id}/pause", follow_redirects=False)
    assert paused.status_code == 303

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        assert session.get(Run, run_id).status == RunStatus.PAUSED.value  # type: ignore[union-attr]
    finally:
        session.close()
        engine.dispose()

    resumed = api_client.post(f"/runs/{run_id}/resume", follow_redirects=False)
    assert resumed.status_code == 303

    cancelled = api_client.post(f"/runs/{run_id}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        assert session.get(Run, run_id).status == RunStatus.CANCELLED.value  # type: ignore[union-attr]
    finally:
        session.close()
        engine.dispose()


def test_run_detail_approve_reject(api_client: TestClient, postgres_url: str) -> None:
    """Approve / reject forms work from the run page when awaiting approval."""
    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = factory()
    try:
        run = run_service.create_run(session, "approve me")
        writer = AuditWriter(session)
        event = writer.append(
            run.id,
            "approval_requested",
            {"reason": "checkout click"},
            actor="system",
        )
        session.flush()
        session.add(
            HumanApproval(
                id=uuid.uuid4(),
                run_id=run.id,
                event_id=event.id,
                status="requested",
                reason="checkout click",
                metadata_={"target": '<img src=x onerror="alert(1)">'},
            )
        )
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.commit()
        run_id = run.id
    finally:
        session.close()
        engine.dispose()

    page = api_client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "Approval required" in page.text
    assert "checkout click" in page.text
    assert f'action="/runs/{run_id}/approve"' in page.text
    assert f'action="/runs/{run_id}/reject"' in page.text
    assert '<img src=x onerror="alert(1)">' not in page.text
    assert "<pre" not in re.sub(r"<details\b[^>]*>.*?</details>", "", page.text, flags=re.S)

    approved = api_client.post(
        f"/runs/{run_id}/approve",
        data={"reason": "ok"},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        assert session.get(Run, run_id).status == RunStatus.RUNNING.value  # type: ignore[union-attr]
        run = session.get(Run, run_id)
        assert run is not None
        writer = AuditWriter(session)
        event = writer.append(
            run.id,
            "approval_requested",
            {"reason": "pay now"},
            actor="system",
        )
        session.flush()
        session.add(
            HumanApproval(
                id=uuid.uuid4(),
                run_id=run.id,
                event_id=event.id,
                status="requested",
                reason="pay now",
            )
        )
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.commit()
    finally:
        session.close()
        engine.dispose()

    rejected = api_client.post(
        f"/runs/{run_id}/reject",
        data={"reason": "no"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        assert session.get(Run, run_id).status == RunStatus.FAILED.value  # type: ignore[union-attr]
    finally:
        session.close()
        engine.dispose()


def test_static_assets_served(api_client: TestClient) -> None:
    """CSS and run.js are served under /static."""
    css = api_client.get("/static/app.css")
    assert css.status_code == 200
    assert "browser-use" in css.text or "--accent" in css.text

    js = api_client.get("/static/run.js")
    assert js.status_code == 200
    assert "WebSocket" in js.text


def test_ui_requires_remote_user_when_auth_required(auth_client: TestClient) -> None:
    """HTML routes reject missing Authelia identity when AUTH_REQUIRED=true."""
    denied = auth_client.get("/")
    assert denied.status_code == 401

    ok = auth_client.get("/", headers={"Remote-User": "alice"})
    assert ok.status_code == 200
    assert "alice" in ok.text


@pytest.mark.parametrize(
    ("status", "actions", "active"),
    [
        (RunStatus.QUEUED, {"cancel"}, True),
        (RunStatus.RUNNING, {"pause", "cancel", "take-control"}, True),
        (RunStatus.PAUSED, {"resume", "cancel", "take-control"}, True),
        (RunStatus.AWAITING_HUMAN, {"cancel", "release-control"}, True),
        (RunStatus.SUCCEEDED, set(), False),
        (RunStatus.FAILED, {"retry"}, False),
        (RunStatus.CANCELLED, set(), False),
    ],
)
def test_workspace_controls_follow_run_status(
    api_client: TestClient,
    postgres_url: str,
    status: RunStatus,
    actions: set[str],
    active: bool,
) -> None:
    """Show valid controls and embed the shared browser only for active runs."""
    engine = create_engine(postgres_url)
    try:
        with Session(engine) as session:
            run = run_service.create_run(session, "Inspect the workspace")
            run.status = status.value
            run_id = run.id
            session.commit()
        page = api_client.get(f"/runs/{run_id}")
    finally:
        engine.dispose()

    assert page.status_code == 200
    assert set(re.findall(rf'action="/runs/{run_id}/([a-z-]+)"', page.text)) == actions
    assert ("<iframe" in page.text) is active
    assert "Activity" in page.text
    assert "Timeline" in page.text
    assert "Artifacts" in page.text
    assert "Run summary" in page.text
    if active:
        assert "shared" in page.text.lower()
    else:
        assert "No screenshot" in page.text


def test_activity_is_chronological_and_technical_data_is_disclosed(
    api_client: TestClient, postgres_url: str
) -> None:
    """Render observable activity safely while keeping model payloads collapsed."""
    unsafe = '</script><script>alert("unsafe")</script>'
    rationale = "PRIVATE_MODEL_RATIONALE_MARKER"
    engine = create_engine(postgres_url)
    try:
        with Session(engine) as session:
            run = run_service.create_run(session, f"Inspect {unsafe}")
            writer = AuditWriter(session)
            writer.append(
                run.id,
                "decision",
                {"kind": "click", "rationale": rationale, "raw_marker": unsafe},
                actor="agent",
            )
            writer.append(
                run.id,
                "action_completed",
                {"kind": "click", "message": "Opened the requested page"},
                actor="agent",
            )
            run_id = run.id
            session.commit()
        page = api_client.get(f"/runs/{run_id}")
    finally:
        engine.dispose()

    assert page.status_code == 200
    assert unsafe not in page.text
    assert "&lt;script&gt;" in page.text
    assert "Action selected" in page.text
    assert "Clicked" in page.text
    assert "Opened the requested page" in page.text
    sequence = re.findall(r'<li\b[^>]*data-seq="(\d+)"', page.text)
    assert len(sequence) >= 3
    assert [int(seq) for seq in sequence] == sorted(int(seq) for seq in sequence)

    disclosures = re.findall(r"<details\b[^>]*>.*?</details>", page.text, flags=re.S)
    assert disclosures
    assert all(not re.search(r"\bopen\b", item.split(">", 1)[0]) for item in disclosures)
    visible = re.sub(r"<details\b[^>]*>.*?</details>", "", page.text, flags=re.S)
    visible = re.sub(r"<script\b[^>]*>.*?</script>", "", visible, flags=re.S)
    assert "<pre" not in visible
    assert rationale not in visible


def test_completed_workspace_uses_recorded_screenshot_and_run_totals(
    api_client: TestClient, postgres_url: str
) -> None:
    """Show persisted metrics, screenshot previews, and artifact downloads."""
    engine = create_engine(postgres_url)
    try:
        with Session(engine) as session:
            run = run_service.create_run(session, "Read the requested page")
            writer = AuditWriter(session)
            for _ in range(2):
                step_id = uuid.uuid4()
                writer.append(run.id, "observation_captured", step_id=step_id)
                writer.append(run.id, "action_requested", {"kind": "click"}, step_id=step_id)
            screenshot_id = uuid.uuid4()
            screenshot_event = writer.append(
                run.id, "screenshot_captured", {"artifact_id": str(screenshot_id)}
            )
            session.add(
                Artifact(
                    id=screenshot_id,
                    run_id=run.id,
                    event_id=screenshot_event.id,
                    kind="screenshot",
                    media_type="image/png",
                    size_bytes=123,
                    sha256="f" * 64,
                    storage_key="f" * 64,
                )
            )
            session.add(CostEntry(run_id=run.id, kind="model", amount=Decimal("0.1234")))
            run.status = RunStatus.SUCCEEDED.value
            run.started_at = datetime.now(UTC) - timedelta(seconds=125)
            run.finished_at = run.started_at + timedelta(seconds=125)
            run_id = run.id
            session.commit()
        page = api_client.get(f"/runs/{run_id}")
    finally:
        engine.dispose()

    assert page.status_code == 200
    assert "<iframe" not in page.text
    artifact_url = f"/runs/{run_id}/artifacts/{screenshot_id}"
    assert f'src="{artifact_url}?preview=1"' in page.text
    assert f'href="{artifact_url}" download' in page.text
    assert re.search(r'id="summary-steps"[^>]*>2<', page.text)
    assert re.search(r'id="summary-actions"[^>]*>2<', page.text)
    assert re.search(r'id="summary-artifacts"[^>]*>1<', page.text)
    assert "125s" in page.text
    assert "$0.12340000 USD" in page.text
