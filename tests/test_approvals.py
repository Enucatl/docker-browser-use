"""Tests for human approval gates (T021)."""

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

from browser_use_agent.agent.approvals import (
    ApprovalContext,
    default_needs_approval,
    load_approval_policy,
    needs_approval,
    wait_for_approval_decision,
)
from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.controls import get_control_hub, reset_control_hub_for_tests
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.api.app import create_app
from browser_use_agent.api.events_bus import reset_event_bus_for_tests
from browser_use_agent.config import AppSettings
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import AgentEvent, HumanApproval, Run
from browser_use_agent.policy.actions import (
    ActionKind,
    ActionParams,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import redact_for_audit
from browser_use_agent.services import approvals as approval_service
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


def _obs(
    *,
    url: str = "https://example.com/",
    candidates: list[CandidateElement] | None = None,
) -> BrowserObservation:
    """Build a small observation."""
    return BrowserObservation(
        url=url,
        title="Example",
        candidates=candidates
        or [
            CandidateElement(index=1, tag="a", name="Next", href="https://example.com/next"),
        ],
        suggested_urls=["https://example.com/next"],
    )


def test_policy_bitwarden_always_needs_approval() -> None:
    """Bitwarden actions always require approval."""
    for kind in (
        ActionKind.BITWARDEN_LOGIN,
        ActionKind.BITWARDEN_IDENTITY,
        ActionKind.BITWARDEN_CARD,
    ):
        req = needs_approval(AgentAction(kind=kind, target_index=0))
        assert req is not None
        assert kind.value in req.reason
        assert default_needs_approval(AgentAction(kind=kind, target_index=0)) is True


def test_policy_buy_click_needs_approval_benign_does_not() -> None:
    """Purchase-like clicks gate; ordinary links do not."""
    buy = CandidateElement(index=2, tag="button", name="Buy now", role="button")
    next_link = CandidateElement(index=1, tag="a", name="Next", href="https://example.com/next")
    obs = _obs(candidates=[next_link, buy])

    blocked = needs_approval(
        AgentAction(kind=ActionKind.CLICK, target_index=2),
        ApprovalContext(observation=obs),
    )
    assert blocked is not None
    assert "buy" in blocked.reason.lower() or "Buy" in blocked.reason

    allowed = needs_approval(
        AgentAction(kind=ActionKind.CLICK, target_index=1),
        ApprovalContext(observation=obs),
    )
    assert allowed is None

    nav_ok = needs_approval(
        AgentAction(
            kind=ActionKind.NAVIGATE,
            params=ActionParams(url="https://example.com/next"),
        ),
    )
    assert nav_ok is None

    nav_pay = needs_approval(
        AgentAction(
            kind=ActionKind.NAVIGATE,
            params=ActionParams(url="https://shop.example/checkout"),
        ),
    )
    assert nav_pay is not None


def test_policy_rules_are_ordered_and_money_keywords_default_deny(tmp_path) -> None:
    """The first matching data rule wins, including the default money rule."""
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        """
version = 1

[[rules]]
id = "specific"
reason_code = "test.specific"
message = "specific"
action_types = ["CLICK"]
element_text_regex = "buy"

[[rules]]
id = "fallback"
reason_code = "test.fallback"
message = "fallback"
action_types = ["CLICK"]
confidence_below = 0.9
""",
        encoding="utf-8",
    )
    policy = load_approval_policy(policy_path)
    action = AgentAction(kind=ActionKind.CLICK, target_index=1, confidence=0.1)
    context = ApprovalContext(
        observation=_obs(candidates=[CandidateElement(index=1, name="Buy now")]),
    )
    request = policy.evaluate(action, context)
    assert request is not None
    assert request.reason_code == "test.specific"
    assert request.policy_id == "specific"

    money = needs_approval(action, context)
    assert money is not None
    assert money.reason_code == "impact.money_keyword"


def test_approve_allows_execute_reject_fails_closed() -> None:
    """Approve continues execute; reject fails without executing the action."""

    async def _approve() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())
        statuses: list[RunStatus] = []

        browser = FakeBrowserPort([_obs()])
        jev = FakeJevClient(
            [
                FakeDecision(
                    operation="NAVIGATE",
                    navigate_url="https://example.com/next",
                    confidence=0.9,
                ),
                FakeDecision(operation="DONE", confidence=0.99),
            ],
        )

        async def approve_soon() -> None:
            # Wait until approval_requested appears.
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if "approval_requested" in audit.types():
                    break
                await asyncio.sleep(0.01)
            signals.set_approval_decision("granted")

        task = asyncio.create_task(approve_soon())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Navigate carefully",
            browser=browser,
            jev=jev,
            audit=audit,
            needs_approval=lambda action: action.kind == ActionKind.NAVIGATE,
            approval_timeout_seconds=5.0,
            set_status=statuses.append,
            control_signals=signals,
            is_cancelled=lambda: False,
            max_steps=10,
        ).run()
        await task

        assert outcome.status == RunStatus.SUCCEEDED
        assert "approval_requested" in audit.types()
        assert "action_requested" in audit.types()
        assert any(a.kind == ActionKind.NAVIGATE for a in browser.executed)
        assert RunStatus.AWAITING_APPROVAL in statuses

    async def _reject() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        browser = FakeBrowserPort([_obs()])
        jev = FakeJevClient(
            [
                FakeDecision(
                    operation="NAVIGATE",
                    navigate_url="https://example.com/next",
                    confidence=0.9,
                ),
            ],
        )

        async def reject_soon() -> None:
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if "approval_requested" in audit.types():
                    break
                await asyncio.sleep(0.01)
            signals.set_approval_decision("denied")

        task = asyncio.create_task(reject_soon())
        outcome = await AgentLoop(
            run_id=run_id,
            goal="Navigate carefully",
            browser=browser,
            jev=jev,
            audit=audit,
            needs_approval=lambda action: action.kind == ActionKind.NAVIGATE,
            approval_timeout_seconds=5.0,
            control_signals=signals,
            is_cancelled=lambda: False,
            max_steps=5,
        ).run()
        await task

        assert outcome.status == RunStatus.FAILED
        assert outcome.message == "approval_rejected"
        assert "approval_requested" in audit.types()
        assert "action_requested" not in audit.types()
        assert browser.executed == []

    asyncio.run(_approve())
    asyncio.run(_reject())


def test_approval_timeout_fails_closed() -> None:
    """Timeout without a human decision fails the run and skips execute."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())

        browser = FakeBrowserPort([_obs()])
        jev = FakeJevClient(
            [
                FakeDecision(
                    operation="NAVIGATE",
                    navigate_url="https://example.com/next",
                    confidence=0.9,
                ),
            ],
        )

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Navigate carefully",
            browser=browser,
            jev=jev,
            audit=audit,
            needs_approval=lambda action: True,
            approval_timeout_seconds=0.05,
            control_signals=signals,
            is_cancelled=lambda: False,
            max_steps=3,
        ).run()

        assert outcome.status == RunStatus.FAILED
        assert outcome.message == "approval_timeout"
        assert "approval_timeout" in audit.types()
        assert browser.executed == []

    asyncio.run(_run())


def test_wait_for_approval_decision_cancel() -> None:
    """Cancel during an approval wait returns cancelled."""

    async def _run() -> None:
        reset_control_hub_for_tests()
        run_id = uuid.uuid4()
        signals = get_control_hub().signals_for(run_id)
        signals.bind_loop(asyncio.get_running_loop())
        cancelled = False

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            nonlocal cancelled
            cancelled = True
            signals.notify()

        task = asyncio.create_task(cancel_soon())
        decision = await wait_for_approval_decision(
            signals=signals,
            is_cancelled=lambda: cancelled,
            timeout_seconds=2.0,
        )
        await task
        assert decision == "cancelled"

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
        pytest.skip("Docker is required for approval API tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-approvals-{uuid.uuid4().hex[:8]}"
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
        for table in (
            "human_approvals",
            "agent_events",
            "runs",
        ):
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


def test_approve_api_records_actor_and_wakes_worker(api_client: TestClient) -> None:
    """POST /approve records Authelia identity on human_approvals."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Need approval")
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.flush()
        event = session.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run.id).limit(1)
        ).first()
        assert event is not None
        pending = approval_service.create_pending_approval(
            session,
            run.id,
            event_id=event.id,
            reason="Buy now click",
            metadata={"kind": "CLICK"},
        )
        session.commit()
        run_id = run.id
        approval_id = pending.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/approve",
        json={"reason": "looks ok"},
        headers={"Remote-User": "alice"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == RunStatus.RUNNING.value

    session = factory()
    try:
        row = session.get(HumanApproval, approval_id)
        assert row is not None
        assert row.status == "granted"
        assert row.actor == "alice"
        assert row.decided_at is not None
        events = list(
            session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id, AgentEvent.event_type == "approval_granted")
                .order_by(AgentEvent.seq)
            )
        )
        assert events
        assert events[-1].actor == "human"
        assert events[-1].metadata_.get("actor") == "alice"
    finally:
        session.close()


def test_reject_api_fails_run_and_records_actor(api_client: TestClient) -> None:
    """POST /reject fails the run and stores the denier identity."""
    factory = api_client.app.state.session_factory
    assert factory is not None
    session = factory()
    try:
        run = run_service.create_run(session, "Need rejection")
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.flush()
        event = session.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run.id).limit(1)
        ).first()
        assert event is not None
        pending = approval_service.create_pending_approval(
            session,
            run.id,
            event_id=event.id,
            reason="Checkout navigate",
        )
        session.commit()
        run_id = run.id
        approval_id = pending.id
    finally:
        session.close()

    response = api_client.post(
        f"/api/runs/{run_id}/reject",
        json={"reason": "too risky"},
        headers={"X-Browser-Use-Dev-User": "bob"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.FAILED.value

    session = factory()
    try:
        row = session.get(HumanApproval, approval_id)
        assert row is not None
        assert row.status == "denied"
        assert row.actor == "bob"
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
    finally:
        session.close()


def test_approve_wrong_status_conflicts(api_client: TestClient) -> None:
    """Approve on a non-awaiting run returns 409."""
    create = api_client.post("/api/runs", json={"goal": "queued only"})
    assert create.status_code == 201
    run_id = create.json()["id"]
    response = api_client.post(
        f"/api/runs/{run_id}/approve",
        headers={"Remote-User": "alice"},
    )
    assert response.status_code == 409
