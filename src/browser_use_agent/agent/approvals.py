"""Human approval gates for high-impact browser actions (T021).

Conservative Phase-1 policy: Bitwarden fills always require approval; clicks and
navigations that look like purchases, payments, account destruction, or other
irreversible submissions require approval when label/URL heuristics match.
Ordinary navigation, scrolling, typing, and benign clicks do not.

Reject and timeout both **fail the run** (fail closed). Replanning after reject
is deferred to T028. Never auto-approve in production defaults.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from browser_use_agent.agent.controls import RunControlSignals, StatusCheck
from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)

# Default wall-clock wait for a human decision (fail closed on expiry).
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0

ApprovalDecision = Literal["granted", "denied", "timeout", "cancelled"]

# Kinds that always need a human before execute (never auto-approve).
ALWAYS_APPROVE_KINDS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.BITWARDEN_LOGIN,
        ActionKind.BITWARDEN_IDENTITY,
        ActionKind.BITWARDEN_CARD,
    }
)

# Substrings matched case-insensitively against click labels / hrefs.
HIGH_IMPACT_CLICK_KEYWORDS: frozenset[str] = frozenset(
    {
        "buy",
        "purchase",
        "checkout",
        "pay now",
        "pay with",
        "place order",
        "submit order",
        "confirm payment",
        "transfer",
        "wire",
        "send money",
        "delete account",
        "close account",
        "remove account",
        "unsubscribe",
        "cancel subscription",
        "destroy",
        "permanently delete",
        "confirm delete",
        "send message",
        "send email",
        "post comment",
        "publish",
        "authorize",
        "approve payment",
    }
)

# Path/host fragments that make NAVIGATE high-impact.
HIGH_IMPACT_URL_FRAGMENTS: frozenset[str] = frozenset(
    {
        "checkout",
        "payment",
        "billing",
        "cart/checkout",
        "pay/",
        "/pay?",
        "unsubscribe",
        "delete-account",
        "close-account",
        "transfer",
        "wire-transfer",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    """Optional surroundings for the approval heuristic.

    Attributes:
        observation: Current browser observation (for target labels).
        goal: Operator goal text when known.
        url: Page URL override when observation is absent.
    """

    observation: BrowserObservation | None = None
    goal: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Pending human gate before an action may execute.

    Attributes:
        reason: Operator-visible explanation for the UI.
        action_kind: Action that is blocked.
        target_index: Snapshot-local element index when targeted.
        policy_id: Stable id of the heuristic that matched.
        metadata: Extra fields persisted on ``human_approvals.metadata``.
    """

    reason: str
    action_kind: ActionKind
    target_index: int | None = None
    policy_id: str = "default_v1"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalSettings:
    """Runtime knobs for approval waits.

    Attributes:
        timeout_seconds: Fail-closed wait bound (never auto-approve).
    """

    timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS


def load_approval_settings() -> ApprovalSettings:
    """Load approval settings from the environment.

    ``APPROVAL_TIMEOUT_SECONDS`` caps how long a run parks in
    ``awaiting_approval`` before failing closed. Must be positive.

    Returns:
        Immutable settings snapshot.
    """
    raw = os.environ.get("APPROVAL_TIMEOUT_SECONDS")
    timeout = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    if raw is not None and raw.strip():
        try:
            timeout = float(raw.strip())
        except ValueError:
            timeout = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    if timeout <= 0:
        timeout = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    return ApprovalSettings(timeout_seconds=timeout)


def _normalize_text(value: str | None) -> str:
    """Lowercase and collapse whitespace for keyword matching.

    Args:
        value: Raw label or URL fragment.

    Returns:
        Normalized string (may be empty).
    """
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _click_haystack(candidate: CandidateElement | None, action: AgentAction) -> str:
    """Build a searchable string from the click target and action rationale.

    Args:
        candidate: Matched observation candidate, if any.
        action: Candidate action.

    Returns:
        Lowercased haystack for keyword search.
    """
    parts: list[str] = []
    if candidate is not None:
        parts.extend(
            [
                candidate.name or "",
                candidate.role or "",
                candidate.href or "",
                candidate.tag or "",
            ]
        )
    if action.rationale:
        parts.append(action.rationale)
    return _normalize_text(" ".join(parts))


def _url_looks_high_impact(url: str | None) -> bool:
    """Return whether a navigate URL matches payment/destructive fragments.

    Args:
        url: Absolute or relative URL.

    Returns:
        ``True`` when a known high-impact fragment appears.
    """
    if not url:
        return False
    lowered = url.strip().lower()
    parsed = urlparse(lowered)
    haystack = f"{parsed.netloc}{parsed.path}?{parsed.query}"
    return any(
        fragment in haystack or fragment in lowered for fragment in HIGH_IMPACT_URL_FRAGMENTS
    )


def _click_looks_high_impact(haystack: str) -> str | None:
    """Return the matched keyword when a click label looks high-impact.

    Args:
        haystack: Normalized label/href text.

    Returns:
        Matched keyword, or ``None``.
    """
    if not haystack:
        return None
    for keyword in HIGH_IMPACT_CLICK_KEYWORDS:
        if keyword in haystack:
            return keyword
    return None


def needs_approval(
    action: AgentAction,
    context: ApprovalContext | None = None,
) -> ApprovalRequest | None:
    """Decide whether ``action`` must wait for a human approve/reject.

    Conservative default: Bitwarden always; click/navigate only when heuristics
    match. Never returns an auto-approve signal — absence of a request means
    the action may proceed.

    Args:
        action: Proposed agent action.
        context: Optional observation / goal for richer heuristics.

    Returns:
        :class:`ApprovalRequest` when blocked, otherwise ``None``.
    """
    ctx = context or ApprovalContext()

    if action.kind in ALWAYS_APPROVE_KINDS:
        return ApprovalRequest(
            reason=f"{action.kind.value} requires human approval before execution.",
            action_kind=action.kind,
            target_index=action.target_index,
            policy_id="always_bitwarden",
            metadata={"kind": action.kind.value},
        )

    if action.kind == ActionKind.NAVIGATE:
        url = action.params.url or ctx.url
        if ctx.observation is not None and not url:
            url = ctx.observation.url or None
        if _url_looks_high_impact(url):
            return ApprovalRequest(
                reason=f"Navigation to high-impact URL requires approval: {url}",
                action_kind=action.kind,
                target_index=None,
                policy_id="navigate_url_heuristic",
                metadata={"url": url},
            )
        return None

    if action.kind == ActionKind.CLICK:
        candidate: CandidateElement | None = None
        if ctx.observation is not None and action.target_index is not None:
            candidate = ctx.observation.candidate_by_index(action.target_index)
        haystack = _click_haystack(candidate, action)
        matched = _click_looks_high_impact(haystack)
        if matched is not None:
            label = (candidate.name if candidate is not None else None) or haystack[:80]
            return ApprovalRequest(
                reason=f"Click looks high-impact ({matched!r}): {label}",
                action_kind=action.kind,
                target_index=action.target_index,
                policy_id="click_keyword_heuristic",
                metadata={"matched_keyword": matched, "label": label},
            )
        return None

    return None


def default_needs_approval(
    action: AgentAction,
    context: ApprovalContext | None = None,
) -> bool:
    """Boolean wrapper around :func:`needs_approval` for legacy hooks.

    Args:
        action: Proposed agent action.
        context: Optional approval context.

    Returns:
        ``True`` when a human gate is required.
    """
    return needs_approval(action, context) is not None


def coerce_approval_request(
    result: ApprovalRequest | bool | None,
    action: AgentAction,
) -> ApprovalRequest | None:
    """Normalize a hook return value into an optional :class:`ApprovalRequest`.

    Args:
        result: Hook output (request, bool, or ``None``).
        action: Action under consideration (for bool→request conversion).

    Returns:
        Concrete request when approval is required, else ``None``.
    """
    if result is None or result is False:
        return None
    if result is True:
        return ApprovalRequest(
            reason=f"{action.kind.value} requires human approval before execution.",
            action_kind=action.kind,
            target_index=action.target_index,
            policy_id="custom_hook",
            metadata={"kind": action.kind.value},
        )
    return result


async def wait_for_approval_decision(
    *,
    signals: RunControlSignals | None,
    is_cancelled: StatusCheck,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> ApprovalDecision:
    """Park until approve, reject, cancel, or fail-closed timeout.

    ``runs.status`` remains authoritative for cancel; ``signals.approval_decision``
    carries grant/deny from the API. A missed wake recovers on the next poll.

    Args:
        signals: Optional in-process wake hub for this run.
        is_cancelled: Sync or async cancel check.
        timeout_seconds: Fail-closed bound (must be positive).
        poll_seconds: Fallback re-check interval.

    Returns:
        ``granted``, ``denied``, ``timeout``, or ``cancelled``.
    """

    async def _call(fn: StatusCheck) -> bool:
        result = fn()
        if isinstance(result, Awaitable):
            return bool(await result)
        return bool(result)

    if timeout_seconds <= 0:
        return "timeout"

    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while True:
        if await _call(is_cancelled):
            return "cancelled"

        if signals is not None:
            decision = signals.consume_approval_decision()
            if decision == "granted":
                return "granted"
            if decision == "denied":
                return "denied"

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return "timeout"

        wait_for = min(poll_seconds, remaining)
        if signals is None:
            await asyncio.sleep(wait_for)
            continue

        signals.wake.clear()
        # Re-check after clear to avoid a lost wake between poll and wait.
        if await _call(is_cancelled):
            return "cancelled"
        decision = signals.consume_approval_decision()
        if decision == "granted":
            return "granted"
        if decision == "denied":
            return "denied"
        try:
            await asyncio.wait_for(signals.wake.wait(), timeout=wait_for)
        except TimeoutError:
            continue


def invoke_needs_approval(
    hook: Callable[..., ApprovalRequest | bool | None],
    action: AgentAction,
    context: ApprovalContext,
) -> ApprovalRequest | None:
    """Call a 1-arg or 2-arg needs-approval hook and coerce the result.

    Args:
        hook: Callable ``(action)`` or ``(action, context)``.
        action: Proposed action.
        context: Surrounding context.

    Returns:
        Normalized :class:`ApprovalRequest` or ``None``.
    """
    try:
        raw = hook(action, context)
    except TypeError:
        raw = hook(action)
    return coerce_approval_request(raw, action)
