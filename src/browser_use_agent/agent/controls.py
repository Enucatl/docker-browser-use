"""Run pause / resume / cancel / retry synchronization.

Synchronization model
---------------------
* **Postgres ``runs.status`` is authoritative.** API mutations update the row
  first; workers re-read it between loop phases. This keeps controls correct
  across request handlers and the asyncio worker task.
* **In-process :class:`RunControlHub` events** wake a parked worker promptly
  when resume or cancel is requested, avoiding a long poll sleep. Events are a
  performance hint only — a missed wake still recovers on the next DB poll.
* Pause is **cooperative**: an in-flight browser action may finish; the loop
  must not start another observe/decide/execute cycle until status leaves
  ``paused``. Cancel is likewise cooperative between phases.
* Retry: ``request_step_retry`` arms a one-shot flag consumed when a step
  action fails (re-observe + decide). ``retry_run`` on a terminal ``failed``
  run clears ``finished_at``, sets ``queued``, and lets the API re-start the
  worker.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from browser_use_agent.runs.status import TERMINAL_STATUSES, RunStatus

# How long a paused worker sleeps between DB re-checks when no wake arrives.
PAUSE_POLL_SECONDS = 0.05

StatusCheck = Callable[[], bool | Awaitable[bool]]


@dataclass
class RunControlSignals:
    """Per-run in-process wakeups and one-shot retry arming.

    Attributes:
        wake: Set when the run leaves a parked wait (resume, cancel, retry, or
            approval decision).
        step_retry_armed: When True, the next failed execute re-observes/decides
            instead of failing the run; cleared when consumed.
        approval_decision: Pending grant/deny from the approvals API (T021);
            consumed by the parked approval waiter.
        pending_approval_id: Optional ``human_approvals.id`` for the open gate.
    """

    wake: asyncio.Event = field(default_factory=asyncio.Event)
    step_retry_armed: bool = False
    approval_decision: str | None = None
    pending_approval_id: uuid.UUID | None = None
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the worker event loop for thread-safe wakes from FastAPI.

        Args:
            loop: Loop running the agent worker task.
        """
        self._loop = loop

    def notify(self) -> None:
        """Wake any parked waiters (thread-safe when called from another thread)."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.wake.set)
        else:
            try:
                self.wake.set()
            except RuntimeError:
                pass

    def arm_step_retry(self) -> None:
        """Arm one cooperative step retry after the next failed execute."""
        self.step_retry_armed = True
        self.notify()

    def consume_step_retry(self) -> bool:
        """Return and clear the step-retry arm flag.

        Returns:
            ``True`` when a retry was armed.
        """
        armed = self.step_retry_armed
        self.step_retry_armed = False
        return armed

    def set_approval_decision(self, decision: str) -> None:
        """Record an approve/reject decision and wake the approval waiter.

        Args:
            decision: ``granted`` or ``denied``.
        """
        self.approval_decision = decision
        self.notify()

    def consume_approval_decision(self) -> str | None:
        """Return and clear a pending approval decision.

        Returns:
            ``granted`` / ``denied``, or ``None`` when none is pending.
        """
        decision = self.approval_decision
        self.approval_decision = None
        return decision


class RunControlHub:
    """Process-local registry of :class:`RunControlSignals` keyed by run id.

    Attributes:
        None publicly mutable; use :meth:`signals_for` / :meth:`discard`.
    """

    def __init__(self) -> None:
        """Create an empty hub."""
        self._lock = threading.Lock()
        self._signals: dict[uuid.UUID, RunControlSignals] = {}

    def signals_for(self, run_id: uuid.UUID) -> RunControlSignals:
        """Return (creating if needed) the signals object for ``run_id``.

        Args:
            run_id: Run primary key.

        Returns:
            Shared :class:`RunControlSignals` for that run.
        """
        with self._lock:
            existing = self._signals.get(run_id)
            if existing is not None:
                return existing
            created = RunControlSignals()
            # Start set so a non-paused run does not block on first wait.
            created.wake.set()
            self._signals[run_id] = created
            return created

    def discard(self, run_id: uuid.UUID) -> None:
        """Drop signals for a finished run.

        Args:
            run_id: Run to forget.
        """
        with self._lock:
            self._signals.pop(run_id, None)

    def notify(self, run_id: uuid.UUID) -> None:
        """Wake waiters for ``run_id`` when signals already exist.

        Args:
            run_id: Run that changed control state.
        """
        with self._lock:
            signals = self._signals.get(run_id)
        if signals is not None:
            signals.notify()

    def arm_step_retry(self, run_id: uuid.UUID) -> None:
        """Arm step retry and wake the worker for ``run_id``.

        Args:
            run_id: Run to arm.
        """
        self.signals_for(run_id).arm_step_retry()

    def set_approval_decision(self, run_id: uuid.UUID, decision: str) -> None:
        """Record approve/reject for ``run_id`` and wake the waiter.

        Args:
            run_id: Run awaiting approval.
            decision: ``granted`` or ``denied``.
        """
        self.signals_for(run_id).set_approval_decision(decision)


_hub = RunControlHub()


def get_control_hub() -> RunControlHub:
    """Return the process-wide run control hub.

    Returns:
        Shared :class:`RunControlHub`.
    """
    return _hub


def reset_control_hub_for_tests() -> RunControlHub:
    """Replace the process hub (tests only).

    Returns:
        The new empty hub.
    """
    global _hub
    _hub = RunControlHub()
    return _hub


def is_pausable(status: RunStatus) -> bool:
    """Return whether ``status`` may transition to ``paused``.

    Args:
        status: Current run status.

    Returns:
        ``True`` for ``running`` only (approval waits are T021).
    """
    return status == RunStatus.RUNNING


def is_resumable(status: RunStatus) -> bool:
    """Return whether ``status`` may transition to ``running`` via resume.

    Args:
        status: Current run status.

    Returns:
        ``True`` for ``paused``.
    """
    return status == RunStatus.PAUSED


def is_cancellable(status: RunStatus) -> bool:
    """Return whether cancel is a meaningful transition.

    Args:
        status: Current run status.

    Returns:
        ``True`` when the run is not already terminal.
    """
    return status not in TERMINAL_STATUSES


def is_retryable_failed(status: RunStatus) -> bool:
    """Return whether a terminal failed run may be re-queued.

    Args:
        status: Current run status.

    Returns:
        ``True`` for ``failed``.
    """
    return status == RunStatus.FAILED


async def wait_while_paused(
    *,
    is_paused: StatusCheck,
    is_cancelled: StatusCheck,
    signals: RunControlSignals,
    poll_seconds: float = PAUSE_POLL_SECONDS,
) -> bool:
    """Park until the run is no longer paused, or cancel wins.

    Clears and waits on ``signals.wake`` between DB polls so resume/cancel
    from the API unblocks promptly.

    Args:
        is_paused: Sync or async callable returning whether status is paused.
        is_cancelled: Sync or async callable returning cancel request.
        signals: In-process wakeups for this run.
        poll_seconds: Fallback DB re-check interval.

    Returns:
        ``True`` when cancel was observed; ``False`` when pause cleared.
    """

    async def _call(fn: StatusCheck) -> bool:
        result = fn()
        if isinstance(result, Awaitable):
            return bool(await result)
        return bool(result)

    while True:
        if await _call(is_cancelled):
            return True
        if not await _call(is_paused):
            return False
        signals.wake.clear()
        # Re-check after clear to avoid a lost wake between poll and wait.
        if await _call(is_cancelled):
            return True
        if not await _call(is_paused):
            return False
        try:
            await asyncio.wait_for(signals.wake.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
