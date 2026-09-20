"""Observe → Jev decide → execute control loop with audit breadcrumbs.

This is the Phase-1 heart of the agent. Keep the step body linear and explicit:
each iteration gets a fresh ``step_id``, writes audit events, and cooperatively
checks cancellation between phases.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from browser_use_agent.agent.browser_port import BrowserPort, TypeTextBlockedError
from browser_use_agent.audit.browser_actions import BrowserActionWriter
from browser_use_agent.audit.model_calls import ModelCallWriter
from browser_use_agent.audit.screenshots import is_destructive_action
from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_adapter import JevAdapter, JevAdapterError
from browser_use_agent.policy.jev_client import JevClient, JevClientError, JevRequest, JevResponse
from browser_use_agent.policy.text_llm import TextLLMClient, TextLLMError, maybe_fill_type_text
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import redact_for_audit

logger = logging.getLogger(__name__)

NeedsApprovalHook = Callable[[AgentAction], bool]
# force_reason is optional (approval / error / destructive); 3-arg hooks still work
# when the loop calls without a fourth positional argument.
CheckpointHook = Callable[
    [uuid.UUID, uuid.UUID, BrowserObservation],
    Awaitable[None] | None,
]
CheckpointHookWithReason = Callable[
    [uuid.UUID, uuid.UUID, BrowserObservation, str | None],
    Awaitable[None] | None,
]
ScreenshotHook = CheckpointHook
ScreenshotHookWithReason = CheckpointHookWithReason
CancelCheck = Callable[[], bool | Awaitable[bool]]


class AgentLoopError(RuntimeError):
    """Raised when the control loop cannot continue."""


class AuditAppend(Protocol):
    """Minimal audit writer surface used by the loop."""

    def append(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "system",
        step_id: uuid.UUID | None = None,
        parent_event_id: uuid.UUID | None = None,
        url: str | None = None,
        tab_id: str | None = None,
        duration_ms: int | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> Any:
        """Append one audit event."""


def default_needs_approval(_action: AgentAction) -> bool:
    """Return whether an action needs human approval (T021 hook).

    Default is always ``False`` until the approval policy lands.

    Args:
        _action: Candidate action (ignored).

    Returns:
        Always ``False``.
    """
    return False


@dataclass(slots=True)
class LoopOutcome:
    """Terminal result of one :meth:`AgentLoop.run` invocation.

    Attributes:
        status: Terminal or waiting run status.
        message: Optional summary for audit / API.
        steps_completed: Number of observe→decide→execute iterations finished.
        last_step_id: UUID of the last step, if any.
    """

    status: RunStatus
    message: str | None = None
    steps_completed: int = 0
    last_step_id: uuid.UUID | None = None


class AgentLoop:
    """Single-run observe → Jev → execute orchestrator.

    Attributes:
        run_id: Owning run identifier.
        goal: Operator natural-language goal.
        browser: Observation / execution port.
        jev: Decision client (live or fake).
        text_llm: Optional small text LLM for ``TYPE_TEXT`` fills (T016).
        audit: Audit writer (must redact; :class:`AuditWriter` does).
        model_calls: Optional T017 ``model_calls`` table writer.
        browser_actions: Optional T017 ``browser_actions`` table writer.
        adapter: Observation ↔ Jev mapper.
        max_steps: Hard cap to avoid infinite loops.
        needs_approval: Hook for T021; defaults to always False.
        checkpoint: Optional T018 hook; skipped when ``None``.
        screenshot: Optional T019 hook; skipped when ``None``.
        is_cancelled: Cooperative cancel check between phases.
    """

    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        goal: str,
        browser: BrowserPort,
        jev: JevClient,
        audit: AuditAppend,
        adapter: JevAdapter | None = None,
        text_llm: TextLLMClient | None = None,
        model_calls: ModelCallWriter | None = None,
        browser_actions: BrowserActionWriter | None = None,
        max_steps: int = 50,
        needs_approval: NeedsApprovalHook | None = None,
        checkpoint: CheckpointHook | CheckpointHookWithReason | None = None,
        screenshot: ScreenshotHook | ScreenshotHookWithReason | None = None,
        is_cancelled: CancelCheck | None = None,
        commit: Callable[[], None] | None = None,
    ) -> None:
        """Create a control loop for one run.

        Args:
            run_id: Owning run id.
            goal: Operator goal text.
            browser: Browser port (real or fake).
            jev: Jev client.
            audit: Audit append sink.
            adapter: Optional adapter; a default is constructed when omitted.
            text_llm: Optional text LLM gate for ``TYPE_TEXT`` (T016).
            model_calls: Optional normalized model-call writer (T017).
            browser_actions: Optional normalized browser-action writer (T017).
            max_steps: Maximum observe/decide/execute iterations.
            needs_approval: Approval gate hook (default always False).
            checkpoint: Optional checkpoint writer (T018). May accept an optional
                fourth ``force_reason`` argument for approval/error boundaries.
            screenshot: Optional screenshot writer (T019). Same call shape as
                ``checkpoint``; forced on approval/error/destructive actions.
            is_cancelled: Returns True when the run should stop cooperatively.
            commit: Optional callback after each audit batch (e.g. session.commit).
        """
        self.run_id = run_id
        self.goal = goal
        self.browser = browser
        self.jev = jev
        self.text_llm = text_llm
        self.audit = audit
        self.model_calls = model_calls
        self.browser_actions = browser_actions
        self.adapter = adapter if adapter is not None else JevAdapter()
        self.max_steps = max(1, max_steps)
        self.needs_approval = needs_approval or default_needs_approval
        self.checkpoint = checkpoint
        self.screenshot = screenshot
        self.is_cancelled = is_cancelled
        self._commit = commit
        self._history: list[str] = []

    async def run(self) -> LoopOutcome:
        """Run the control loop until DONE, error, cancel, or approval pause.

        Returns:
            Terminal :class:`LoopOutcome`.
        """
        self.audit.append(
            self.run_id,
            "run_started",
            {"goal": self.goal, "max_steps": self.max_steps},
            actor="system",
        )
        self._flush()

        steps = 0
        last_step_id: uuid.UUID | None = None

        while steps < self.max_steps:
            if await self._cancelled():
                self.audit.append(
                    self.run_id,
                    "run_cancelled",
                    {"reason": "cooperative_cancel", "steps_completed": steps},
                    actor="system",
                    step_id=last_step_id,
                )
                self._flush()
                return LoopOutcome(
                    status=RunStatus.CANCELLED,
                    message="cancelled",
                    steps_completed=steps,
                    last_step_id=last_step_id,
                )

            step_id = uuid.uuid4()
            last_step_id = step_id
            steps += 1

            try:
                outcome = await self._step(step_id, step_number=steps)
            except (TypeTextBlockedError, TextLLMError) as exc:
                self.audit.append(
                    self.run_id,
                    "action_failed",
                    {"kind": ActionKind.TYPE_TEXT.value, "error": str(exc)},
                    actor="agent",
                    step_id=step_id,
                )
                self.audit.append(
                    self.run_id,
                    "run_failed",
                    {"error": str(exc), "steps_completed": steps},
                    actor="system",
                    step_id=step_id,
                )
                self._flush()
                return LoopOutcome(
                    status=RunStatus.FAILED,
                    message=str(exc),
                    steps_completed=steps,
                    last_step_id=step_id,
                )
            except (JevAdapterError, JevClientError, AgentLoopError) as exc:
                logger.exception("Agent loop step %s failed", step_id)
                self.audit.append(
                    self.run_id,
                    "run_failed",
                    {"error": str(exc), "steps_completed": steps},
                    actor="system",
                    step_id=step_id,
                )
                self._flush()
                return LoopOutcome(
                    status=RunStatus.FAILED,
                    message=str(exc),
                    steps_completed=steps,
                    last_step_id=step_id,
                )
            except Exception as exc:
                logger.exception("Unexpected agent loop failure on step %s", step_id)
                self.audit.append(
                    self.run_id,
                    "run_failed",
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "steps_completed": steps,
                    },
                    actor="system",
                    step_id=step_id,
                )
                self._flush()
                return LoopOutcome(
                    status=RunStatus.FAILED,
                    message=str(exc),
                    steps_completed=steps,
                    last_step_id=step_id,
                )

            if outcome is not None:
                return LoopOutcome(
                    status=outcome.status,
                    message=outcome.message,
                    steps_completed=steps,
                    last_step_id=step_id,
                )

        self.audit.append(
            self.run_id,
            "run_failed",
            {"error": "max_steps_exceeded", "max_steps": self.max_steps},
            actor="system",
            step_id=last_step_id,
        )
        self._flush()
        return LoopOutcome(
            status=RunStatus.FAILED,
            message="max_steps_exceeded",
            steps_completed=steps,
            last_step_id=last_step_id,
        )

    async def _step(self, step_id: uuid.UUID, *, step_number: int) -> LoopOutcome | None:
        """Execute one observe → decide → execute cycle.

        Args:
            step_id: Explicit step grouping id for audit events.
            step_number: 1-based step counter.

        Returns:
            A :class:`LoopOutcome` when the run should stop; otherwise ``None``.
        """
        if await self._cancelled():
            self.audit.append(
                self.run_id,
                "run_cancelled",
                {"reason": "cooperative_cancel_before_observe", "step_number": step_number},
                actor="system",
                step_id=step_id,
            )
            self._flush()
            return LoopOutcome(status=RunStatus.CANCELLED, message="cancelled")

        # --- observe ---
        t0 = time.perf_counter()
        observation = await self.browser.observe()
        observe_ms = int((time.perf_counter() - t0) * 1000)
        self.audit.append(
            self.run_id,
            "observation_captured",
            _observation_audit_payload(observation),
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
            duration_ms=observe_ms,
        )
        self._flush()

        await self._maybe_checkpoint(step_id, observation)
        await self._maybe_screenshot(step_id, observation)

        if await self._cancelled():
            self.audit.append(
                self.run_id,
                "run_cancelled",
                {"reason": "cooperative_cancel_before_decide", "step_number": step_number},
                actor="system",
                step_id=step_id,
            )
            self._flush()
            return LoopOutcome(status=RunStatus.CANCELLED, message="cancelled")

        # --- Jev decide ---
        history_summary = " | ".join(self._history[-8:])
        request = self.adapter.to_jev_request(observation, self.goal, history_summary)
        t1 = time.perf_counter()
        response = self.jev.decide(request)
        decide_ms = int((time.perf_counter() - t1) * 1000)
        action = self.adapter.from_jev_response(response, observation=observation)

        decision_payload = {
            "kind": action.kind.value,
            "target_index": action.target_index,
            "confidence": action.confidence,
            "probabilities": action.probabilities,
            "target_confidence": action.target_confidence,
            "target_probabilities": action.target_probabilities,
            "alternatives": action.alternatives,
            "params": action.params.model_dump(mode="json"),
            "rationale": action.rationale,
            "raw_answers": action.raw_answers,
            "model": response.model,
        }
        decision_event = self.audit.append(
            self.run_id,
            "decision",
            decision_payload,
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
            duration_ms=decide_ms,
        )
        self._record_jev_model_call(
            event=decision_event,
            request=request,
            response=response,
            action=action,
            latency_ms=decide_ms,
        )
        self._flush()

        if self.needs_approval(action):
            await self._maybe_checkpoint(step_id, observation, force_reason="approval")
            await self._maybe_screenshot(step_id, observation, force_reason="approval")
            self.audit.append(
                self.run_id,
                "approval_requested",
                {
                    "kind": action.kind.value,
                    "target_index": action.target_index,
                    "confidence": action.confidence,
                },
                actor="agent",
                step_id=step_id,
                url=observation.url or None,
            )
            self._flush()
            return LoopOutcome(
                status=RunStatus.AWAITING_APPROVAL,
                message="needs_approval",
                last_step_id=step_id,
            )

        if await self._cancelled():
            self.audit.append(
                self.run_id,
                "run_cancelled",
                {"reason": "cooperative_cancel_before_execute", "step_number": step_number},
                actor="system",
                step_id=step_id,
            )
            self._flush()
            return LoopOutcome(status=RunStatus.CANCELLED, message="cancelled")

        # --- text LLM gate (TYPE_TEXT only) ---
        self._fill_type_text_if_needed(action, observation=observation, step_id=step_id)

        # --- execute ---
        target_meta = _target_forensics(observation, action)
        requested_event = self.audit.append(
            self.run_id,
            "action_requested",
            {
                "kind": action.kind.value,
                "target_index": action.target_index,
                "params": action.params.model_dump(mode="json"),
            },
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
        )
        self._record_browser_action(
            event=requested_event,
            action=action,
            status="requested",
            observation=observation,
            target_meta=target_meta,
            result_message=None,
            page_changed=None,
            duration_ms=None,
            exec_meta=None,
        )
        self._flush()

        t2 = time.perf_counter()
        result = await self.browser.execute(action)
        exec_ms = int((time.perf_counter() - t2) * 1000)
        page_changed = _page_changed(observation, result.metadata)

        if result.ok:
            completed_event = self.audit.append(
                self.run_id,
                "action_completed",
                {
                    "kind": action.kind.value,
                    "message": result.message,
                    "done": result.done,
                    "metadata": result.metadata,
                },
                actor="agent",
                step_id=step_id,
                url=observation.url or None,
                duration_ms=exec_ms,
            )
            self._record_browser_action(
                event=completed_event,
                action=action,
                status="completed",
                observation=observation,
                target_meta=target_meta,
                result_message=result.message,
                page_changed=page_changed,
                duration_ms=exec_ms,
                exec_meta=result.metadata,
            )
            self._flush()
            self._history.append(f"{action.kind.value}:{result.message or 'ok'}")
            if is_destructive_action(action.kind):
                await self._maybe_screenshot(
                    step_id,
                    observation,
                    force_reason="destructive",
                )
        else:
            await self._maybe_checkpoint(step_id, observation, force_reason="error")
            await self._maybe_screenshot(step_id, observation, force_reason="error")
            failed_event = self.audit.append(
                self.run_id,
                "action_failed",
                {
                    "kind": action.kind.value,
                    "error": result.error,
                    "metadata": result.metadata,
                },
                actor="agent",
                step_id=step_id,
                url=observation.url or None,
                duration_ms=exec_ms,
            )
            self._record_browser_action(
                event=failed_event,
                action=action,
                status="failed",
                observation=observation,
                target_meta=target_meta,
                result_message=result.error,
                page_changed=page_changed,
                duration_ms=exec_ms,
                exec_meta=result.metadata,
            )
            self.audit.append(
                self.run_id,
                "run_failed",
                {"error": result.error or "action_failed", "kind": action.kind.value},
                actor="system",
                step_id=step_id,
            )
            self._flush()
            return LoopOutcome(
                status=RunStatus.FAILED,
                message=result.error or "action_failed",
                last_step_id=step_id,
            )

        if result.done or action.kind == ActionKind.DONE:
            self.audit.append(
                self.run_id,
                "run_succeeded",
                {
                    "message": result.message or action.params.message,
                    "steps": step_number,
                },
                actor="system",
                step_id=step_id,
            )
            self._flush()
            return LoopOutcome(
                status=RunStatus.SUCCEEDED,
                message=result.message or action.params.message or "done",
                last_step_id=step_id,
            )

        return None

    def _fill_type_text_if_needed(
        self,
        action: AgentAction,
        *,
        observation: BrowserObservation,
        step_id: uuid.UUID,
    ) -> None:
        """Invoke the text LLM only for ``TYPE_TEXT`` actions missing text.

        Writes ``model_call`` / ``model_call_failed`` audit events and, when
        configured, normalized ``model_calls`` rows (T017).

        Args:
            action: Decision action (mutated when text is filled).
            observation: Current observation for field context.
            step_id: Step grouping id for audit events.

        Raises:
            TextLLMError: When the gate fails; also audited before raise.
        """
        if action.kind != ActionKind.TYPE_TEXT:
            return
        if action.params.text:
            return
        try:
            result = maybe_fill_type_text(
                action,
                goal=self.goal,
                observation=observation,
                client=self.text_llm,
            )
        except TextLLMError as exc:
            failed_event = self.audit.append(
                self.run_id,
                "model_call_failed",
                {
                    "call_kind": "text_llm",
                    "kind": ActionKind.TYPE_TEXT.value,
                    "target_index": action.target_index,
                    "error": str(exc),
                },
                actor="agent",
                step_id=step_id,
                url=observation.url or None,
            )
            if self.model_calls is not None and _is_agent_event(failed_event):
                self.model_calls.record(
                    event=failed_event,
                    call_kind="text_llm",
                    status="failed",
                    request_meta={
                        "kind": ActionKind.TYPE_TEXT.value,
                        "target_index": action.target_index,
                    },
                    response_meta={"error": str(exc)},
                )
            self._flush()
            raise

        if result is None:
            return

        call_event = self.audit.append(
            self.run_id,
            "model_call",
            {
                "call_kind": "text_llm",
                "provider": result.request_meta.get("provider"),
                "model": result.model,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "latency_ms": result.latency_ms,
                "request_meta": result.request_meta,
                "response_meta": result.response_meta,
                "target_index": action.target_index,
                "chars": len(result.text),
            },
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
            duration_ms=result.latency_ms,
        )
        if self.model_calls is not None and _is_agent_event(call_event):
            provider = result.request_meta.get("provider")
            request_id = result.response_meta.get("request_id") or result.request_meta.get(
                "request_id"
            )
            self.model_calls.record(
                event=call_event,
                call_kind="text_llm",
                provider=str(provider) if provider is not None else None,
                model_name=result.model,
                status="ok",
                request_id=str(request_id) if request_id is not None else None,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                request_meta={
                    **result.request_meta,
                    "target_index": action.target_index,
                    "raw_content": result.raw_content,
                },
                response_meta={
                    **result.response_meta,
                    "parsed_output": {"text": result.text},
                    "raw_output": result.raw_content,
                },
            )
        self._flush()

    def _record_jev_model_call(
        self,
        *,
        event: Any,
        request: JevRequest,
        response: JevResponse,
        action: AgentAction,
        latency_ms: int,
    ) -> None:
        """Write a ``model_calls`` row for one Jev decide round-trip.

        Args:
            event: Source ``decision`` audit event (when DB-backed).
            request: Outbound Jev request.
            response: Inbound Jev response.
            action: Mapped agent action.
            latency_ms: Decide latency.
        """
        if self.model_calls is None or not _is_agent_event(event):
            return
        usage = response.usage
        cost = usage.cost_usd if usage is not None else None
        prompt_tokens = usage.input_tokens if usage is not None else None
        completion_tokens = usage.output_tokens if usage is not None else None
        self.model_calls.record(
            event=event,
            call_kind="jev",
            provider="jev",
            model_name=response.model,
            status="ok",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            request_meta={
                "model": request.model,
                "state": request.state,
                "questions": {
                    name: q.model_dump(mode="json") for name, q in request.questions.items()
                },
            },
            response_meta={
                "raw_output": response.answers,
                "parsed_output": {
                    "kind": action.kind.value,
                    "target_index": action.target_index,
                    "confidence": action.confidence,
                },
                "candidates": _candidate_summaries_from_request(request),
                "probabilities": action.probabilities,
                "target_probabilities": action.target_probabilities,
                "selected_action": action.kind.value,
                "selected_target": action.target_index,
                "alternatives": action.alternatives,
                "usage": usage.model_dump(mode="json") if usage is not None else None,
            },
        )

    def _record_browser_action(
        self,
        *,
        event: Any,
        action: AgentAction,
        status: str,
        observation: BrowserObservation,
        target_meta: dict[str, Any],
        result_message: str | None,
        page_changed: bool | None,
        duration_ms: int | None,
        exec_meta: Mapping[str, Any] | None,
    ) -> None:
        """Write a ``browser_actions`` row when a DB-backed writer is configured.

        Args:
            event: Source audit event.
            action: Executed (or requested) action.
            status: ``requested``, ``completed``, or ``failed``.
            observation: Pre-action observation.
            target_meta: Element forensics from the observation.
            result_message: Outcome or error string.
            page_changed: Whether the page changed after execute.
            duration_ms: Action duration.
            exec_meta: Executor metadata (may include Browser Use ids).
        """
        if self.browser_actions is None or not _is_agent_event(event):
            return
        meta: dict[str, Any] = {
            **target_meta,
            "params": action.params.model_dump(mode="json"),
        }
        if exec_meta:
            meta["executor"] = dict(exec_meta)
            if "browser_use_ids" in exec_meta:
                meta["browser_use_ids"] = exec_meta["browser_use_ids"]
            if "bounds" in exec_meta and "bounds" not in meta:
                meta["bounds"] = exec_meta["bounds"]
        before_id = _optional_uuid((exec_meta or {}).get("before_artifact_id"))
        after_id = _optional_uuid((exec_meta or {}).get("after_artifact_id"))
        self.browser_actions.record(
            event=event,
            action_type=action.kind.value,
            status=status,
            target=target_meta.get("target_label"),
            url=observation.url or None,
            title=observation.title or None,
            element_index=action.target_index,
            page_changed=page_changed,
            result=result_message,
            duration_ms=duration_ms,
            before_artifact_id=before_id,
            after_artifact_id=after_id,
            metadata=meta,
        )

    async def _cancelled(self) -> bool:
        """Return whether cooperative cancellation was requested.

        Returns:
            ``True`` when the cancel check says stop.
        """
        if self.is_cancelled is None:
            return False
        result = self.is_cancelled()
        if isinstance(result, Awaitable):
            return bool(await result)
        return bool(result)

    async def _maybe_checkpoint(
        self,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        *,
        force_reason: str | None = None,
    ) -> None:
        """Invoke the optional checkpoint hook when configured.

        Args:
            step_id: Step grouping id.
            observation: Latest observation.
            force_reason: Optional force reason (``approval``, ``error``).
        """
        if self.checkpoint is None:
            return
        hook = self.checkpoint
        if force_reason is not None:
            try:
                maybe = hook(self.run_id, step_id, observation, force_reason)  # type: ignore[call-arg]
            except TypeError:
                maybe = hook(self.run_id, step_id, observation)
        else:
            maybe = hook(self.run_id, step_id, observation)
        if maybe is not None:
            await maybe

    async def _maybe_screenshot(
        self,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        *,
        force_reason: str | None = None,
    ) -> None:
        """Invoke the optional screenshot hook when configured.

        Args:
            step_id: Step grouping id.
            observation: Latest observation.
            force_reason: Optional force reason (``approval``, ``error``,
                ``destructive``).
        """
        if self.screenshot is None:
            return
        hook = self.screenshot
        if force_reason is not None:
            try:
                maybe = hook(self.run_id, step_id, observation, force_reason)  # type: ignore[call-arg]
            except TypeError:
                maybe = hook(self.run_id, step_id, observation)
        else:
            maybe = hook(self.run_id, step_id, observation)
        if maybe is not None:
            await maybe

    def _flush(self) -> None:
        """Commit the current audit batch when a commit callback is set."""
        if self._commit is not None:
            self._commit()


def _observation_audit_payload(observation: BrowserObservation) -> dict[str, Any]:
    """Build a small, redacted observation summary for ``agent_events``.

    Large DOM dumps belong in T018 checkpoints, not the event row.

    Args:
        observation: Current browser observation.

    Returns:
        Compact metadata dict (already suitable for AuditWriter redaction).
    """
    payload = {
        "url": observation.url,
        "title": observation.title,
        "candidate_count": len(observation.candidates),
        "candidate_indices": [c.index for c in observation.candidates[:64]],
        "pixels_above": observation.pixels_above,
        "pixels_below": observation.pixels_below,
        "page_summary": observation.page_summary,
        "browser_errors": observation.browser_errors[:8],
    }
    redacted = redact_for_audit(payload)
    return redacted if isinstance(redacted, dict) else {"value": redacted}


def _is_agent_event(event: Any) -> bool:
    """Return True when ``event`` looks like a persisted ``AgentEvent``.

    Args:
        event: Object returned from :meth:`AuditAppend.append`.

    Returns:
        Whether the object has ``id``, ``run_id``, and ``seq`` attributes.
    """
    return (
        getattr(event, "id", None) is not None
        and getattr(event, "run_id", None) is not None
        and getattr(event, "seq", None) is not None
    )


def _target_forensics(
    observation: BrowserObservation,
    action: AgentAction,
) -> dict[str, Any]:
    """Build element forensic fields from the observation candidate.

    Args:
        observation: Pre-action observation.
        action: Action that may reference a target index.

    Returns:
        Metadata dict with role, accessible name, attributes, and label.
    """
    meta: dict[str, Any] = {}
    if action.target_index is None:
        return meta
    candidate = observation.candidate_by_index(action.target_index)
    if candidate is None:
        meta["target_label"] = f"element[{action.target_index}]"
        meta["element_index"] = action.target_index
        return meta
    meta.update(_candidate_forensics(candidate))
    return meta


def _candidate_forensics(candidate: CandidateElement) -> dict[str, Any]:
    """Map a candidate element to browser-action forensic fields.

    Args:
        candidate: Interactable element from the observation.

    Returns:
        Dict with ``accessible_name``, ``role``, ``attributes``, ``target_label``.
    """
    attributes: dict[str, Any] = {}
    if candidate.tag:
        attributes["tag"] = candidate.tag
    if candidate.href:
        attributes["href"] = candidate.href
    if candidate.input_type:
        attributes["input_type"] = candidate.input_type
    attributes["is_editable"] = candidate.is_editable
    attributes["is_password_field"] = candidate.is_password_field
    return {
        "element_index": candidate.index,
        "accessible_name": candidate.name,
        "role": candidate.role,
        "attributes": attributes,
        "target_label": candidate.criteria_label(),
    }


def _candidate_summaries_from_request(request: JevRequest) -> list[dict[str, Any]]:
    """Extract compact candidate labels from a Jev request's target criteria.

    Args:
        request: Outbound Jev request.

    Returns:
        List of ``{index, label}`` dicts when criteria look like element indices.
    """
    summaries: list[dict[str, Any]] = []
    for question in request.questions.values():
        criteria = getattr(question, "criteria", None)
        if not isinstance(criteria, Mapping):
            continue
        for key, label in criteria.items():
            try:
                index = int(key)
            except TypeError, ValueError:
                continue
            summaries.append({"index": index, "label": label})
        if summaries:
            break
    return summaries


def _page_changed(
    observation: BrowserObservation,
    exec_meta: Mapping[str, Any] | None,
) -> bool | None:
    """Infer whether the page changed from executor metadata.

    Args:
        observation: Pre-action observation.
        exec_meta: Executor result metadata.

    Returns:
        Explicit flag from metadata, URL comparison when present, else ``None``.
    """
    if not exec_meta:
        return None
    if "page_changed" in exec_meta:
        return bool(exec_meta["page_changed"])
    after_url = exec_meta.get("url")
    if isinstance(after_url, str) and after_url:
        return after_url != observation.url
    return None


def _optional_uuid(value: Any) -> uuid.UUID | None:
    """Parse an optional UUID from executor metadata.

    Args:
        value: Raw value (UUID, str, or other).

    Returns:
        Parsed UUID, or ``None`` when missing/invalid.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except TypeError, ValueError, AttributeError:
        return None
