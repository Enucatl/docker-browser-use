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
from browser_use_agent.policy.actions import ActionKind, AgentAction, BrowserObservation
from browser_use_agent.policy.jev_adapter import JevAdapter, JevAdapterError
from browser_use_agent.policy.jev_client import JevClient, JevClientError
from browser_use_agent.policy.text_llm import TextLLMClient, TextLLMError, maybe_fill_type_text
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import redact_for_audit

logger = logging.getLogger(__name__)

NeedsApprovalHook = Callable[[AgentAction], bool]
CheckpointHook = Callable[
    [uuid.UUID, uuid.UUID, BrowserObservation],
    Awaitable[None] | None,
]
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
        adapter: Observation ↔ Jev mapper.
        max_steps: Hard cap to avoid infinite loops.
        needs_approval: Hook for T021; defaults to always False.
        checkpoint: Optional T018 hook; skipped when ``None``.
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
        max_steps: int = 50,
        needs_approval: NeedsApprovalHook | None = None,
        checkpoint: CheckpointHook | None = None,
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
            max_steps: Maximum observe/decide/execute iterations.
            needs_approval: Approval gate hook (default always False).
            checkpoint: Optional checkpoint writer (T018).
            is_cancelled: Returns True when the run should stop cooperatively.
            commit: Optional callback after each audit batch (e.g. session.commit).
        """
        self.run_id = run_id
        self.goal = goal
        self.browser = browser
        self.jev = jev
        self.text_llm = text_llm
        self.audit = audit
        self.adapter = adapter if adapter is not None else JevAdapter()
        self.max_steps = max(1, max_steps)
        self.needs_approval = needs_approval or default_needs_approval
        self.checkpoint = checkpoint
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

        if self.checkpoint is not None:
            maybe = self.checkpoint(self.run_id, step_id, observation)
            if maybe is not None:
                await maybe

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
        self.audit.append(
            self.run_id,
            "decision",
            decision_payload,
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
            duration_ms=decide_ms,
        )
        self._flush()

        if self.needs_approval(action):
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
        self.audit.append(
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
        self._flush()

        t2 = time.perf_counter()
        result = await self.browser.execute(action)
        exec_ms = int((time.perf_counter() - t2) * 1000)

        if result.ok:
            self.audit.append(
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
            self._flush()
            self._history.append(f"{action.kind.value}:{result.message or 'ok'}")
        else:
            self.audit.append(
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

        Writes ``model_call`` / ``model_call_failed`` audit events (T017 will
        promote these into dedicated ``model_calls`` rows).

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
            self.audit.append(
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
            self._flush()
            raise

        if result is None:
            return

        self.audit.append(
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
        self._flush()

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
