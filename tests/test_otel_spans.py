"""Tests for optional OpenTelemetry loop instrumentation (T030)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from browser_use_agent.agent.approvals import ApprovalRequest
from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.controls import RunControlSignals
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.policy.actions import ActionKind, BrowserObservation, CandidateElement
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.policy.text_llm import FakeTextLLMClient
from browser_use_agent.telemetry.otel import OTelSettings, Telemetry


def _audit() -> MagicMock:
    """Return an audit-shaped sink without storing test payloads."""
    audit = MagicMock()
    audit.append.return_value = MagicMock(id=uuid.uuid4())
    return audit


def _form_observation() -> BrowserObservation:
    """Return a safe editable-field observation."""
    return BrowserObservation(
        url="https://example.test/form",
        title="Form",
        candidates=[CandidateElement(index=2, tag="input", name="query", is_editable=True)],
    )


async def test_loop_emits_safe_phase_spans_and_metrics() -> None:
    """Loop phases carry IDs and never copy goal or typed text into spans."""
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    telemetry = Telemetry(
        OTelSettings(enabled=True, service_name="test-agent"),
        span_exporter=spans,
        metric_reader=metrics,
    )
    run_id = uuid.uuid4()

    async def _run() -> None:
        observation = _form_observation()
        await AgentLoop(
            run_id=run_id,
            goal="type hunter2 into the form",
            browser=FakeBrowserPort([observation, observation]),
            jev=FakeJevClient(
                [
                    FakeDecision(operation="TYPE_TEXT", target_key="2"),
                    FakeDecision(operation="DONE", done_message="finished"),
                ]
            ),
            text_llm=FakeTextLLMClient(texts=("hunter2",)),
            audit=_audit(),
            telemetry=telemetry,
        ).run()

        signals = RunControlSignals()
        signals.bind_loop(asyncio.get_running_loop())
        approval_audit = _audit()
        approval_loop = AgentLoop(
            run_id=uuid.uuid4(),
            goal="approve navigation",
            browser=FakeBrowserPort([observation, observation]),
            jev=FakeJevClient(
                [
                    FakeDecision(operation="NAVIGATE", navigate_url="https://example.test/next"),
                    FakeDecision(operation="DONE", done_message="finished"),
                ]
            ),
            audit=approval_audit,
            needs_approval=lambda action: (
                ApprovalRequest(reason="test approval", action_kind=ActionKind.NAVIGATE)
                if action.kind == ActionKind.NAVIGATE
                else None
            ),
            control_signals=signals,
            telemetry=telemetry,
        )
        approval_task = asyncio.create_task(approval_loop.run())
        while not any(
            call.args[1] == "approval_requested" for call in approval_audit.append.call_args_list
        ):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        signals.set_approval_decision("granted")
        await approval_task

    await _run()

    finished = spans.get_finished_spans()
    names = {span.name for span in finished}
    assert {"step", "observe", "jev", "text_llm", "execute", "approve_wait"} <= names
    assert all(span.attributes["run_id"] for span in finished)
    assert any("step_id" in span.attributes for span in finished if span.name != "run")
    assert all("hunter2" not in str(span.attributes) for span in finished)

    metric_names = {
        metric.name
        for metric in metrics.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
    }
    assert {
        "browser_use.agent.step.duration",
        "browser_use.agent.approval.wait.duration",
    } <= metric_names
