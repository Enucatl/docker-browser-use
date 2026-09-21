"""Human approval gates for high-impact browser actions (T021/T028).

Conservative Phase-1 policy: Bitwarden fills always require approval; clicks and
navigations that look like purchases, payments, account destruction, or other
irreversible submissions require approval when configured rules match.
Ordinary navigation, scrolling, typing, and benign clicks do not.

Reject and timeout both **fail the run** (fail closed). Never auto-approve in
production defaults.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import os
import re
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    """Optional surroundings for the approval policy.

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
        reason_code: Stable machine-readable policy reason.
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
    reason_code: str = "approval.required"


@dataclass(frozen=True, slots=True)
class ApprovalSettings:
    """Runtime knobs for approval waits.

    Attributes:
        timeout_seconds: Fail-closed wait bound (never auto-approve).
    """

    timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    """One ordered, data-driven approval rule."""

    rule_id: str
    reason_code: str
    message: str
    action_types: frozenset[str] = frozenset()
    url_regex: str | None = None
    element_text_regex: str | None = None
    confidence_below: float | None = None


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Pure first-match evaluator for approval rules."""

    version: int
    rules: tuple[ApprovalRule, ...]

    def evaluate(
        self,
        action: AgentAction,
        context: ApprovalContext | None = None,
    ) -> ApprovalRequest | None:
        """Return the first matching approval request, if any.

        Args:
            action: Proposed browser action.
            context: Current observation and operator goal.

        Returns:
            The configured approval request, or ``None`` when allowed.
        """
        ctx = context or ApprovalContext()
        candidate = None
        if ctx.observation is not None and action.target_index is not None:
            candidate = ctx.observation.candidate_by_index(action.target_index)
        element_text = _element_text(candidate, action)
        url = action.params.url or (candidate.href if candidate is not None else None)
        url = url or ctx.url or (ctx.observation.url if ctx.observation is not None else None)

        for rule in self.rules:
            if rule.action_types and action.kind.value not in rule.action_types:
                continue
            if rule.url_regex and (not url or re.search(rule.url_regex, url) is None):
                continue
            if rule.element_text_regex and (
                re.search(rule.element_text_regex, element_text) is None
            ):
                continue
            if rule.confidence_below is not None and (
                action.confidence is None or action.confidence >= rule.confidence_below
            ):
                continue

            reason = rule.message.format(
                action_kind=action.kind.value,
                confidence=action.confidence if action.confidence is not None else "unknown",
                element_text=element_text,
                url=url or "unknown URL",
            )
            metadata: dict[str, Any] = {"kind": action.kind.value}
            if rule.url_regex and url:
                metadata["url"] = url
            return ApprovalRequest(
                reason=reason,
                action_kind=action.kind,
                reason_code=rule.reason_code,
                target_index=action.target_index,
                policy_id=rule.rule_id,
                metadata=metadata,
            )
        return None


def _element_text(candidate: CandidateElement | None, action: AgentAction) -> str:
    """Build non-secret text used by element rules."""
    values = []
    if candidate is not None:
        values.extend((candidate.name, candidate.role, candidate.href, candidate.tag))
    values.append(action.rationale)
    return _normalize_text(" ".join(value for value in values if value))


def load_approval_policy(path: str | Path | None = None) -> ApprovalPolicy:
    """Load the ordered approval policy from TOML.

    Args:
        path: Optional policy path, primarily useful for tests.

    Returns:
        Parsed immutable policy.

    Raises:
        ValueError: If the policy shape or regular expressions are invalid.
    """
    if path is None:
        resource = importlib.resources.files("browser_use_agent.policy").joinpath(
            "approval_policy.toml"
        )
        raw = tomllib.loads(resource.read_text(encoding="utf-8"))
    else:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    try:
        version = int(raw["version"])
        rules = tuple(
            ApprovalRule(
                rule_id=str(item["id"]),
                reason_code=str(item["reason_code"]),
                message=str(item["message"]),
                action_types=frozenset(str(kind) for kind in item.get("action_types", [])),
                url_regex=str(item["url_regex"]) if "url_regex" in item else None,
                element_text_regex=(
                    str(item["element_text_regex"]) if "element_text_regex" in item else None
                ),
                confidence_below=(
                    float(item["confidence_below"]) if "confidence_below" in item else None
                ),
            )
            for item in raw["rules"]
        )
        for rule in rules:
            if rule.url_regex:
                re.compile(rule.url_regex)
            if rule.element_text_regex:
                re.compile(rule.element_text_regex)
            if not rule.rule_id or not rule.reason_code or not rule.message:
                raise ValueError("approval rules require id, reason_code, and message")
    except (KeyError, TypeError, ValueError, re.error) as exc:
        raise ValueError("invalid approval policy") from exc
    return ApprovalPolicy(version=version, rules=rules)


DEFAULT_APPROVAL_POLICY = load_approval_policy()


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


def needs_approval(
    action: AgentAction,
    context: ApprovalContext | None = None,
) -> ApprovalRequest | None:
    """Decide whether ``action`` must wait for a human approve/reject.

    The packaged policy is ordered and first-match wins. Never returns an
    auto-approve signal — absence of a request means the action may proceed.

    Args:
        action: Proposed agent action.
        context: Optional observation / goal for richer heuristics.

    Returns:
        :class:`ApprovalRequest` when blocked, otherwise ``None``.
    """
    return DEFAULT_APPROVAL_POLICY.evaluate(action, context)


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
