"""Fail-closed Bitwarden autofill actions.

The login MVP uses Bitwarden's documented browser shortcut on the active page.
It keeps vault values inside the extension and relies on the current site's URI
matching. Identity/card selection and extension-popup item selection remain
explicitly unsupported until a stable extension automation API is available.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from browser_use_agent.policy.actions import ActionKind, AgentAction
from browser_use_agent.security.redaction import redact_for_audit

if TYPE_CHECKING:
    from browser_use_agent.agent.browser_port import ActionExecutionResult


BITWARDEN_FIELDS: Final[Mapping[ActionKind, tuple[str, ...]]] = {
    ActionKind.BITWARDEN_LOGIN: ("username", "password"),
    ActionKind.BITWARDEN_IDENTITY: (
        "title",
        "first_name",
        "last_name",
        "email",
        "phone",
        "address",
    ),
    ActionKind.BITWARDEN_CARD: (
        "cardholder_name",
        "card_number",
        "expiry",
        "security_code",
    ),
}
BITWARDEN_KINDS: Final[frozenset[ActionKind]] = frozenset(BITWARDEN_FIELDS)


def is_bitwarden_action(action: AgentAction) -> bool:
    """Return whether ``action`` is one of the specialized vault actions."""
    return action.kind in BITWARDEN_KINDS


def bitwarden_audit_payload(action: AgentAction, **extra: Any) -> dict[str, Any]:
    """Build a redacted audit payload containing names, never field values."""
    raw = {
        "kind": action.kind.value,
        "target_index": action.target_index,
        "item_name": action.params.item_name,
        "item_id": action.params.item_id,
        "fields_affected": list(BITWARDEN_FIELDS.get(action.kind, ())),
        **extra,
    }
    safe = redact_for_audit(raw)
    return safe if isinstance(safe, dict) else {"value": safe}


class BitwardenActionExecutor:
    """Execute the supported Bitwarden login shortcut against a live page.

    Attributes:
        session: Browser Use session whose active page receives the shortcut.

    The shortcut selects the current site's matching/last-used login. Chrome
    extension popup automation is intentionally not guessed here: popup DOM,
    focus, and item selection are version-sensitive and can fill the wrong tab.
    """

    def __init__(self, session: Any, *, tools: Any | None = None) -> None:
        """Bind the executor to a Browser Use session.

        Args:
            session: Browser Use session with the target page.
            tools: Optional Browser Use tools registry used to focus the target.
        """
        self.session = session
        self.tools = tools

    async def execute(self, action: AgentAction) -> ActionExecutionResult:
        """Execute one Bitwarden action without handling vault data."""
        from browser_use_agent.agent.browser_port import ActionExecutionResult

        if not is_bitwarden_action(action):
            return ActionExecutionResult(ok=False, error="not a Bitwarden action")
        if action.target_index is None:
            return ActionExecutionResult(
                ok=False,
                error=f"{action.kind.value} requires target_index",
                metadata=bitwarden_audit_payload(action, result="missing_target"),
            )

        if await self.session.get_element_by_index(action.target_index) is None:
            return ActionExecutionResult(
                ok=False,
                error="Bitwarden target is no longer present; refusing to fill",
                metadata=bitwarden_audit_payload(action, result="stale_target"),
            )

        if action.kind != ActionKind.BITWARDEN_LOGIN:
            # TODO(T027 follow-up): automate card/identity extension selection
            # after a stable, testable Bitwarden popup API is available.
            return ActionExecutionResult(
                ok=False,
                error=f"{action.kind.value} extension automation is not implemented",
                metadata=bitwarden_audit_payload(action, result="unsupported_action"),
            )

        page = await self.session.get_current_page()
        if page is None:
            return ActionExecutionResult(
                ok=False,
                error="Bitwarden login autofill requires an active page",
                metadata=bitwarden_audit_payload(action, result="missing_page"),
            )

        try:
            if self.tools is not None:
                action_model = self.tools.registry.create_action_model()
                click_result = await self.tools.act(
                    action_model.model_validate({"click": {"index": action.target_index}}),
                    self.session,
                )
                if getattr(click_result, "error", None):
                    raise RuntimeError("target focus failed")
            await page.press("Control+Shift+L")
        except Exception:
            return ActionExecutionResult(
                ok=False,
                error="Bitwarden login autofill shortcut failed",
                metadata=bitwarden_audit_payload(action, result="shortcut_failed"),
            )

        return ActionExecutionResult(
            ok=True,
            message="Bitwarden login autofill requested",
            metadata=bitwarden_audit_payload(
                action,
                result="shortcut_sent",
                method="keyboard_shortcut",
                selection="current_site_last_used_login",
            ),
        )
