"""Browser observation / action execution ports for the agent loop.

Real Chrome execution goes through Browser Use; unit tests inject
:class:`FakeBrowserPort`. ``TYPE_TEXT`` requires ``params.text`` (filled by
the T016 text LLM gate in the agent loop, or a test stub).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    ScrollDirection,
)


class TypeTextBlockedError(RuntimeError):
    """Raised when ``TYPE_TEXT`` reaches execute without ``params.text``."""


def _scrub_page_value(value: Any, secrets: set[str]) -> Any:
    """Remove captured form values and secret patterns before serialization."""
    from browser_use_agent.security.redaction import REDACTED, is_deny_field, redact_text

    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parts = urlsplit(value)
            query = parse_qsl(parts.query, keep_blank_values=True)
            if parts.username is not None or any(is_deny_field(key) for key, _ in query):
                value = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc.rsplit("@", 1)[-1],
                        parts.path,
                        urlencode(
                            [(key, REDACTED if is_deny_field(key) else item) for key, item in query]
                        ),
                        parts.fragment,
                    )
                )
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _scrub_page_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_page_value(item, secrets) for item in value]
    return value


def _safe_page_html(tree: Any) -> tuple[str, set[str]]:
    """Reuse Browser Use's HTML reader while omitting form control contents."""
    from browser_use.dom.serializer.html_serializer import HTMLSerializer
    from browser_use.dom.views import NodeType

    from browser_use_agent.security.redaction import is_deny_field

    secrets: set[str] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        attrs = node.attributes or {}
        sensitive = str(attrs.get("type") or "").strip().lower() == "password" or any(
            is_deny_field(str(attrs.get(key) or ""))
            or any(
                marker in str(attrs.get(key) or "").lower()
                for marker in ("password", "credential", "token", "secret", "api-key", "api_key")
            )
            for key in ("name", "id", "autocomplete")
        )
        opaque = node.tag_name == "textarea" or (
            "contenteditable" in attrs and attrs["contenteditable"] != "false"
        )
        if sensitive or opaque:
            if attrs.get("value"):
                secrets.add(attrs["value"])
            if opaque:
                text = node.get_all_children_text()
                if text:
                    secrets.add(text)
        secrets.update(value for key, value in attrs.items() if value and is_deny_field(key))
        stack.extend(node.children_and_shadow_roots)
        if node.content_document:
            stack.append(node.content_document)

    class SafeHTMLSerializer(HTMLSerializer):
        """Omit editable values and scrub remaining text and attributes."""

        def serialize(self, node: Any, depth: int = 0) -> str:
            """Serialize ordinary content without form values."""
            if node.tag_name in {"input", "textarea", "select"} or (
                "contenteditable" in node.attributes
                and node.attributes["contenteditable"] != "false"
            ):
                return ""
            if node.node_type == NodeType.TEXT_NODE:
                return self._escape_html(_scrub_page_value(node.node_value, secrets))
            return super().serialize(node, depth)

        def _serialize_attributes(self, attributes: dict[str, str]) -> str:
            """Exclude credential attributes before creating serialized HTML."""
            return super()._serialize_attributes(
                {
                    key: _scrub_page_value(value, secrets)
                    for key, value in attributes.items()
                    if key != "value" and not is_deny_field(key)
                }
            )

    return SafeHTMLSerializer().serialize(tree), secrets


def _page_chunk(observation: BrowserObservation, content: str, offset: int) -> BrowserObservation:
    """Attach a bounded chunk and hash of the complete sanitized page."""
    if offset < 0:
        raise ValueError("Page text offset must be nonnegative")
    from browser_use_agent.security.redaction import redact_text

    content = redact_text(content)
    offset = min(offset, len(content))
    text = content[offset : offset + 10_000]
    safe = _scrub_page_value(observation.model_dump(), set())
    return BrowserObservation.model_validate(
        {
            **safe,
            "page_summary": text,
            "page_text": text,
            "page_text_offset": offset,
            "page_text_remaining": max(0, len(content) - offset - len(text)),
            "page_text_hash": sha256(content.encode()).hexdigest(),
        }
    )


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

    async def read_page(self, offset: int = 0) -> BrowserObservation:
        """Read a bounded chunk of sanitized current page text for research."""

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Execute one mapped agent action.

        Args:
            action: Action produced from a Jev decision.

        Returns:
            Execution result (success, done, or error).
        """

    async def screenshot(self) -> bytes:
        """Capture a viewport screenshot as raw image bytes.

        Returns:
            PNG (or other) image bytes for T019 encoding / artifact storage.
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
        screenshot_bytes: Fake image bytes returned by :meth:`screenshot`.
    """

    def __init__(
        self,
        observations: list[BrowserObservation] | None = None,
        *,
        type_text_stub: str | None = None,
        fail_kinds: set[ActionKind] | None = None,
        screenshot_bytes: bytes | None = None,
    ) -> None:
        """Create a fake browser port.

        Args:
            observations: Ordered observations; defaults to a blank page.
            type_text_stub: Optional stub text for ``TYPE_TEXT``.
            fail_kinds: Kinds that return a failed execution result.
            screenshot_bytes: Optional fake screenshot payload (T019 tests).
        """
        if observations:
            self.observations = list(observations)
        else:
            self.observations = [
                BrowserObservation(url="about:blank", title="Blank"),
            ]
        self._cursor = 0
        self._current = self.observations[0]
        self.executed: list[AgentAction] = []
        self.type_text_stub = type_text_stub
        self.fail_kinds = set(fail_kinds or ())
        self.screenshot_bytes = screenshot_bytes if screenshot_bytes is not None else b""

    async def observe(self) -> BrowserObservation:
        """Return the next scripted observation (or the last one).

        Returns:
            Current observation snapshot.
        """
        if self._cursor < len(self.observations):
            obs = self.observations[self._cursor]
            self._cursor += 1
            self._current = obs
            return obs
        return self.observations[-1]

    async def read_page(self, offset: int = 0) -> BrowserObservation:
        """Read the latest observed fake page without advancing navigation."""
        from browser_use_agent.security.redaction import redact_text

        content = redact_text(self._current.page_text or self._current.page_summary or "")
        return _page_chunk(self._current, content, offset)

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

    async def screenshot(self) -> bytes:
        """Return configured fake screenshot bytes (no Chrome).

        Returns:
            Bytes from ``screenshot_bytes`` (may be empty when unset).
        """
        return self.screenshot_bytes


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
        return await self.read_page()

    async def read_page(self, offset: int = 0) -> BrowserObservation:
        """Read sanitized Browser Use markdown in 10,000-character chunks."""
        from browser_use.dom.markdown_extractor import (
            _get_enhanced_dom_tree_from_browser_session,
            convert_html_to_markdown,
        )

        from browser_use_agent.policy.jev_adapter import observation_from_browser_state

        summary = await self.session.get_browser_state_summary(
            include_screenshot=False,
            cached=False,
        )
        observation = observation_from_browser_state(summary)
        try:
            source_url = await self.session.get_current_page_url()
            if source_url != observation.url:
                raise ValueError("Page changed before reading")
            tree = await _get_enhanced_dom_tree_from_browser_session(self.session)
            html, secrets = _safe_page_html(tree)
            observation = BrowserObservation.model_validate(
                _scrub_page_value(observation.model_dump(), secrets)
            )
            content, _, _ = convert_html_to_markdown(html)
            if await self.session.get_current_page_url() != source_url:
                raise ValueError("Page changed while reading")
        except Exception as exc:
            # Ordinary runs still observe elements when the reader is unavailable;
            # empty text fails closed when the research loop tries to extract.
            observation.browser_errors.append(f"Page reader unavailable: {type(exc).__name__}")
            content = ""
        return _page_chunk(observation, content, offset)

    async def screenshot(self) -> bytes:
        """Capture a PNG viewport screenshot via Browser Use / CDP.

        Encoding to WebP/JPEG is handled by the T019 screenshot writer so unit
        tests can inject fake bytes without Chrome.

        Returns:
            Raw PNG image bytes from ``BrowserSession.take_screenshot``.
        """
        data = await self.session.take_screenshot(format="png")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"take_screenshot returned {type(data)!r}, expected bytes")
        return bytes(data)

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
            from browser_use_agent.browser.bitwarden_actions import BitwardenActionExecutor

            return await BitwardenActionExecutor(self.session, tools=self._get_tools()).execute(
                action
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

        if action.kind == ActionKind.SWITCH_TAB:
            if not action.params.tab_id:
                raise ValueError("SWITCH_TAB requires params.tab_id")
            return {"switch": {"tab_id": action.params.tab_id}}

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
