"""Constrained Jev / Browser Use action space (Phase 1).

Element targeting
-----------------
Candidates are addressed by a **snapshot-local integer index** taken from
Browser Use's ``selector_map`` (highlight / interactable index for the current
observation). The adapter and executor must:

1. Prefer ``index`` over inventing CSS selectors or screen coordinates.
2. Treat indices as valid only for the observation they came from; re-resolve
   before execute (T015) and abort if the index is gone or the node hash moved.
3. Never put passwords, CVVs, cookies, or raw form values into Jev criteria
   labels — only role, tag, accessible name, and similar non-secret hints.

Action space is deliberately small. Bitwarden kinds are stubs until T027.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Core ops Jev may select in Phase 1.
CORE_ACTION_KINDS: Final[tuple[str, ...]] = (
    "CLICK",
    "TYPE_TEXT",
    "SCROLL",
    "GO_BACK",
    "NAVIGATE",
    "DONE",
)

# Reserved specialized ops (stubs; execution lands in T027).
BITWARDEN_ACTION_KINDS: Final[tuple[str, ...]] = (
    "BITWARDEN_LOGIN",
    "BITWARDEN_IDENTITY",
    "BITWARDEN_CARD",
)

ALL_ACTION_KINDS: Final[tuple[str, ...]] = CORE_ACTION_KINDS + BITWARDEN_ACTION_KINDS


class ActionKind(StrEnum):
    """Allowed agent action kinds for the constrained Jev action space."""

    CLICK = "CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    SCROLL = "SCROLL"
    GO_BACK = "GO_BACK"
    NAVIGATE = "NAVIGATE"
    DONE = "DONE"
    BITWARDEN_LOGIN = "BITWARDEN_LOGIN"
    BITWARDEN_IDENTITY = "BITWARDEN_IDENTITY"
    BITWARDEN_CARD = "BITWARDEN_CARD"


class ScrollDirection(StrEnum):
    """Scroll axis for ``SCROLL`` actions."""

    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


# Kinds that require a candidate element index.
TARGETED_KINDS: Final[frozenset[ActionKind]] = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.TYPE_TEXT,
        ActionKind.BITWARDEN_LOGIN,
        ActionKind.BITWARDEN_IDENTITY,
        ActionKind.BITWARDEN_CARD,
    }
)

# Default operations advertised to Jev when the page has interactable candidates.
DEFAULT_AVAILABLE_OPERATIONS: Final[tuple[ActionKind, ...]] = tuple(
    ActionKind(k) for k in ALL_ACTION_KINDS
)


class CandidateElement(BaseModel):
    """Interactable element summary safe to send to Jev.

    Attributes:
        index: Snapshot-local Browser Use selector-map index.
        tag: Lowercased HTML tag when known.
        role: ARIA role when known.
        name: Accessible name / label / short text (never a password value).
        href: Link target when present and non-secret.
        input_type: ``type`` attribute for inputs (``password`` is normalized).
        is_editable: Whether the element accepts text input.
        is_password_field: True when the control is a password input (no value).
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, description="Snapshot-local interactable index.")
    tag: str | None = None
    role: str | None = None
    name: str | None = None
    href: str | None = None
    input_type: str | None = None
    is_editable: bool = False
    is_password_field: bool = False

    @field_validator("tag", "role", "name", "href", "input_type", mode="before")
    @classmethod
    def _strip_empty(cls, value: Any) -> Any:
        """Normalize blank strings to ``None``."""
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    def criteria_label(self) -> str:
        """Build a short, non-secret label for Jev choice criteria.

        Returns:
            Human-readable description keyed by index in the Jev request.
        """
        parts: list[str] = []
        if self.tag:
            parts.append(self.tag)
        if self.role and self.role != self.tag:
            parts.append(f"role={self.role}")
        if self.is_password_field:
            parts.append("password-field")
        elif self.input_type:
            parts.append(f"type={self.input_type}")
        if self.name:
            # Cap length so criteria stay small / cheap for Jev tokens.
            label = self.name if len(self.name) <= 80 else f"{self.name[:77]}..."
            parts.append(f'"{label}"')
        if self.href:
            href = self.href if len(self.href) <= 60 else f"{self.href[:57]}..."
            parts.append(href)
        if not parts:
            return f"element[{self.index}]"
        return " ".join(parts)


class BrowserObservation(BaseModel):
    """Browser state summary fed into the Jev adapter.

    Attributes:
        url: Current page URL.
        title: Document title.
        candidates: Interactable elements with snapshot-local indices.
        pixels_above: Scrollable pixels above the viewport.
        pixels_below: Scrollable pixels below the viewport.
        page_summary: Optional short textual page digest (already redacted).
        suggested_urls: Optional navigate targets (bookmarks / extracted links).
        browser_errors: Recent browser/page errors when present.
        available_operations: Ops to advertise; defaults to the full Phase-1 set.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    title: str = ""
    candidates: list[CandidateElement] = Field(default_factory=list)
    pixels_above: int = 0
    pixels_below: int = 0
    page_summary: str | None = None
    suggested_urls: list[str] = Field(default_factory=list)
    browser_errors: list[str] = Field(default_factory=list)
    available_operations: list[ActionKind] = Field(
        default_factory=lambda: list(DEFAULT_AVAILABLE_OPERATIONS),
    )

    def candidate_by_index(self, index: int) -> CandidateElement | None:
        """Return the candidate with the given snapshot index, if any.

        Args:
            index: Snapshot-local element index.

        Returns:
            Matching candidate, or ``None``.
        """
        for candidate in self.candidates:
            if candidate.index == index:
                return candidate
        return None

    def click_candidates(self) -> list[CandidateElement]:
        """Return candidates suitable for ``CLICK``."""
        return list(self.candidates)

    def type_candidates(self) -> list[CandidateElement]:
        """Return candidates suitable for ``TYPE_TEXT`` / Bitwarden fills."""
        return [c for c in self.candidates if c.is_editable or c.is_password_field]


class ActionParams(BaseModel):
    """Optional parameters attached to an :class:`AgentAction`.

    Attributes:
        url: Target URL for ``NAVIGATE``.
        text: Typed string for ``TYPE_TEXT`` (usually filled later by T016).
        direction: Scroll direction for ``SCROLL``.
        message: Completion note for ``DONE``.
        amount: Optional scroll magnitude hint.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    text: str | None = None
    direction: ScrollDirection | None = None
    message: str | None = None
    amount: int | None = Field(default=None, ge=0)


class AgentAction(BaseModel):
    """Concrete action produced from a Jev decision for Browser Use execution.

    Confidence and per-option probabilities are retained for audit and approval
    gates (T017 / T021). They are never secret.

    Attributes:
        kind: Selected action kind.
        target_index: Snapshot-local element index when the kind is targeted.
        params: Kind-specific parameters.
        confidence: Calibrated confidence for the chosen operation (0-1).
        probabilities: Operation-level probability mass from Jev.
        target_confidence: Confidence for the chosen target head when present.
        target_probabilities: Target-head probability mass when present.
        alternatives: Other high-probability operation keys for debugging.
        rationale: Optional short reason string.
        raw_answers: Redacted Jev answer map retained for audit traces.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    target_index: int | None = Field(default=None, ge=0)
    params: ActionParams = Field(default_factory=ActionParams)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)
    target_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    target_probabilities: dict[str, float] = Field(default_factory=dict)
    alternatives: list[str] = Field(default_factory=list)
    rationale: str | None = None
    raw_answers: dict[str, Any] = Field(default_factory=dict)

    def requires_target(self) -> bool:
        """Return whether this kind must carry a ``target_index``."""
        return self.kind in TARGETED_KINDS
