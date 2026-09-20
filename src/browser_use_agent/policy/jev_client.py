"""Pluggable Jev Decision API client.

Assumed wire shapes (System One / hosted Jev Decision API)
----------------------------------------------------------
Request ``POST {base_url}/v1/decide`` (or TypeSafe ``/v1/systemone``)::

    {
      "model": "jev-latest",
      "state": { ... } | "string" | [...],
      "questions": {
        "operation": {
          "type": "choice",
          "instructions": "...",
          "criteria": {"CLICK": "Click an element", "DONE": "Finish", ...}
        },
        "click_target": {
          "type": "choice",
          "instructions": "...",
          "criteria": {"0": "button \\"Submit\\"", "3": "a Home", ...}
        },
        ...
      }
    }

Response::

    {
      "model": "jev-1.13.0",
      "answers": {
        "operation": {
          "type": "choice",
          "choice": "CLICK",
          "confidence": 0.91,
          "probabilities": {"CLICK": 0.91, "DONE": 0.09, ...}
        },
        "click_target": {
          "type": "choice",
          "choice": "3",
          "confidence": 0.88,
          "probabilities": {"0": 0.1, "3": 0.88, ...}
        }
      },
      "usage": {
        "input_tokens": 120,
        "output_tokens": 0,
        "cost_usd": 0.00005,
        "credits_remaining_usd": 4.99
      }
    }

Auth: ``Authorization: Bearer jv_live_…`` from ``JEV_API_KEY`` / ``*_FILE``.
This module isolates the HTTP boundary so the real endpoint can be swapped
without changing the adapter. Production credentials beyond config hooks are
out of scope for T014; use :class:`FakeJevClient` in tests.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from browser_use_agent.policy.actions import ActionKind

QuestionType = Literal["choice", "score", "noul"]


class JevChoiceQuestion(BaseModel):
    """A Jev ``choice`` question (pick one labelled option)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str | None] = Field(default_factory=dict)


class JevScoreQuestion(BaseModel):
    """A Jev ``score`` question (ordered levels)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str] = Field(default_factory=list)


class JevNoulQuestion(BaseModel):
    """A Jev ``noul`` yes/no probability question."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["noul"] = "noul"
    instructions: str
    criteria: dict[str, str] | None = None


JevQuestion = JevChoiceQuestion | JevScoreQuestion | JevNoulQuestion


class JevRequest(BaseModel):
    """Outbound decision request matching the assumed Jev wire shape.

    Attributes:
        model: Model pin (``jev-latest`` or a version).
        state: Non-secret decision context (goal, page summary, candidates).
        questions: Named typed questions evaluated in one round trip.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = "jev-latest"
    state: Any
    questions: dict[str, JevQuestion]

    @model_validator(mode="after")
    def _reject_password_keys_in_state(self) -> JevRequest:
        """Fail closed if an obviously secret field slipped into ``state``."""
        _assert_no_secret_keys(self.state, path="state")
        return self


class JevChoiceAnswer(BaseModel):
    """Parsed ``choice`` answer from Jev."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["choice"] = "choice"
    choice: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)


class JevScoreAnswer(BaseModel):
    """Parsed ``score`` answer from Jev."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["score"] = "score"
    score: float
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)
    legend: list[str] | None = None


class JevNoulAnswer(BaseModel):
    """Parsed ``noul`` answer from Jev."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["noul"] = "noul"
    noul: float = Field(ge=0.0, le=1.0)


JevAnswer = JevChoiceAnswer | JevScoreAnswer | JevNoulAnswer


class JevUsage(BaseModel):
    """Optional usage / billing block from a Jev response."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    credits_remaining_usd: float | None = None


class JevResponse(BaseModel):
    """Inbound decision response matching the assumed Jev wire shape.

    Attributes:
        model: Resolved model version.
        answers: Map of question name → typed answer.
        usage: Optional token/cost accounting.
    """

    model_config = ConfigDict(extra="ignore")

    model: str = "jev-latest"
    answers: dict[str, Any]
    usage: JevUsage | None = None

    def choice(self, name: str) -> JevChoiceAnswer | None:
        """Return a typed choice answer by question name.

        Args:
            name: Question key from the request.

        Returns:
            Parsed choice answer, or ``None`` when missing / wrong type.
        """
        raw = self.answers.get(name)
        if not isinstance(raw, Mapping):
            return None
        if raw.get("type", "choice") != "choice" or "choice" not in raw:
            return None
        return JevChoiceAnswer.model_validate(raw)


@dataclass(frozen=True, slots=True)
class JevClientSettings:
    """Config hooks for a real Jev HTTP client (credentials not required for tests).

    Attributes:
        base_url: API root without trailing slash.
        api_key: Bearer token (``jv_live_…``); never log this value.
        model: Default model pin.
        timeout_seconds: HTTP timeout.
    """

    base_url: str = "https://jevtypesafeai.com/api"
    api_key: str | None = None
    model: str = "jev-latest"
    timeout_seconds: float = 30.0


def load_jev_client_settings() -> JevClientSettings:
    """Load Jev client hooks from the environment.

    Reads ``JEV_BASE_URL``, ``JEV_MODEL``, ``JEV_TIMEOUT_SECONDS``, and
    ``JEV_API_KEY`` or ``JEV_API_KEY_FILE`` (Docker secret file).

    Returns:
        Immutable settings snapshot (key may be ``None``).
    """
    api_key = os.environ.get("JEV_API_KEY", "").strip() or None
    if api_key is None:
        key_file = os.environ.get("JEV_API_KEY_FILE", "").strip()
        if key_file:
            from pathlib import Path

            api_key = Path(key_file).read_text(encoding="utf-8").strip() or None

    timeout_raw = os.environ.get("JEV_TIMEOUT_SECONDS", "30").strip()
    return JevClientSettings(
        base_url=os.environ.get("JEV_BASE_URL", "https://jevtypesafeai.com/api").rstrip("/"),
        api_key=api_key,
        model=os.environ.get("JEV_MODEL", "jev-latest").strip() or "jev-latest",
        timeout_seconds=float(timeout_raw),
    )


class JevClientError(RuntimeError):
    """Base error for Jev client failures."""


class JevClientNotConfiguredError(JevClientError):
    """Raised when a live client is used without an API key."""


class JevClient(ABC):
    """Abstract Jev decision client."""

    @abstractmethod
    def decide(self, request: JevRequest) -> JevResponse:
        """Send a decision request and return the typed response.

        Args:
            request: Adapter-built Jev request (no secrets).

        Returns:
            Parsed Jev response with answers and optional usage.
        """


@dataclass
class FakeDecision:
    """One scripted decision step for :class:`FakeJevClient`.

    Attributes:
        operation: Action kind string (e.g. ``CLICK``).
        target_key: Target criteria key (usually an index string).
        confidence: Operation confidence to report.
        probabilities: Optional operation probability map.
        target_confidence: Optional target-head confidence.
        target_probabilities: Optional target probability map.
        scroll_direction: For ``SCROLL``, criteria key (``UP``/``DOWN``/…).
        navigate_url: For ``NAVIGATE``, chosen URL criteria key.
        done_message: Optional ``DONE`` message stored in answers metadata.
    """

    operation: str
    target_key: str | None = None
    confidence: float = 0.95
    probabilities: Mapping[str, float] | None = None
    target_confidence: float | None = None
    target_probabilities: Mapping[str, float] | None = None
    scroll_direction: str | None = None
    navigate_url: str | None = None
    done_message: str | None = None


class FakeJevClient(JevClient):
    """Deterministic Jev client for unit tests and offline loops.

    Consumes a script of :class:`FakeDecision` values in order. When the script
    is exhausted (or empty), returns ``DONE`` with high confidence. Ignores the
    live network; validates that requested question names exist when answering
    target heads.
    """

    def __init__(
        self,
        script: Sequence[FakeDecision] | None = None,
        *,
        model: str = "jev-fake",
    ) -> None:
        """Create a fake client.

        Args:
            script: Ordered decisions to return from successive ``decide`` calls.
            model: Model string stamped on responses.
        """
        self._script = list(script or ())
        self._cursor = 0
        self.model = model
        self.calls: list[JevRequest] = []

    def reset(self) -> None:
        """Rewind the script cursor and clear recorded requests."""
        self._cursor = 0
        self.calls.clear()

    def decide(self, request: JevRequest) -> JevResponse:
        """Return the next scripted decision as a Jev-shaped response.

        Args:
            request: Adapter request (recorded for assertions).

        Returns:
            Synthetic :class:`JevResponse`.
        """
        self.calls.append(request)
        if self._cursor < len(self._script):
            step = self._script[self._cursor]
            self._cursor += 1
        else:
            step = FakeDecision(operation=ActionKind.DONE.value, confidence=0.99)

        return self._build_response(request, step)

    def _build_response(self, request: JevRequest, step: FakeDecision) -> JevResponse:
        """Map a scripted step onto the request's question names.

        Args:
            request: Original request (for criteria keys).
            step: Scripted decision.

        Returns:
            Synthetic response with choice answers.
        """
        answers: dict[str, Any] = {}
        op_probs = dict(step.probabilities or {})
        if not op_probs and "operation" in request.questions:
            op_q = request.questions["operation"]
            if isinstance(op_q, JevChoiceQuestion):
                keys = list(op_q.criteria)
                op_probs = {k: (step.confidence if k == step.operation else 0.0) for k in keys}
                # Leave a little residual mass on DONE when present and not chosen.
                if step.operation in op_probs:
                    residual = max(0.0, 1.0 - step.confidence)
                    others = [k for k in keys if k != step.operation]
                    if others and residual > 0:
                        share = residual / len(others)
                        for k in others:
                            op_probs[k] = share
                    op_probs[step.operation] = step.confidence

        answers["operation"] = {
            "type": "choice",
            "choice": step.operation,
            "confidence": step.confidence,
            "probabilities": op_probs,
        }

        target_question = _target_question_for_operation(step.operation)
        if target_question and target_question in request.questions and step.target_key is not None:
            t_conf = (
                step.target_confidence if step.target_confidence is not None else step.confidence
            )
            t_probs = dict(step.target_probabilities or {})
            t_q = request.questions[target_question]
            if not t_probs and isinstance(t_q, JevChoiceQuestion):
                t_probs = {k: (t_conf if k == step.target_key else 0.0) for k in t_q.criteria}
            answers[target_question] = {
                "type": "choice",
                "choice": step.target_key,
                "confidence": t_conf,
                "probabilities": t_probs,
            }

        if step.operation == ActionKind.SCROLL.value and "scroll_direction" in request.questions:
            direction = step.scroll_direction or "DOWN"
            answers["scroll_direction"] = {
                "type": "choice",
                "choice": direction,
                "confidence": step.confidence,
                "probabilities": {direction: step.confidence},
            }

        if step.operation == ActionKind.NAVIGATE.value and "navigate_url" in request.questions:
            url_key = step.navigate_url or step.target_key
            if url_key is not None:
                answers["navigate_url"] = {
                    "type": "choice",
                    "choice": url_key,
                    "confidence": step.confidence,
                    "probabilities": {url_key: step.confidence},
                }

        if step.done_message:
            answers["done_meta"] = {
                "type": "choice",
                "choice": step.done_message,
                "confidence": step.confidence,
                "probabilities": {step.done_message: 1.0},
            }

        return JevResponse(model=self.model, answers=answers, usage=JevUsage(input_tokens=0))


class HttpJevClient(JevClient):
    """Live HTTP client against the assumed Jev Decision API.

    Uses ``niquests`` when available; raises :class:`JevClientNotConfiguredError`
    when no API key is configured. Kept thin so T015/T032 can swap endpoints.
    """

    def __init__(self, settings: JevClientSettings | None = None) -> None:
        """Create an HTTP client.

        Args:
            settings: Optional settings; loads from the environment when omitted.
        """
        self.settings = settings if settings is not None else load_jev_client_settings()

    def decide(self, request: JevRequest) -> JevResponse:
        """POST the request to the configured Jev endpoint.

        Args:
            request: Adapter-built request.

        Returns:
            Parsed response.

        Raises:
            JevClientNotConfiguredError: When ``api_key`` is missing.
            JevClientError: On HTTP or parse failures.
        """
        if not self.settings.api_key:
            raise JevClientNotConfiguredError(
                "JEV_API_KEY / JEV_API_KEY_FILE is not configured",
            )
        try:
            import niquests
        except ImportError as exc:  # pragma: no cover - optional until dependency added
            raise JevClientError(
                "niquests is required for HttpJevClient; use FakeJevClient in tests",
            ) from exc

        payload = request.model_dump(mode="json")
        if not payload.get("model"):
            payload["model"] = self.settings.model

        url = f"{self.settings.base_url}/v1/decide"
        try:
            response = niquests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise JevClientError(f"Jev HTTP call failed: {exc}") from exc

        try:
            return JevResponse.model_validate(data)
        except Exception as exc:
            raise JevClientError(f"Jev response parse failed: {exc}") from exc


_SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "cookies",
        "authorization",
        "cvv",
        "cvc",
        "pan",
        "card_number",
        "api_key",
        "apikey",
        "private_key",
        "master_password",
        "bw_password",
    }
)


def _assert_no_secret_keys(value: Any, *, path: str) -> None:
    """Recursively reject mappings whose keys look like secret field names.

    Structural flags such as ``is_password_field`` are allowed; only exact
    deny-listed key names (e.g. ``password``, ``cookie``) fail closed.

    Args:
        value: Nested JSON-like structure.
        path: Dotted path for error messages.

    Raises:
        ValueError: When a deny-listed key is present.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_str = str(key).lower().replace("-", "_").replace(" ", "_")
            if key_str in _SECRET_KEY_NAMES:
                raise ValueError(
                    f"JevRequest must not include secret field {path}.{key!s}",
                )
            _assert_no_secret_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, child in enumerate(value):
            _assert_no_secret_keys(child, path=f"{path}[{i}]")


def _target_question_for_operation(operation: str) -> str | None:
    """Return the speculative target question name for an operation.

    Args:
        operation: Action kind string.

    Returns:
        Question name, or ``None`` when the op has no element target.
    """
    mapping = {
        ActionKind.CLICK.value: "click_target",
        ActionKind.TYPE_TEXT.value: "type_target",
        ActionKind.BITWARDEN_LOGIN.value: "bitwarden_target",
        ActionKind.BITWARDEN_IDENTITY.value: "bitwarden_target",
        ActionKind.BITWARDEN_CARD.value: "bitwarden_target",
    }
    return mapping.get(operation)
