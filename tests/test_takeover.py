"""Tests for take-control / release-control mutex (T023)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.controls import get_control_hub, reset_control_hub_for_tests
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.agent.takeover import (
    TakeoverGuardedBrowserPort,
    wrap_browser_for_takeover,
)
from browser_use_agent.api.app import create_app
from browser_use_agent.api.events_bus import reset_event_bus_for_tests
from browser_use_agent.config import AppSettings
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import AgentEvent, Run
from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import redact_for_audit
from browser_use_agent.services import runs as run_service
from browser_use_agent.services import takeover as takeover_service


@dataclass
class _RecordedEvent:
    """One audit event captured by :class:`RecordingAuditWriter`."""

    run_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    actor: str
    step_id: uuid.UUID | None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class RecordingAuditWriter:
    """In-memory audit sink that always redacts payloads (no Postgres)."""

    def __init__(self) -> None:
        """Create an empty recording writer."""
        self.events: list[_RecordedEvent] = []

    def append(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        step_id: uuid.UUID | None = None,
        parent_event_id: uuid.UUID | None = None,
        url: str | None = None,
        tab_id: str | None = None,
        duration_ms: int | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> _RecordedEvent:
        """Append a redacted event to the in-memory list."""
        del parent_event_id, url, tab_id, duration_ms, occurred_at
        raw = dict(payload or {})
        redacted = redact_for_audit(raw)
        if not isinstance(redacted, dict):
            redacted = {"value": redacted}
        event = _RecordedEvent(
            run_id=run_id,
            event_type=event_type,
            payload=redacted,
            actor=actor,
            step_id=step_id,
            id=event_id or uuid.uuid4(),
        )
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        """Return event_type values in append order."""
        return [e.event_type for e in self.events]


def _obs(*, url: str = "https://example.com/") -> BrowserObservation:
    """Build a small observation with one clickable link."""
    return BrowserObservation(
        url=url,
        title="Example",
        candidates=[
            CandidateElement(index=1, tag="a", name="Next", href="https://example.com/next"),
        ],
        suggested_urls=["https://example.com/next"],
    )


def test_guarded_port_refuses_execute_while_held() -> None:
    """Fake executor must not record click/type while takeover is active."""

    async def _run() -> None:
        inner = FakeBrowserPort([_obs()])
        held = True
        guarded = wrap_browser_for_takeover(inner, lambda: held)
        assert isinstance(guarded, TakeoverGuardedBrowserPort)

        action = AgentAction(kind=ActionKind.CLICK, target_index=1, confidence=0.9)
        result = await guarded.execute(action)
        assert result.ok is False
        assert result.metadata.get("refused") is True
        assert inner.executed == []

        held = False
        result = await guarded.execute(action)
        assert result.ok is True
        assert len(inner.executed) == 1
        assert inner.executed[0].kind == ActionKind.CLICK

    asyncio.run(_run())


def test_takeover_blocks_agent_actions_until_release() -> None:
    """While awaiting_human, the loop parks and executes no further actions."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        awaiting_human = False
        cancelled = False
        inner = FakeBrowserPort(
            [
                _obs(),
                _obs(url="https://example.com/2"),
                _obs(url="https://example.com/3"),
            ],
        )
        execute_started = asyncio.Event()
        original_execute = inner.execute

        async def slow_execute(action: AgentAction):
            execute_started.set()
            await asyncio.sleep(0.15)
            return await original_execute(action)

        inner.execute = slow_execute  # type: ignore[method-assign]
        browser = wrap_browser_for_takeover(inner, lambda: awaiting_human)
        jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", confidence=0.99),
            ],
        )

        async def takeover_during_first_execute() -> None:
            nonlocal awaiting_human
            await execute_started.wait()
            awaiting_human = True
            signals.wake.clear()
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if len(inner.executed) == 1:
                    await asyncio.sleep(0.1)
                    break
                await asyncio.sleep(0.02)
            assert len(inner.executed) == 1
            awaiting_human = False
            signals.notify()

        task = asyncio.create_task(takeover_during_first_execute())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Scroll twice then done",
            browser=browser,
            jev=jev,
            audit=audit,
            is_cancelled=lambda: cancelled,
            is_paused=lambda: False,
            is_awaiting_human=lambda: awaiting_human,
            control_signals=signals,
            max_steps=10,
        ).run()
        await task

        assert outcome.status == RunStatus.SUCCEEDED
        assert len(inner.executed) == 3
        assert inner.executed[-1].kind == ActionKind.DONE

    asyncio.run(_run())


def test_release_after_mid_step_takeover_does_fresh_observe() -> None:
    """Release mid-step discards the stale decide and re-observes."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        awaiting_human = False
        observe_urls: list[str] = []
        decide_count = 0
        inner = FakeBrowserPort(
            [
                _obs(url="https://example.com/before"),
                _obs(url="https://example.com/after-human"),
            ],
        )
        original_observe = inner.observe

        async def tracking_observe() -> BrowserObservation:
            obs = await original_observe()
            observe_urls.append(obs.url)
            return obs

        inner.observe = tracking_observe  # type: ignore[method-assign]
        browser = wrap_browser_for_takeover(inner, lambda: awaiting_human)

        base_jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", done_message="ok", confidence=0.99),
            ],
        )

        class _TakeoverOnFirstDecide:
            """Arm human control immediately after the first Jev decision."""

            def decide(self, request):  # noqa: ANN001
                nonlocal awaiting_human, decide_count
                response = base_jev.decide(request)
                decide_count += 1
                if decide_count == 1:
                    awaiting_human = True
                    signals.wake.clear()
                return response

        async def release_after_park() -> None:
            nonlocal awaiting_human
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if decide_count >= 1 and awaiting_human:
                    await asyncio.sleep(0.1)
                    assert inner.executed == []
                    awaiting_human = False
                    signals.notify()
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("never parked under human control")

        task = asyncio.create_task(release_after_park())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Human then done",
            browser=browser,
            jev=_TakeoverOnFirstDecide(),  # type: ignore[arg-type]
            audit=audit,
            is_cancelled=lambda: False,
            is_paused=lambda: False,
            is_awaiting_human=lambda: awaiting_human,
            control_signals=signals,
            max_steps=10,
        ).run()
        await task

        assert outcome.status == RunStatus.SUCCEEDED
        assert "takeover_fresh_observe" in audit.types()
        assert len(observe_urls) >= 2
        assert observe_urls[0] == "https://example.com/before"
        assert "https://example.com/after-human" in observe_urls
        # Stale SCROLL must not have executed; only DONE.
        assert [a.kind for a in inner.executed] == [ActionKind.DONE]

    asyncio.run(_run())


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
        pytest.skip("Docker is required for takeover API tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-takeover-{uuid.uuid4().hex[:8]}"
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
                eng = create_engine(url, pool_pre_ping=True)
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                eng.dispose()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        else:
            subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)
            pytest.skip(f"Postgres never became ready: {last_error}")

        yield url
    finally:
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)


@pytest.fixture
def api_client(postgres_url: str) -> Iterator[TestClient]:
    """Yield a TestClient against a migrated empty database."""
    reset_control_hub_for_tests()
    reset_event_bus_for_tests()
    upgrade_head(database_url=postgres_url)
    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for table in ("human_approvals", "agent_events", "runs"):
            conn.execute(text(f"TRUNCATE {table} CASCADE"))

    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=False,
        allowed_hosts=(),
        csrf_trusted_origins=(),
    )
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        yield client
    engine.dispose()
    reset_control_hub_for_tests()
    reset_event_bus_for_tests()


def test_take_control_api_records_actor(api_client: TestClient) -> None:
    """POST /take-control sets awaiting_human and audits Authelia identity."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Need MFA")
        run.status = RunStatus.RUNNING.value
        session.commit()
        run_id = run.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/take-control",
        headers={"Remote-User": "alice"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.AWAITING_HUMAN.value

    session = factory()
    try:
        events = list(
            session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.event_type == "takeover_started",
                )
                .order_by(AgentEvent.seq)
            )
        )
        assert events
        assert events[-1].actor == "human"
        assert events[-1].metadata_.get("actor") == "alice"
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == RunStatus.AWAITING_HUMAN.value
    finally:
        session.close()


def test_release_control_api_records_actor(api_client: TestClient) -> None:
    """POST /release-control returns running and audits the operator."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Release me")
        run.status = RunStatus.AWAITING_HUMAN.value
        session.commit()
        run_id = run.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/release-control",
        headers={"Remote-User": "bob"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.RUNNING.value

    session = factory()
    try:
        events = list(
            session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.event_type == "takeover_ended",
                )
                .order_by(AgentEvent.seq)
            )
        )
        assert events
        assert events[-1].metadata_.get("actor") == "bob"
    finally:
        session.close()


def test_take_control_from_paused_is_stronger(api_client: TestClient) -> None:
    """Take-control escalates a paused run to awaiting_human."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Was paused")
        run.status = RunStatus.PAUSED.value
        session.commit()
        run_id = run.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/take-control",
        headers={"Remote-User": "carol"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.AWAITING_HUMAN.value


def test_take_control_rejects_awaiting_approval(api_client: TestClient) -> None:
    """Take-control is not allowed while an approval gate is open."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Need approval")
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.commit()
        run_id = run.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/take-control",
        headers={"Remote-User": "dave"},
    )
    assert response.status_code == 409


def test_take_control_requires_auth_when_enabled(postgres_url: str) -> None:
    """AUTH_REQUIRED rejects take-control without Remote-User."""
    reset_control_hub_for_tests()
    reset_event_bus_for_tests()
    upgrade_head(database_url=postgres_url)
    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for table in ("human_approvals", "agent_events", "runs"):
            conn.execute(text(f"TRUNCATE {table} CASCADE"))

    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=True,
        allowed_hosts=(),
        csrf_trusted_origins=("https://browser-use.docker.home.arpa",),
    )
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        factory = client.app.state.session_factory
        assert factory is not None
        session = factory()
        try:
            run = run_service.create_run(session, "Auth gate")
            run.status = RunStatus.RUNNING.value
            session.commit()
            run_id = run.id
        finally:
            session.close()

        denied = client.post(
            f"/api/runs/{run_id}/take-control",
            headers={"Origin": "https://browser-use.docker.home.arpa"},
        )
        assert denied.status_code == 401

        allowed = client.post(
            f"/api/runs/{run_id}/take-control",
            headers={
                "Remote-User": "alice",
                "Origin": "https://browser-use.docker.home.arpa",
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["status"] == RunStatus.AWAITING_HUMAN.value
    engine.dispose()
    reset_control_hub_for_tests()
    reset_event_bus_for_tests()


def test_service_take_and_release_roundtrip(postgres_url: str) -> None:
    """Domain service take/release updates status and audits both ends."""
    upgrade_head(database_url=postgres_url)
    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        for table in ("human_approvals", "agent_events", "runs"):
            conn.execute(text(f"TRUNCATE {table} CASCADE"))
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        run = run_service.create_run(session, "Roundtrip")
        run.status = RunStatus.RUNNING.value
        session.flush()
        takeover_service.take_control(session, run.id, actor="erin")
        session.commit()
        assert run.status == RunStatus.AWAITING_HUMAN.value
        takeover_service.release_control(session, run.id, actor="erin")
        session.commit()
        assert run.status == RunStatus.RUNNING.value
        types = [
            e.event_type
            for e in session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.seq)
            )
        ]
        assert "takeover_started" in types
        assert "takeover_ended" in types
    finally:
        session.close()
        engine.dispose()
