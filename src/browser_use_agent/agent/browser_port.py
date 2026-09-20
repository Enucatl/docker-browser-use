"""Browser observation / action execution ports for the agent loop.

Real Chrome execution goes through Browser Use; unit tests inject
:class:`FakeBrowserPort`. ``TYPE_TEXT`` requires ``params.text`` (filled by
the T016 text LLM gate in the agent loop, or a test stub).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    ScrollDirection,
)


class TypeTextBlockedError(RuntimeError):
    """Raised when ``TYPE_TEXT`` reaches execute without ``params.text``."""


@dataclass(slots=True)
class ActionExecutionResult:
    """Outcome of executing one :class:`AgentAction`.

    Attributes:
        ok: Whether the browser action completed without error.
        done: True when the action was ``DONE`` (run should stop successfully).
        message: Optional human-readable summary.
        error: Error string when ``ok`` is False.
        metadata: Extra non-secret details for audit payloads.
    """

    ok: bool = True
    done: bool = False
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BrowserPort(Protocol):
    """Observe and execute against a browser (or a fake)."""

    async def observe(self) -> BrowserObservation:
        """Return the current page observation for Jev.

        Returns:
            Adapter-ready observation (candidates + page meta).
        """

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Execute one mapped agent action.

        Args:
            action: Action produced from a Jev decision.

        Returns:
            Execution result (success, done, or error).
        """


class FakeBrowserPort:
    """Scripted browser for unit tests (no Chrome).

    Observations are consumed in order; when exhausted, the last observation
    is reused. Executed actions are recorded on ``executed``.

    Attributes:
        observations: Scripted observation sequence.
        executed: Actions passed to :meth:`execute` in order.
        fail_kinds: Action kinds that should fail closed when executed.
        type_text_stub: Optional text returned for ``TYPE_TEXT`` when params
            lack text (tests only; production defaults to fail-closed).
    """

    def __init__(
        self,
        observations: list[BrowserObservation] | None = None,
        *,
        type_text_stub: str | None = None,
        fail_kinds: set[ActionKind] | None = None,
    ) -> None:
        """Create a fake browser port.

        Args:
            observations: Ordered observations; defaults to a blank page.
            type_text_stub: Optional stub text for ``TYPE_TEXT``.
            fail_kinds: Kinds that return a failed execution result.
        """
        if observations:
            self.observations = list(observations)
        else:
            self.observations = [
                BrowserObservation(url="about:blank", title="Blank"),
            ]
        self._cursor = 0
        self.executed: list[AgentAction] = []
        self.type_text_stub = type_text_stub
        self.fail_kinds = set(fail_kinds or ())

    async def observe(self) -> BrowserObservation:
        """Return the next scripted observation (or the last one).

        Returns:
            Current observation snapshot.
        """
        if self._cursor < len(self.observations):
            obs = self.observations[self._cursor]
            self._cursor += 1
            return obs
        return self.observations[-1]

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Record and optionally fail or complete an action.

        Args:
            action: Action to simulate.

        Returns:
            Synthetic execution result.

        Raises:
            TypeTextBlockedError: When ``TYPE_TEXT`` has no text and no stub.
        """
        self.executed.append(action)

        if action.kind in self.fail_kinds:
            return ActionExecutionResult(
                ok=False,
                error=f"fake failure for {action.kind.value}",
                metadata={"kind": action.kind.value},
            )

        if action.kind == ActionKind.DONE:
            return ActionExecutionResult(
                ok=True,
                done=True,
                message=action.params.message or "done",
            )

        if action.kind == ActionKind.TYPE_TEXT:
            text = action.params.text or self.type_text_stub
            if not text:
                raise TypeTextBlockedError(
                    "TYPE_TEXT requires text from the text LLM gate (T016)",
                )
            return ActionExecutionResult(
                ok=True,
                message="typed (fake)",
                metadata={"target_index": action.target_index, "chars": len(text)},
            )

        if action.kind in {
            ActionKind.BITWARDEN_LOGIN,
            ActionKind.BITWARDEN_IDENTITY,
            ActionKind.BITWARDEN_CARD,
        }:
            return ActionExecutionResult(
                ok=False,
                error=f"{action.kind.value} is not implemented until T027",
                metadata={"kind": action.kind.value},
            )

        return ActionExecutionResult(
            ok=True,
            message=f"executed {action.kind.value}",
            metadata={
                "kind": action.kind.value,
                "target_index": action.target_index,
                "url": action.params.url,
                "direction": (action.params.direction.value if action.params.direction else None),
            },
        )


class BrowserUsePort:
    """Observe / execute via a live :class:`~browser_use.BrowserSession`.

    Uses Browser Use ``get_browser_state_summary`` for observation and the
    Tools registry for click / navigate / scroll / go_back. ``TYPE_TEXT``
    requires ``params.text`` (the agent loop fills it via the T016 gate).

    Attributes:
        session: Connected Browser Use session.
    """

    def __init__(self, session: Any, *, tools: Any | None = None) -> None:
        """Bind to an attached Browser Use session.

        Args:
            session: Live ``BrowserSession`` instance.
            tools: Optional prebuilt ``Tools`` instance; created lazily.
        """
        self.session = session
        self._tools = tools

    def _get_tools(self) -> Any:
        """Lazily construct Browser Use Tools.

        Returns:
            Tools registry used for ``act``.
        """
        if self._tools is None:
            from browser_use.tools.service import Tools

            self._tools = Tools()
        return self._tools

    async def observe(self) -> BrowserObservation:
        """Capture Browser Use state and map it to an observation.

        Returns:
            Redaction-safe observation for the Jev adapter.
        """
        from browser_use_agent.policy.jev_adapter import observation_from_browser_state

        summary = await self.session.get_browser_state_summary(
            include_screenshot=False,
            cached=False,
        )
        return observation_from_browser_state(summary)

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Dispatch the action through Browser Use Tools.

        Args:
            action: Mapped agent action.

        Returns:
            Normalized execution result.

        Raises:
            TypeTextBlockedError: When ``TYPE_TEXT`` has no text.
        """
        if action.kind == ActionKind.DONE:
            return ActionExecutionResult(
                ok=True,
                done=True,
                message=action.params.message or "done",
            )

        if action.kind == ActionKind.TYPE_TEXT:
            if not action.params.text:
                raise TypeTextBlockedError(
                    "TYPE_TEXT requires text from the text LLM gate (T016)",
                )

        if action.kind in {
            ActionKind.BITWARDEN_LOGIN,
            ActionKind.BITWARDEN_IDENTITY,
            ActionKind.BITWARDEN_CARD,
        }:
            return ActionExecutionResult(
                ok=False,
                error=f"{action.kind.value} is not implemented until T027",
            )

        # Re-resolve targeted indices against the current selector map.
        if action.requires_target():
            if action.target_index is None:
                return ActionExecutionResult(
                    ok=False,
                    error=f"{action.kind.value} missing target_index",
                )
            node = await self.session.get_element_by_index(action.target_index)
            if node is None:
                return ActionExecutionResult(
                    ok=False,
                    error=(
                        f"target index {action.target_index} missing from "
                        "current selector map; aborting execute"
                    ),
                    metadata={"target_index": action.target_index},
                )

        try:
            payload = self._action_model_payload(action)
        except ValueError as exc:
            return ActionExecutionResult(ok=False, error=str(exc))

        tools = self._get_tools()
        action_model = tools.registry.create_action_model()
        model = action_model.model_validate(payload)
        result = await tools.act(model, self.session)
        error = getattr(result, "error", None)
        if error:
            return ActionExecutionResult(
                ok=False,
                error=str(error),
                metadata={"kind": action.kind.value},
            )
        extracted = getattr(result, "extracted_content", None)
        return ActionExecutionResult(
            ok=True,
            message=str(extracted) if extracted else f"executed {action.kind.value}",
            metadata={"kind": action.kind.value},
        )

    def _action_model_payload(self, action: AgentAction) -> dict[str, Any]:
        """Build a Browser Use ActionModel dump for ``Tools.act``.

        Args:
            action: Agent action to translate.

        Returns:
            Single-key action payload (e.g. ``{\"click\": {...}}``).

        Raises:
            ValueError: When the kind cannot be mapped.
        """
        if action.kind == ActionKind.CLICK:
            if action.target_index is None:
                raise ValueError("CLICK requires target_index")
            return {"click": {"index": action.target_index}}

        if action.kind == ActionKind.TYPE_TEXT:
            if action.target_index is None or not action.params.text:
                raise ValueError("TYPE_TEXT requires target_index and text")
            return {
                "input": {
                    "index": action.target_index,
                    "text": action.params.text,
                    "clear": True,
                },
            }

        if action.kind == ActionKind.NAVIGATE:
            if not action.params.url:
                raise ValueError("NAVIGATE requires params.url")
            return {"navigate": {"url": action.params.url, "new_tab": False}}

        if action.kind == ActionKind.GO_BACK:
            return {"go_back": {}}

        if action.kind == ActionKind.SCROLL:
            direction = action.params.direction or ScrollDirection.DOWN
            down = direction in {ScrollDirection.DOWN, ScrollDirection.RIGHT}
            pages = 1.0
            if action.params.amount is not None and action.params.amount > 0:
                pages = float(action.params.amount)
            return {"scroll": {"down": down, "pages": pages}}

        raise ValueError(f"Unsupported action kind for Browser Use: {action.kind.value}")
