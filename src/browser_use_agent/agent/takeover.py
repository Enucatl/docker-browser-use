"""Human take-control / release-control mutex for VNC handoff (T023).

State machine relative to T020 / T021
-------------------------------------
* **Take-control is stronger than pause.** A ``running`` or ``paused`` run may
  enter ``awaiting_human``. While in that status the agent loop parks between
  phases (same cooperative wait as pause) and browser execute is refused by the
  mutex wrapper even if a phase is somehow reached.
* **Release** returns the run to ``running`` and wakes the worker. After release
  the loop continues with a **fresh observe** (a mid-step decide that was parked
  at ``before_execute`` is discarded so the human's page changes are seen).
* **Approval waits (T021)** stay on ``awaiting_approval``. Take-control is not
  allowed from that status — approve/reject (or cancel) first. Pause/resume APIs
  likewise reject ``awaiting_human`` (use release-control instead of resume).
* **Cancel** remains allowed from ``awaiting_human`` (cooperative exit).

Audit events: ``takeover_started`` / ``takeover_ended`` with Authelia operator
identity in the redacted payload (``actor`` field). VNC view-only toggling is
out of scope for this module; the mutex is agent-side only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from browser_use_agent.agent.browser_port import ActionExecutionResult, BrowserPort
from browser_use_agent.agent.controls import (
    PAUSE_POLL_SECONDS,
    RunControlSignals,
    StatusCheck,
    wait_while_paused,
)
from browser_use_agent.policy.actions import AgentAction, BrowserObservation
from browser_use_agent.runs.status import RunStatus


class TakeoverActiveError(RuntimeError):
    """Raised when an agent browser action is refused during human takeover."""


def is_takeoverable(status: RunStatus) -> bool:
    """Return whether ``status`` may transition to ``awaiting_human``.

    Args:
        status: Current run status.

    Returns:
        ``True`` for ``running`` or ``paused`` (takeover is stronger than pause).
    """
    return status in {RunStatus.RUNNING, RunStatus.PAUSED}


def is_releasable(status: RunStatus) -> bool:
    """Return whether ``status`` may leave ``awaiting_human`` via release.

    Args:
        status: Current run status.

    Returns:
        ``True`` for ``awaiting_human``.
    """
    return status == RunStatus.AWAITING_HUMAN


def status_is_human_control(status: str | RunStatus) -> bool:
    """Return whether a stored/parsed status means the human owns the browser.

    Args:
        status: Run status string or enum.

    Returns:
        ``True`` when the agent must not drive Chrome.
    """
    value = status.value if isinstance(status, RunStatus) else status
    return value == RunStatus.AWAITING_HUMAN.value


async def wait_while_awaiting_human(
    *,
    is_awaiting_human: StatusCheck,
    is_cancelled: StatusCheck,
    signals: RunControlSignals,
    poll_seconds: float = PAUSE_POLL_SECONDS,
) -> bool:
    """Park until human control ends, or cancel wins.

    Same wake/poll semantics as :func:`wait_while_paused` (T020).

    Args:
        is_awaiting_human: Sync or async callable for ``awaiting_human``.
        is_cancelled: Sync or async callable for cancel.
        signals: In-process wakeups for this run.
        poll_seconds: Fallback DB re-check interval.

    Returns:
        ``True`` when cancel was observed; ``False`` when human control cleared.
    """
    return await wait_while_paused(
        is_paused=is_awaiting_human,
        is_cancelled=is_cancelled,
        signals=signals,
        poll_seconds=poll_seconds,
    )


class TakeoverGuardedBrowserPort:
    """Browser port wrapper that refuses execute while human owns control.

    Observation and screenshots remain allowed so operators can still see
    state; click/type/navigate and other agent actions are blocked.

    Attributes:
        inner: Underlying browser port (real or fake).
        is_held: Callable returning whether takeover is active.
    """

    def __init__(
        self,
        inner: BrowserPort,
        is_held: Callable[[], bool | Awaitable[bool]],
    ) -> None:
        """Wrap ``inner`` with a takeover mutex on execute.

        Args:
            inner: Port to delegate observe / execute / screenshot to.
            is_held: Returns True when the human owns control.
        """
        self.inner = inner
        self.is_held = is_held

    async def _held(self) -> bool:
        """Evaluate the hold check.

        Returns:
            ``True`` when takeover is active.
        """
        result = self.is_held()
        if isinstance(result, Awaitable):
            return bool(await result)
        return bool(result)

    async def observe(self) -> BrowserObservation:
        """Delegate observation to the inner port.

        Returns:
            Current page observation.
        """
        return await self.inner.observe()

    async def screenshot(self) -> bytes:
        """Delegate screenshot capture to the inner port.

        Returns:
            Image bytes from the inner port.
        """
        return await self.inner.screenshot()

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Refuse agent actions while takeover is active; otherwise execute.

        Args:
            action: Action the agent wants to run.

        Returns:
            Failed result when held; otherwise the inner execution result.
        """
        if await self._held():
            return ActionExecutionResult(
                ok=False,
                error="takeover_active: agent actions refused while human controls browser",
                metadata={
                    "kind": action.kind.value,
                    "refused": True,
                    "reason": "awaiting_human",
                },
            )
        return await self.inner.execute(action)


def wrap_browser_for_takeover(
    browser: BrowserPort,
    is_held: Callable[[], bool | Awaitable[bool]],
) -> TakeoverGuardedBrowserPort:
    """Return a mutex-guarded browser port.

    Args:
        browser: Inner port (may already be a fake).
        is_held: Takeover hold check.

    Returns:
        Guarded wrapper around ``browser``.
    """
    return TakeoverGuardedBrowserPort(browser, is_held)
