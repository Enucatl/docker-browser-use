"""Browser Use ↔ Jev adapter.

Maps a redacted :class:`~browser_use_agent.policy.actions.BrowserObservation`
plus goal/history into a :class:`~browser_use_agent.policy.jev_client.JevRequest`,
and maps a :class:`~browser_use_agent.policy.jev_client.JevResponse` back to an
:class:`~browser_use_agent.policy.actions.AgentAction`.

Element indices stay snapshot-local (see ``actions`` module docstring). Speculative
target heads (``click_target``, ``type_target``, …) are always included when
candidates exist; only the head matching the chosen operation is applied.
"""

from __future__ import annotations

from typing import Any

from browser_use_agent.policy.actions import (
    TARGETED_KINDS,
    ActionKind,
    ActionParams,
    AgentAction,
    BrowserObservation,
    CandidateElement,
    ScrollDirection,
)
from browser_use_agent.policy.jev_client import (
    JevChoiceAnswer,
    JevChoiceQuestion,
    JevRequest,
    JevResponse,
)
from browser_use_agent.security import REDACTED, redact_for_audit, redact_text

OPERATION_INSTRUCTIONS = (
    "Given the operator goal, recent history, and the current page candidates, "
    "choose exactly one next browser operation."
)

CLICK_TARGET_INSTRUCTIONS = "Choose the element index to click."
TYPE_TARGET_INSTRUCTIONS = "Choose the element index that should receive text."
BITWARDEN_TARGET_INSTRUCTIONS = (
    "Choose the element index for a Bitwarden autofill (no secrets in state)."
)
SCROLL_INSTRUCTIONS = "Choose the scroll direction for this page."
NAVIGATE_INSTRUCTIONS = "Choose which suggested URL to open next."

OPERATION_CRITERIA: dict[str, str] = {
    ActionKind.CLICK.value: "Click an interactable element by index.",
    ActionKind.TYPE_TEXT.value: "Type into an editable field (text filled later).",
    ActionKind.SCROLL.value: "Scroll the page viewport.",
    ActionKind.GO_BACK.value: "Navigate back in browser history.",
    ActionKind.NAVIGATE.value: "Open a suggested URL.",
    ActionKind.DONE.value: "The goal is complete; stop the run.",
    ActionKind.BITWARDEN_LOGIN.value: "Fill the current site's login via Bitwarden.",
    ActionKind.BITWARDEN_IDENTITY.value: "Fill identity fields via Bitwarden.",
    ActionKind.BITWARDEN_CARD.value: "Fill card fields via Bitwarden.",
}


class JevAdapterError(ValueError):
    """Raised when a Jev response cannot be mapped to a valid agent action."""


class JevAdapter:
    """Bidirectional mapper between Browser Use observations and Jev I/O.

    Attributes:
        model: Default model pin stamped on requests.
        low_confidence_threshold: Below this, alternatives are still preserved
            but callers may escalate (T021); mapping itself does not fail.
    """

    def __init__(
        self,
        *,
        model: str = "jev-latest",
        low_confidence_threshold: float = 0.5,
    ) -> None:
        """Create an adapter.

        Args:
            model: Jev model identifier for requests.
            low_confidence_threshold: Soft threshold recorded for callers.
        """
        self.model = model
        self.low_confidence_threshold = low_confidence_threshold

    def to_jev_request(
        self,
        observation: BrowserObservation,
        goal: str,
        history_summary: str = "",
    ) -> JevRequest:
        """Build a Jev decision request from an observation and goal.

        Secret-looking fields are stripped via redaction. Password field *values*
        are never included; password inputs appear only as labelled controls.

        Args:
            observation: Current browser observation (candidates + page meta).
            goal: Operator goal for this run.
            history_summary: Short prior-step summary (already non-secret).

        Returns:
            Validated :class:`JevRequest` with operation + speculative targets.

        Raises:
            ValueError: If redaction cannot produce a safe state object.
        """
        ops = list(observation.available_operations) or list(ActionKind)
        # When there are no candidates, drop targeted ops so Jev cannot pick them.
        if not observation.candidates:
            ops = [
                op
                for op in ops
                if op
                not in {
                    ActionKind.CLICK,
                    ActionKind.TYPE_TEXT,
                    ActionKind.BITWARDEN_LOGIN,
                    ActionKind.BITWARDEN_IDENTITY,
                    ActionKind.BITWARDEN_CARD,
                }
            ]
        if ActionKind.DONE not in ops:
            ops.append(ActionKind.DONE)

        criteria = {
            op.value: OPERATION_CRITERIA.get(op.value, op.value)
            for op in ops
            if op.value in OPERATION_CRITERIA
        }

        questions: dict[str, JevChoiceQuestion] = {
            "operation": JevChoiceQuestion(
                instructions=OPERATION_INSTRUCTIONS,
                criteria=criteria,
            ),
        }

        click_criteria = _candidate_criteria(observation.click_candidates())
        if click_criteria and ActionKind.CLICK in ops:
            questions["click_target"] = JevChoiceQuestion(
                instructions=CLICK_TARGET_INSTRUCTIONS,
                criteria=click_criteria,
            )

        type_criteria = _candidate_criteria(observation.type_candidates())
        if type_criteria and (
            ActionKind.TYPE_TEXT in ops
            or ActionKind.BITWARDEN_LOGIN in ops
            or ActionKind.BITWARDEN_IDENTITY in ops
            or ActionKind.BITWARDEN_CARD in ops
        ):
            questions["type_target"] = JevChoiceQuestion(
                instructions=TYPE_TARGET_INSTRUCTIONS,
                criteria=type_criteria,
            )
            if any(
                op in ops
                for op in (
                    ActionKind.BITWARDEN_LOGIN,
                    ActionKind.BITWARDEN_IDENTITY,
                    ActionKind.BITWARDEN_CARD,
                )
            ):
                questions["bitwarden_target"] = JevChoiceQuestion(
                    instructions=BITWARDEN_TARGET_INSTRUCTIONS,
                    criteria=type_criteria,
                )

        if ActionKind.SCROLL in ops:
            questions["scroll_direction"] = JevChoiceQuestion(
                instructions=SCROLL_INSTRUCTIONS,
                criteria={
                    ScrollDirection.UP.value: "Scroll up",
                    ScrollDirection.DOWN.value: "Scroll down",
                    ScrollDirection.LEFT.value: "Scroll left",
                    ScrollDirection.RIGHT.value: "Scroll right",
                },
            )

        suggested = [u for u in observation.suggested_urls if u and u.strip()]
        if not suggested and observation.url:
            # Allow re-navigation to the current URL as a safe no-op option set.
            suggested = [observation.url]
        if ActionKind.NAVIGATE in ops and suggested:
            questions["navigate_url"] = JevChoiceQuestion(
                instructions=NAVIGATE_INSTRUCTIONS,
                criteria={u: u for u in suggested[:32]},
            )

        state = {
            "goal": redact_text(goal),
            "history_summary": redact_text(history_summary) if history_summary else "",
            "url": observation.url,
            "title": redact_text(observation.title) if observation.title else "",
            "pixels_above": observation.pixels_above,
            "pixels_below": observation.pixels_below,
            "page_summary": (
                redact_text(observation.page_summary) if observation.page_summary else None
            ),
            "candidates": [
                {
                    "index": c.index,
                    "tag": c.tag,
                    "role": c.role,
                    "name": c.name,
                    "href": c.href,
                    "input_type": ("password" if c.is_password_field else c.input_type),
                    "is_editable": c.is_editable,
                    "is_password_field": c.is_password_field,
                }
                for c in observation.candidates
            ],
            "browser_errors": [redact_text(e) for e in observation.browser_errors],
            "available_operations": [op.value for op in ops],
        }
        safe_state = redact_for_audit(state)
        if not isinstance(safe_state, dict):
            raise ValueError("redact_for_audit must return a mapping for Jev state")
        # Belt-and-suspenders: never ship placeholder secrets as if real.
        _strip_redacted_noise(safe_state)

        return JevRequest(model=self.model, state=safe_state, questions=questions)

    def from_jev_response(
        self,
        resp: JevResponse,
        *,
        observation: BrowserObservation | None = None,
    ) -> AgentAction:
        """Map a Jev response to a single :class:`AgentAction`.

        Args:
            resp: Jev decision response.
            observation: Optional observation used to validate target indices.

        Returns:
            Executable agent action with confidence / probabilities preserved.

        Raises:
            JevAdapterError: When the operation or required target is invalid.
        """
        op_answer = resp.choice("operation")
        if op_answer is None:
            raise JevAdapterError("Jev response missing operation choice answer")

        try:
            kind = ActionKind(op_answer.choice)
        except ValueError as exc:
            raise JevAdapterError(f"Unknown operation {op_answer.choice!r}") from exc

        alternatives = _alternative_keys(op_answer, exclude=kind.value)
        params = ActionParams()
        target_index: int | None = None
        target_confidence: float | None = None
        target_probabilities: dict[str, float] = {}

        if kind in TARGETED_KINDS:
            target_answer = _resolve_target_answer(resp, kind)
            if target_answer is None:
                raise JevAdapterError(f"Operation {kind.value} requires a target choice")
            try:
                target_index = int(target_answer.choice)
            except ValueError as exc:
                raise JevAdapterError(
                    f"Target choice must be an integer index, got {target_answer.choice!r}",
                ) from exc
            target_confidence = target_answer.confidence
            target_probabilities = dict(target_answer.probabilities)
            if observation is not None and observation.candidate_by_index(target_index) is None:
                raise JevAdapterError(
                    f"Target index {target_index} not present in observation candidates",
                )

        if kind == ActionKind.SCROLL:
            scroll = resp.choice("scroll_direction")
            direction = ScrollDirection.DOWN
            if scroll is not None:
                try:
                    direction = ScrollDirection(scroll.choice)
                except ValueError as exc:
                    raise JevAdapterError(
                        f"Invalid scroll direction {scroll.choice!r}",
                    ) from exc
            params = ActionParams(direction=direction)

        if kind == ActionKind.NAVIGATE:
            nav = resp.choice("navigate_url")
            if nav is None or not nav.choice:
                raise JevAdapterError("NAVIGATE requires a navigate_url choice")
            params = ActionParams(url=nav.choice)

        if kind == ActionKind.DONE:
            done_meta = resp.choice("done_meta")
            if done_meta is not None:
                params = ActionParams(message=done_meta.choice)

        raw_answers = redact_for_audit(dict(resp.answers))
        if not isinstance(raw_answers, dict):
            raw_answers = {}

        action = AgentAction(
            kind=kind,
            target_index=target_index,
            params=params,
            confidence=op_answer.confidence,
            probabilities=dict(op_answer.probabilities),
            target_confidence=target_confidence,
            target_probabilities=target_probabilities,
            alternatives=alternatives,
            rationale=None,
            raw_answers=raw_answers,
        )
        return action

    def is_low_confidence(self, action: AgentAction) -> bool:
        """Return whether the action falls below the soft confidence threshold.

        Args:
            action: Mapped agent action.

        Returns:
            ``True`` when confidence is present and below the threshold.
        """
        if action.confidence is None:
            return False
        return action.confidence < self.low_confidence_threshold


def observation_from_browser_state(state: Any) -> BrowserObservation:
    """Build a :class:`BrowserObservation` from Browser Use state when available.

    Accepts a ``BrowserStateSummary``-like object or a plain mapping. Missing
    fields default safely. Candidate names omit attribute values that look like
    secrets (``value`` on password inputs is dropped).

    Args:
        state: Browser Use summary or mapping with ``url`` / ``title`` /
            ``selector_map``-like candidates.

    Returns:
        Adapter-ready observation.
    """
    if isinstance(state, BrowserObservation):
        return state

    if isinstance(state, dict):
        return _observation_from_mapping(state)

    url = str(getattr(state, "url", "") or "")
    title = str(getattr(state, "title", "") or "")
    pixels_above = int(getattr(state, "pixels_above", 0) or 0)
    pixels_below = int(getattr(state, "pixels_below", 0) or 0)
    browser_errors = list(getattr(state, "browser_errors", None) or [])

    candidates: list[CandidateElement] = []
    dom_state = getattr(state, "dom_state", None)
    selector_map = getattr(dom_state, "selector_map", None) if dom_state is not None else None
    if isinstance(selector_map, dict):
        for index, node in selector_map.items():
            candidates.append(_candidate_from_dom_node(int(index), node))

    return BrowserObservation(
        url=url,
        title=title,
        candidates=candidates,
        pixels_above=pixels_above,
        pixels_below=pixels_below,
        browser_errors=[str(e) for e in browser_errors],
    )


def _observation_from_mapping(data: dict[str, Any]) -> BrowserObservation:
    """Parse an observation from a plain dict (tests / checkpoints).

    Args:
        data: Mapping with observation fields.

    Returns:
        Validated observation.
    """
    raw_candidates = data.get("candidates") or []
    candidates: list[CandidateElement] = []
    for item in raw_candidates:
        if isinstance(item, CandidateElement):
            candidates.append(item)
        elif isinstance(item, dict):
            candidates.append(CandidateElement.model_validate(item))
    ops_raw = data.get("available_operations")
    ops = [ActionKind(o) for o in ops_raw] if ops_raw else list(ActionKind)
    return BrowserObservation(
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        candidates=candidates,
        pixels_above=int(data.get("pixels_above") or 0),
        pixels_below=int(data.get("pixels_below") or 0),
        page_summary=data.get("page_summary"),
        suggested_urls=list(data.get("suggested_urls") or []),
        browser_errors=[str(e) for e in data.get("browser_errors") or []],
        available_operations=ops,
    )


def _candidate_from_dom_node(index: int, node: Any) -> CandidateElement:
    """Convert a Browser Use DOM node into a safe candidate summary.

    Args:
        index: Snapshot-local index.
        node: ``EnhancedDOMTreeNode``-like object.

    Returns:
        Candidate without secret attribute values.
    """
    attrs = getattr(node, "attributes", None) or {}
    if not isinstance(attrs, dict):
        attrs = {}
    tag = (getattr(node, "tag_name", None) or getattr(node, "node_name", None) or "").lower()
    input_type = str(attrs.get("type") or "").lower() or None
    is_password = input_type == "password" or str(attrs.get("name") or "").lower() in {
        "password",
        "passwd",
        "pwd",
    }
    role = attrs.get("role")
    name = attrs.get("aria-label") or attrs.get("name") or attrs.get("placeholder")
    if not name:
        try:
            name = getattr(node, "get_meaningful_text_for_llm", lambda: None)()
        except Exception:
            name = None
    if isinstance(name, str) and is_password:
        # Never forward password control values as the accessible name.
        name = attrs.get("aria-label") or attrs.get("placeholder") or "password"
    href = attrs.get("href")
    is_editable = tag in {"input", "textarea"} or attrs.get("contenteditable") == "true"
    return CandidateElement(
        index=index,
        tag=tag or None,
        role=str(role) if role else None,
        name=str(name) if name else None,
        href=str(href) if href else None,
        input_type="password" if is_password else input_type,
        is_editable=bool(is_editable),
        is_password_field=is_password,
    )


def _candidate_criteria(candidates: list[CandidateElement]) -> dict[str, str | None]:
    """Build Jev choice criteria keyed by index string.

    Args:
        candidates: Elements to advertise.

    Returns:
        Criteria map (max 255 entries per Jev choice limits).
    """
    out: dict[str, str | None] = {}
    for candidate in candidates[:255]:
        out[str(candidate.index)] = candidate.criteria_label()
    return out


def _resolve_target_answer(resp: JevResponse, kind: ActionKind) -> JevChoiceAnswer | None:
    """Pick the speculative target head for a targeted operation.

    Args:
        resp: Full Jev response.
        kind: Chosen operation.

    Returns:
        Target choice answer, or ``None``.
    """
    if kind == ActionKind.CLICK:
        return resp.choice("click_target")
    if kind == ActionKind.TYPE_TEXT:
        return resp.choice("type_target") or resp.choice("click_target")
    if kind in {
        ActionKind.BITWARDEN_LOGIN,
        ActionKind.BITWARDEN_IDENTITY,
        ActionKind.BITWARDEN_CARD,
    }:
        return resp.choice("bitwarden_target") or resp.choice("type_target")
    return None


def _alternative_keys(answer: JevChoiceAnswer, *, exclude: str) -> list[str]:
    """List other operations ordered by descending probability.

    Args:
        answer: Operation choice answer.
        exclude: Winning key to omit.

    Returns:
        Alternative operation keys (highest probability first).
    """
    items = [
        (key, prob)
        for key, prob in answer.probabilities.items()
        if key != exclude and isinstance(prob, int | float)
    ]
    items.sort(key=lambda pair: pair[1], reverse=True)
    return [key for key, _ in items]


def _strip_redacted_noise(state: dict[str, Any]) -> None:
    """Drop mapping entries whose values are solely the redaction placeholder.

    Args:
        state: Mutable redacted state dict.
    """
    doomed = [k for k, v in state.items() if v == REDACTED]
    for key in doomed:
        del state[key]
