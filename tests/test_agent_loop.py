"""Tests for the observe → Jev → execute agent loop (T015)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from browser_use_agent.agent.approvals import default_needs_approval
from browser_use_agent.agent.browser_port import FakeBrowserPort, TypeTextBlockedError
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.agent.worker import RunWorker, RunWorkerSettings
from browser_use_agent.policy.actions import (
    ActionKind,
    ActionParams,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import REDACTED, redact_for_audit


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
        """Append a redacted event to the in-memory list.

        Args:
            run_id: Owning run.
            event_type: Logical event kind.
            payload: Structured metadata.
            actor: Causing actor.
            step_id: Optional step grouping.
            parent_event_id: Unused (API parity).
            url: Unused (API parity).
            tab_id: Unused (API parity).
            duration_ms: Unused (API parity).
            occurred_at: Unused (API parity).
            event_id: Optional event id.

        Returns:
            Recorded event handle.
        """
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
    *, url: str = "https://example.com/", password_in_title: bool = False
) -> BrowserObservation:
    """Build a small observation with one clickable link."""
    return BrowserObservation(
        url=url,
        title="Secret password=hunter2 page" if password_in_title else "Example",
        candidates=[
            CandidateElement(index=1, tag="a", name="Next", href="https://example.com/next"),
        ],
        suggested_urls=["https://example.com/next"],
    )


async def test_fake_jev_reaches_done_with_audit_chain() -> None:
    """A scripted Jev client navigates then DONE with a coherent event chain."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort(
            [
                _obs(),
                BrowserObservation(
                    url="https://example.com/next",
                    title="Next",
                    candidates=[],
                    suggested_urls=[],
                ),
            ],
        )
        jev = FakeJevClient(
            [
                FakeDecision(
                    operation="NAVIGATE",
                    navigate_url="https://example.com/next",
                    confidence=0.9,
                ),
                FakeDecision(operation="DONE", done_message="finished", confidence=0.99),
            ],
        )

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Open the next page and finish",
            browser=browser,
            jev=jev,
            audit=audit,
            max_steps=10,
        ).run()

        assert outcome.status == RunStatus.SUCCEEDED
        assert outcome.steps_completed == 2
        types = audit.types()
        assert types[0] == "run_started"
        assert "observation_captured" in types
        assert types.count("decision") == 2
        assert types.count("action_requested") == 2
        assert types.count("action_completed") == 2
        assert types[-1] == "run_succeeded"
        assert len(browser.executed) == 2
        assert browser.executed[0].kind == ActionKind.NAVIGATE
        assert browser.executed[1].kind == ActionKind.DONE
        # Explicit step_ids on decision / action events.
        decision_steps = {e.step_id for e in audit.events if e.event_type == "decision"}
        assert None not in decision_steps
        assert len(decision_steps) == 2

    await _run()


async def test_predecision_does_not_block_event_loop() -> None:
    """An async Jev client cannot freeze control signals or other tasks."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_obs()])
        fake = FakeJevClient([FakeDecision(operation="DONE", confidence=0.99)])

        class SlowJev:
            async def decide(self, request: Any) -> Any:
                await asyncio.sleep(0.1)
                return await fake.decide(request)

        heartbeat = 0

        async def tick() -> None:
            nonlocal heartbeat
            while True:
                heartbeat += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(tick())
        try:
            outcome = await AgentLoop(
                run_id=run_id,
                goal="Finish without blocking",
                browser=browser,
                jev=SlowJev(),
                audit=audit,
            ).run()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

        assert outcome.status == RunStatus.SUCCEEDED
        assert heartbeat >= 5


async def test_cooperative_cancel_between_steps() -> None:
    """Cancel check between steps stops the loop before further executes."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_obs(), _obs(url="https://example.com/2")])
        jev = FakeJevClient(
            [
                FakeDecision(operation="SCROLL", scroll_direction="DOWN", confidence=0.8),
                FakeDecision(operation="DONE", confidence=0.99),
            ],
        )

        def is_cancelled() -> bool:
            """Cancel once the first action has executed."""
            return len(browser.executed) >= 1

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Scroll then stop",
            browser=browser,
            jev=jev,
            audit=audit,
            is_cancelled=is_cancelled,
            max_steps=10,
        ).run()

        assert outcome.status == RunStatus.CANCELLED
        assert "run_cancelled" in audit.types()
        assert len(browser.executed) == 1
        assert browser.executed[0].kind == ActionKind.SCROLL

    await _run()


async def test_secrets_redacted_in_stored_payloads() -> None:
    """Secrets do not appear in stored audit payloads (redaction path used)."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_obs(password_in_title=True)])
        secret_goal = "Use password=hunter2 to finish"
        jev = FakeJevClient([FakeDecision(operation="DONE", done_message="ok", confidence=0.99)])

        outcome = await AgentLoop(
            run_id=run_id,
            goal=secret_goal,
            browser=browser,
            jev=jev,
            audit=audit,
        ).run()

        assert outcome.status == RunStatus.SUCCEEDED
        blob = str([e.payload for e in audit.events])
        assert "hunter2" not in blob
        started = next(e for e in audit.events if e.event_type == "run_started")
        assert started.payload.get("goal") == REDACTED or REDACTED in str(started.payload)

    await _run()


async def test_type_text_fail_closed_without_text_llm() -> None:
    """TYPE_TEXT without a text LLM client fails closed with audit events."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort(
            [
                BrowserObservation(
                    url="https://example.com/form",
                    title="Form",
                    candidates=[
                        CandidateElement(index=2, tag="input", name="q", is_editable=True),
                    ],
                ),
            ],
        )
        jev = FakeJevClient(
            [FakeDecision(operation="TYPE_TEXT", target_key="2", confidence=0.9)],
        )

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Type into the search box",
            browser=browser,
            jev=jev,
            audit=audit,
        ).run()

        assert outcome.status == RunStatus.FAILED
        assert "TYPE_TEXT" in (outcome.message or "") or "text LLM" in (outcome.message or "")
        assert "model_call_failed" in audit.types()
        assert "run_failed" in audit.types()
        with pytest.raises(TypeTextBlockedError):
            await FakeBrowserPort().execute(
                AgentAction(kind=ActionKind.TYPE_TEXT, target_index=1, params=ActionParams()),
            )

    await _run()


async def test_needs_approval_hook_defaults_false_and_can_pause() -> None:
    """DONE never needs approval; a custom hook parks until granted."""
    assert (
        default_needs_approval(
            AgentAction(kind=ActionKind.DONE, params=ActionParams(message="x")),
        )
        is False
    )

    async def _run() -> None:
        from browser_use_agent.agent.controls import get_control_hub, reset_control_hub_for_tests

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
                FakeDecision(operation="DONE", done_message="ok", confidence=0.99),
            ],
        )

        async def grant_soon() -> None:
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                if "approval_requested" in audit.types():
                    break
                await asyncio.sleep(0.01)
            signals.set_approval_decision("granted")

        grant_task = asyncio.create_task(grant_soon())
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
            max_steps=10,
        ).run()
        await grant_task

        assert outcome.status == RunStatus.SUCCEEDED
        assert "approval_requested" in audit.types()
        assert any(a.kind == ActionKind.NAVIGATE for a in browser.executed)

    await _run()


async def test_run_worker_with_fake_port_reaches_done() -> None:
    """RunWorker drives a queued run to succeeded using injected fakes."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        run = MagicMock()
        run.id = run_id
        run.goal = "Finish quickly"
        run.profile_id = None
        run.status = RunStatus.QUEUED.value
        run.started_at = None
        run.finished_at = None
        run.updated_at = now

        def session_factory() -> MagicMock:
            session = MagicMock()
            session.get.return_value = run
            return session

        audit_trail = RecordingAuditWriter()

        class _SessionAudit:
            def __init__(self, _session: Any) -> None:
                pass

            def append(self, *args: Any, **kwargs: Any) -> Any:
                return audit_trail.append(*args, **kwargs)

        browser = FakeBrowserPort(
            [BrowserObservation(url="about:blank", title="", candidates=[])],
        )
        jev = FakeJevClient(
            [FakeDecision(operation="DONE", done_message="ok", confidence=0.99)],
        )

        import browser_use_agent.agent.worker as worker_mod

        worker = RunWorker(
            session_factory,
            settings=RunWorkerSettings(max_concurrent_runs=1, max_steps=5),
            browser_port_factory=lambda _rid, _session: browser,
            jev_factory=lambda: jev,
        )

        original_audit = worker_mod.AuditWriter
        original_get_run = worker_mod.get_run
        worker_mod.AuditWriter = _SessionAudit  # type: ignore[misc, assignment]
        worker_mod.get_run = lambda _s, rid: run  # type: ignore[assignment]
        try:
            task = await worker.start_run(run_id)
            outcome = await task
        finally:
            worker_mod.AuditWriter = original_audit  # type: ignore[misc]
            worker_mod.get_run = original_get_run  # type: ignore[assignment]

        assert outcome.status == RunStatus.SUCCEEDED
        assert run.status == RunStatus.SUCCEEDED.value
        assert "run_started" in audit_trail.types()
        assert "run_succeeded" in audit_trail.types()
        assert "decision" in audit_trail.types()

    await _run()


def test_redact_for_audit_used_on_decision_shaped_payload() -> None:
    """Sanity: decision-shaped dicts with secrets are redacted before store."""
    payload = {
        "kind": "DONE",
        "confidence": 0.9,
        "password": "hunter2",
        "alternatives": ["CLICK"],
    }
    out = redact_for_audit(payload)
    assert isinstance(out, dict)
    assert out["password"] == REDACTED
    assert out["kind"] == "DONE"
