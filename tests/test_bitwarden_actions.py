"""Tests for the T027 Bitwarden action executor."""

from __future__ import annotations

import asyncio
from typing import Any

from browser_use_agent.agent.browser_port import BrowserUsePort
from browser_use_agent.browser.bitwarden_actions import (
    BitwardenActionExecutor,
    bitwarden_audit_payload,
)
from browser_use_agent.policy.actions import ActionKind, ActionParams, AgentAction


class _Page:
    """Mock active page that records keyboard input."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def press(self, key: str) -> None:
        """Record one key sequence."""
        self.keys.append(key)


class _Session:
    """Mock Browser Use session with a stable target."""

    def __init__(self) -> None:
        self.page = _Page()

    async def get_element_by_index(self, index: int) -> object | None:
        """Return a target for the expected index."""
        return object() if index == 4 else None

    async def get_current_page(self) -> _Page:
        """Return the mock active page."""
        return self.page


class _ActionModel:
    """Small action-model stub for the Browser Use port test."""

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the payload unchanged."""
        return payload


class _Tools:
    """Mock Browser Use registry that successfully focuses a target."""

    class registry:
        """Mock registry namespace."""

        @staticmethod
        def create_action_model() -> type[_ActionModel]:
            """Return the action model stub."""
            return _ActionModel

    async def act(self, action: dict[str, Any], session: _Session) -> Any:
        """Return a successful click result."""
        del action, session
        return type("Result", (), {"error": None})()


def _login_action(**params: Any) -> AgentAction:
    """Build a targeted login action for tests."""
    return AgentAction(
        kind=ActionKind.BITWARDEN_LOGIN,
        target_index=4,
        params=ActionParams(**params),
    )


def test_login_uses_browser_shortcut_without_secret_material() -> None:
    """Login autofill focuses the target and sends only the extension shortcut."""

    async def _run() -> None:
        session = _Session()
        result = await BitwardenActionExecutor(session).execute(
            _login_action(item_name="Example login", item_id="item-123"),
        )

        assert result.ok is True
        assert session.page.keys == ["Control+Shift+L"]
        assert result.metadata["fields_affected"] == ["username", "password"]
        assert "hunter2" not in str(result.metadata)

    asyncio.run(_run())


def test_stale_target_fails_closed() -> None:
    """A changed selector map never receives an autofill shortcut."""

    async def _run() -> None:
        session = _Session()
        action = _login_action()
        action.target_index = 9
        result = await BitwardenActionExecutor(session).execute(action)
        assert result.ok is False
        assert result.metadata["result"] == "stale_target"
        assert session.page.keys == []

    asyncio.run(_run())


def test_identity_and_card_are_explicit_fail_closed_stubs() -> None:
    """Unsupported fill types never try to handle secrets in the controller."""

    async def _run() -> None:
        session = _Session()
        for kind in (ActionKind.BITWARDEN_IDENTITY, ActionKind.BITWARDEN_CARD):
            result = await BitwardenActionExecutor(session).execute(
                AgentAction(kind=kind, target_index=4),
            )
            assert result.ok is False
            assert result.metadata["result"] == "unsupported_action"
        assert session.page.keys == []

    asyncio.run(_run())


def test_audit_payload_redacts_secret_shaped_item_names() -> None:
    """Dedicated fill events preserve field names but redact selector text."""
    action = _login_action(item_name="password=hunter2")
    payload = bitwarden_audit_payload(action, result="requested")
    assert "hunter2" not in str(payload)
    assert payload["fields_affected"] == ["username", "password"]


def test_browser_use_port_dispatches_bitwarden_executor() -> None:
    """The live browser port routes specialized actions to the executor."""

    async def _run() -> None:
        session = _Session()
        result = await BrowserUsePort(session, tools=_Tools()).execute(_login_action())
        assert result.ok is True

    asyncio.run(_run())


def test_browser_use_port_builds_switch_tab_payload() -> None:
    """Popup tab selection uses Browser Use's native switch action."""
    action = AgentAction(
        kind=ActionKind.SWITCH_TAB,
        params=ActionParams(tab_id="abcd"),
    )
    assert BrowserUsePort(_Session())._action_model_payload(action) == {
        "switch": {"tab_id": "abcd"},
    }
