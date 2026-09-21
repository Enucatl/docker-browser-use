"""Tests for pause / resume / cancel / retry controls (T020)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from browser_use_agent.agent.browser_port import ActionExecutionResult, FakeBrowserPort
from browser_use_agent.agent.controls import (
    get_control_hub,
    reset_control_hub_for_tests,
)
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.api.app import create_app
from browser_use_agent.api.events_bus import get_event_bus, reset_event_bus_for_tests
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


class SlowFakeBrowserPort(FakeBrowserPort):
    """Fake browser that sleeps during execute to expose control races."""

    def __init__(
        self,
        observations: list[BrowserObservation] | None = None,
        *,
        execute_delay: float = 0.2,
        **kwargs: Any,
    ) -> None:
        """Create a slow fake browser.

        Args:
            observations: Scripted observations.
            execute_delay: Seconds to sleep inside :meth:`execute`.
            **kwargs: Forwarded to :class:`FakeBrowserPort`.
        """
        super().__init__(observations, **kwargs)
        self.execute_delay = execute_delay
        self.execute_started = asyncio.Event()
        self.execute_finished_count = 0

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Sleep, then delegate to the base fake execute.

        Args:
            action: Action to simulate.

        Returns:
            Synthetic execution result.
        """
        self.execute_started.set()
        await asyncio.sleep(self.execute_delay)
        result = await super().execute(action)
        self.execute_finished_count += 1
        return result


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


async def test_pause_blocks_further_actions_until_resume() -> None:
    """Paused run executes no further browser actions until resume."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        paused = False
        cancelled = False

        browser = SlowFakeBrowserPort(
            [_obs(), _obs(url="https://example.com/2"), _obs(url="https://example.com/3")],
            execute_delay=0.15,
        )
        jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", confidence=0.99),
            ],
        )

        def is_paused() -> bool:
            return paused

        def is_cancelled() -> bool:
            return cancelled

        async def pause_during_first_execute() -> None:
            nonlocal paused
            await browser.execute_started.wait()
            # Arm pause while the first execute is in flight so the loop parks
            # at the next control gate (no second browser action).
            paused = True
            signals.wake.clear()
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if browser.execute_finished_count >= 1 and len(browser.executed) == 1:
                    await asyncio.sleep(0.1)
                    break
                await asyncio.sleep(0.02)
            assert len(browser.executed) == 1
            paused = False
            signals.notify()

        pause_task = asyncio.create_task(pause_during_first_execute())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Scroll twice then done",
            browser=browser,
            jev=jev,
            audit=audit,
            is_cancelled=is_cancelled,
            is_paused=is_paused,
            control_signals=signals,
            max_steps=10,
        ).run()
        await pause_task

        assert outcome.status == RunStatus.SUCCEEDED
        assert len(browser.executed) == 3
        assert browser.executed[-1].kind == ActionKind.DONE

    await _run()


async def test_cancel_during_slow_step_ends_cancelled() -> None:
    """Cancel during a slow execute stops before the next action."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        cancelled = False
        browser = SlowFakeBrowserPort(
            [_obs(), _obs(url="https://example.com/2")],
            execute_delay=0.2,
        )
        jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", confidence=0.99),
            ],
        )

        async def cancel_mid_first_execute() -> None:
            nonlocal cancelled
            await browser.execute_started.wait()
            cancelled = True
            signals.notify()

        cancel_task = asyncio.create_task(cancel_mid_first_execute())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Scroll then done",
            browser=browser,
            jev=jev,
            audit=audit,
            is_cancelled=lambda: cancelled,
            is_paused=lambda: False,
            control_signals=signals,
            max_steps=10,
        ).run()
        await cancel_task

        assert outcome.status == RunStatus.CANCELLED
        assert "run_cancelled" in audit.types()
        # In-flight execute may complete; no further actions.
        assert len(browser.executed) == 1

    await _run()


async def test_pause_then_cancel_race() -> None:
    """Cancel while paused unblocks the waiter and ends cancelled."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        paused = True
        cancelled = False
        browser = FakeBrowserPort([_obs()])
        jev = FakeJevClient(
            [FakeDecision(operation="DONE", confidence=0.99)],
        )

        async def cancel_while_paused() -> None:
            nonlocal cancelled, paused
            await asyncio.sleep(0.05)
            cancelled = True
            paused = False
            signals.notify()

        cancel_task = asyncio.create_task(cancel_while_paused())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Never start",
            browser=browser,
            jev=jev,
            audit=audit,
            is_cancelled=lambda: cancelled,
            is_paused=lambda: paused,
            control_signals=signals,
            max_steps=5,
        ).run()
        await cancel_task

        assert outcome.status == RunStatus.CANCELLED
        assert browser.executed == []
        assert "run_cancelled" in audit.types()

    await _run()


async def test_step_retry_reobserves_after_failed_execute() -> None:
    """Armed step retry re-observes and decides instead of failing the run."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.arm_step_retry()

        browser = FakeBrowserPort([_obs(), _obs(url="https://example.com/retry")])
        original_execute = browser.execute

        async def execute_once_fail(action: AgentAction) -> ActionExecutionResult:
            if not browser.executed and action.kind == ActionKind.SCROLL:
                browser.executed.append(action)
                return ActionExecutionResult(ok=False, error="transient", metadata={})
            return await original_execute(action)

        browser.execute = execute_once_fail  # type: ignore[method-assign]

        jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", done_message="ok", confidence=0.99),
            ],
        )

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Recover from failure",
            browser=browser,
            jev=jev,
            audit=audit,
            control_signals=signals,
            consume_step_retry=signals.consume_step_retry,
            max_steps=10,
        ).run()

        assert outcome.status == RunStatus.SUCCEEDED
        assert "step_retry" in audit.types()
        assert "run_failed" not in audit.types()
        assert any(a.kind == ActionKind.DONE for a in browser.executed)

    await _run()


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
        pytest.skip("Docker is required for run control API tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-controls-{uuid.uuid4().hex[:8]}"
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
    upgrade_head(database_url=postgres_url)
    reset_control_hub_for_tests()
    reset_event_bus_for_tests()

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE runs CASCADE"))

    settings = AppSettings(host="127.0.0.1", port=8000, database=None)
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        yield client

    engine.dispose()
    reset_control_hub_for_tests()


def test_pause_resume_cancel_api_audits_and_ws(
    api_client: TestClient,
    postgres_url: str,
) -> None:
    """API transitions persist status, audit events, and publish on the WS bus."""
    bus = get_event_bus()
    created = api_client.post("/api/runs", json={"goal": "control me"}).json()
    run_id = uuid.UUID(created["id"])
    queue = bus.subscribe(run_id)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = RunStatus.RUNNING.value
        run.started_at = datetime.now(UTC)
        session.commit()
    finally:
        session.close()

    paused = api_client.post(f"/api/runs/{run_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == RunStatus.PAUSED.value

    resumed = api_client.post(f"/api/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == RunStatus.RUNNING.value

    cancelled = api_client.post(f"/api/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED.value
    assert cancelled.json()["finished_at"] is not None

    conflict = api_client.post(f"/api/runs/{run_id}/pause")
    assert conflict.status_code == 409

    session = factory()
    try:
        events = list(
            session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.seq)
            ).all()
        )
        types = [e.event_type for e in events]
        assert "run_paused" in types
        assert "run_resumed" in types
        assert "run_cancelled" in types
    finally:
        session.close()
        engine.dispose()

    live = bus.drain(queue, max_items=64)
    bus.unsubscribe(run_id, queue)
    live_types = [m.event_type for m in live if m.type == "event"]
    assert "run_paused" in live_types
    assert "run_resumed" in live_types
    assert "run_cancelled" in live_types


def test_retry_failed_run_requeues(api_client: TestClient, postgres_url: str) -> None:
    """POST /retry on a failed run clears finished_at and sets queued."""
    created = api_client.post("/api/runs", json={"goal": "retry me"}).json()
    run_id = uuid.UUID(created["id"])

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        run = session.get(Run, run_id)
        assert run is not None
        run.status = RunStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        session.commit()
    finally:
        session.close()

    # Avoid starting a real browser worker: clear worker on app.
    api_client.app.state.run_worker = None
    retried = api_client.post(f"/api/runs/{run_id}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == RunStatus.QUEUED.value
    assert body["finished_at"] is None

    session = factory()
    try:
        events = list(
            session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id, AgentEvent.event_type == "run_retry_requested")
                .order_by(AgentEvent.seq)
            ).all()
        )
        assert len(events) == 1
        assert events[0].metadata_.get("mode") == "requeue"
    finally:
        session.close()
        engine.dispose()


def test_service_pause_idempotent(postgres_url: str) -> None:
    """pause_run is idempotent when already paused."""
    upgrade_head(database_url=postgres_url)
    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE runs CASCADE"))
        run = run_service.create_run(session, "pause twice")
        run.status = RunStatus.RUNNING.value
        session.commit()

        first = run_service.pause_run(session, run.id)
        session.commit()
        assert first.status == RunStatus.PAUSED.value
        second = run_service.pause_run(session, run.id)
        session.commit()
        assert second.status == RunStatus.PAUSED.value
    finally:
        session.close()
        engine.dispose()
