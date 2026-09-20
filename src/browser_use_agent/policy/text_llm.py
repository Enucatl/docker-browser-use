"""Small generative text LLM gate for ``TYPE_TEXT`` only.

Jev still chooses that typing is needed and which field. This module proposes
the string to type (search query, invoice month, etc.). Non-``TYPE_TEXT``
actions must never invoke the client. Prompts are redacted; secrets stay out.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from browser_use_agent.db.settings import read_env_or_file
from browser_use_agent.policy.actions import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.security.redaction import redact_for_audit, redact_text

DEFAULT_MAX_TOKENS = 64
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class TextLLMError(RuntimeError):
    """Raised when the text LLM gate cannot produce typeable text."""


class TextLLMNotConfiguredError(TextLLMError):
    """Raised when a live client is used without an API key."""


@dataclass(frozen=True, slots=True)
class TextLLMSettings:
    """Config for an OpenAI-compatible chat-completions client.

    Attributes:
        base_url: API root without trailing slash (``…/v1``).
        api_key: Bearer token; never log this value.
        model: Chat model id.
        max_tokens: Completion budget (form-filling, keep small).
        timeout_seconds: HTTP timeout.
        temperature: Sampling temperature (prefer 0 for deterministic fills).
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    temperature: float = 0.0


def load_text_llm_settings() -> TextLLMSettings:
    """Load text-LLM settings from the environment.

    Reads ``TEXT_LLM_BASE_URL``, ``TEXT_LLM_MODEL``, ``TEXT_LLM_MAX_TOKENS``,
    ``TEXT_LLM_TIMEOUT_SECONDS``, ``TEXT_LLM_TEMPERATURE``, and
    ``TEXT_LLM_API_KEY`` or ``TEXT_LLM_API_KEY_FILE``.

    Returns:
        Immutable settings snapshot (key may be ``None``).
    """
    api_key = read_env_or_file("TEXT_LLM_API_KEY")
    if api_key is not None:
        api_key = api_key.strip() or None

    max_tokens_raw = os.environ.get("TEXT_LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)).strip()
    timeout_raw = os.environ.get(
        "TEXT_LLM_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    ).strip()
    temp_raw = os.environ.get("TEXT_LLM_TEMPERATURE", "0").strip()
    try:
        max_tokens = max(1, int(max_tokens_raw))
    except ValueError:
        max_tokens = DEFAULT_MAX_TOKENS
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    try:
        temperature = float(temp_raw)
    except ValueError:
        temperature = 0.0

    return TextLLMSettings(
        base_url=os.environ.get("TEXT_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        api_key=api_key,
        model=os.environ.get("TEXT_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
    )


@dataclass(frozen=True, slots=True)
class TextLLMPrompt:
    """Redacted prompt payload for a ``TYPE_TEXT`` fill.

    Attributes:
        system: System instructions (no secrets).
        user: User message with goal / field context.
        messages: OpenAI-compatible chat messages list.
        meta: Structured, redacted metadata for audit traces.
    """

    system: str
    user: str
    messages: list[dict[str, str]]
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TextLLMResult:
    """Outcome of one text-LLM fill attempt.

    Attributes:
        text: Proposed string to type (empty when failed).
        model: Model id used.
        latency_ms: End-to-end latency.
        prompt_tokens: Optional usage input tokens.
        completion_tokens: Optional usage output tokens.
        request_meta: Redacted request metadata for audit.
        response_meta: Redacted response metadata for audit.
        raw_content: Raw model content before cleanup (redacted for audit).
    """

    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    request_meta: dict[str, Any] = field(default_factory=dict)
    response_meta: dict[str, Any] = field(default_factory=dict)
    raw_content: str | None = None


class TextLLMClient(ABC):
    """Abstract small-text generative client used only for ``TYPE_TEXT``."""

    @abstractmethod
    def complete_type_text(self, prompt: TextLLMPrompt) -> TextLLMResult:
        """Propose text to type given a redacted prompt.

        Args:
            prompt: Built by :func:`build_type_text_prompt`.

        Returns:
            Fill result including audit metadata.

        Raises:
            TextLLMError: On HTTP, parse, or empty-output failures.
        """


@dataclass
class FakeTextLLMClient(TextLLMClient):
    """Scripted client for unit tests.

    Attributes:
        texts: Ordered strings to return; last value repeats when exhausted.
        calls: Recorded prompts from each invocation.
        fail_with: When set, every call raises :class:`TextLLMError` with this.
        model: Model name reported in results.
    """

    texts: Sequence[str] = ("typed-by-fake",)
    calls: list[TextLLMPrompt] = field(default_factory=list)
    fail_with: str | None = None
    model: str = "fake-text-llm"

    def complete_type_text(self, prompt: TextLLMPrompt) -> TextLLMResult:
        """Return the next scripted string (or raise).

        Args:
            prompt: Redacted type-text prompt.

        Returns:
            Synthetic :class:`TextLLMResult`.

        Raises:
            TextLLMError: When ``fail_with`` is set or no texts remain empty.
        """
        self.calls.append(prompt)
        if self.fail_with is not None:
            raise TextLLMError(self.fail_with)
        index = min(len(self.calls) - 1, len(self.texts) - 1)
        text = self.texts[index] if self.texts else ""
        if not text:
            raise TextLLMError("FakeTextLLMClient returned empty text")
        request_meta = _redact_mapping(
            {
                "call_kind": "text_llm",
                "provider": "fake",
                "model": self.model,
                "messages": prompt.messages,
                "prompt_meta": prompt.meta,
            }
        )
        response_meta = _redact_mapping(
            {
                "call_kind": "text_llm",
                "provider": "fake",
                "model": self.model,
                "content": text,
            }
        )
        return TextLLMResult(
            text=text,
            model=self.model,
            latency_ms=0,
            prompt_tokens=0,
            completion_tokens=len(text.split()),
            request_meta=request_meta,
            response_meta=response_meta,
            raw_content=text,
        )


class OpenAICompatibleTextLLMClient(TextLLMClient):
    """Sync OpenAI-compatible ``/chat/completions`` client via ``niquests``.

    Homelab note: endpoints such as OpenRouter or a local ``shared-inference``
    peer speak the same wire shape. This adapter stays thin and does not import
    ``shared_inference``.
    """

    def __init__(self, settings: TextLLMSettings | None = None) -> None:
        """Create a live HTTP client.

        Args:
            settings: Optional settings; loads from the environment when omitted.
        """
        self.settings = settings if settings is not None else load_text_llm_settings()

    def complete_type_text(self, prompt: TextLLMPrompt) -> TextLLMResult:
        """POST a chat completion and return cleaned typeable text.

        Args:
            prompt: Redacted type-text prompt.

        Returns:
            Fill result with usage and audit metadata.

        Raises:
            TextLLMNotConfiguredError: When ``api_key`` is missing.
            TextLLMError: On HTTP, parse, or empty-output failures.
        """
        if not self.settings.api_key:
            raise TextLLMNotConfiguredError(
                "TEXT_LLM_API_KEY / TEXT_LLM_API_KEY_FILE is not configured",
            )
        try:
            import niquests
        except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
            raise TextLLMError(
                "niquests is required for OpenAICompatibleTextLLMClient; "
                "use FakeTextLLMClient in tests",
            ) from exc

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": prompt.messages,
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
        }
        url = f"{self.settings.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        request_meta = _redact_mapping(
            {
                "call_kind": "text_llm",
                "provider": "openai-compatible",
                "model": self.settings.model,
                "url": url,
                "max_tokens": self.settings.max_tokens,
                "temperature": self.settings.temperature,
                "messages": prompt.messages,
                "prompt_meta": prompt.meta,
            }
        )

        started = time.perf_counter()
        try:
            response = niquests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except TextLLMError:
            raise
        except Exception as exc:
            raise TextLLMError(f"Text LLM HTTP call failed: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            content = _extract_chat_content(data)
        except TextLLMError:
            raise
        except Exception as exc:
            raise TextLLMError(f"Text LLM response parse failed: {exc}") from exc

        cleaned = _clean_type_text(content)
        if not cleaned:
            raise TextLLMError("Text LLM returned empty typeable text")

        usage = data.get("usage") if isinstance(data, Mapping) else None
        prompt_tokens = None
        completion_tokens = None
        if isinstance(usage, Mapping):
            prompt_tokens = _optional_int(usage.get("prompt_tokens"))
            completion_tokens = _optional_int(usage.get("completion_tokens"))

        response_meta = _redact_mapping(
            {
                "call_kind": "text_llm",
                "provider": "openai-compatible",
                "model": data.get("model", self.settings.model)
                if isinstance(data, Mapping)
                else self.settings.model,
                "content": cleaned,
                "raw_content": content,
                "usage": usage if isinstance(usage, Mapping) else {},
                "id": data.get("id") if isinstance(data, Mapping) else None,
            }
        )
        model_name = (
            str(data.get("model") or self.settings.model)
            if isinstance(data, Mapping)
            else self.settings.model
        )
        return TextLLMResult(
            text=cleaned,
            model=model_name,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            request_meta=request_meta,
            response_meta=response_meta,
            raw_content=content,
        )


def build_type_text_prompt(
    *,
    goal: str,
    observation: BrowserObservation,
    target: CandidateElement | None,
    constraints: Sequence[str] | None = None,
) -> TextLLMPrompt:
    """Build a redacted chat prompt for filling one form field.

    Args:
        goal: Operator natural-language goal.
        observation: Current browser observation (for nearby labels / URL).
        target: Selected editable candidate, if resolved.
        constraints: Extra operator/system constraints.

    Returns:
        Prompt with messages and audit metadata (already redacted).

    Raises:
        TextLLMError: When the target is a password field (Bitwarden only).
    """
    if target is not None and target.is_password_field:
        raise TextLLMError(
            "TYPE_TEXT on password fields must use Bitwarden actions (T027), not the text LLM",
        )

    safe_goal = redact_text(goal) or ""
    field_context = {
        "index": target.index if target else None,
        "tag": target.tag if target else None,
        "role": target.role if target else None,
        "name": target.name if target else None,
        "input_type": target.input_type if target else None,
        "is_editable": target.is_editable if target else None,
    }
    nearby = _nearby_labels(observation, target.index if target else None)
    constraint_list = [
        *(constraints or ()),
        "Reply with only the text to type into the field.",
        "Do not include quotes, labels, or explanation.",
        "Never invent passwords, API keys, cookies, or payment card numbers.",
        "Keep the answer short (a few words or one line).",
    ]
    meta = _redact_mapping(
        {
            "goal": safe_goal,
            "url": observation.url,
            "title": observation.title,
            "field": field_context,
            "nearby_labels": nearby,
            "constraints": constraint_list,
            "page_summary": observation.page_summary,
        }
    )

    system = (
        "You fill a single web form field for a browser agent. "
        "Output only the value to type. No secrets."
    )
    field_meta = meta.get("field")
    field_for_prompt = field_meta if isinstance(field_meta, Mapping) else field_context
    user_parts = [
        f"Goal: {meta.get('goal', safe_goal)}",
        f"Page URL: {meta.get('url') or observation.url or '(unknown)'}",
        f"Page title: {meta.get('title') or observation.title or '(unknown)'}",
        f"Target field: {_format_field(field_for_prompt)}",
        f"Nearby labels: {', '.join(nearby) if nearby else '(none)'}",
        "Constraints:",
        *[f"- {c}" for c in constraint_list],
    ]
    if meta.get("page_summary"):
        user_parts.insert(3, f"Page summary: {meta['page_summary']}")
    user = "\n".join(user_parts)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return TextLLMPrompt(system=system, user=user, messages=messages, meta=meta)


def maybe_fill_type_text(
    action: AgentAction,
    *,
    goal: str,
    observation: BrowserObservation,
    client: TextLLMClient | None,
) -> TextLLMResult | None:
    """Fill ``TYPE_TEXT`` params via the text LLM when needed.

    Non-``TYPE_TEXT`` actions return ``None`` without calling the client.
    Actions that already carry ``params.text`` are left untouched.

    Args:
        action: Decision action (mutated in place when filled).
        goal: Operator goal.
        observation: Observation used for field / nearby context.
        client: Text LLM client; ``None`` means fail closed for empty text.

    Returns:
        :class:`TextLLMResult` when the LLM was invoked; otherwise ``None``.

    Raises:
        TextLLMError: When ``TYPE_TEXT`` needs text but the client is missing
            or the completion fails / returns empty.
    """
    if action.kind != ActionKind.TYPE_TEXT:
        return None
    if action.params.text:
        return None
    if client is None:
        raise TextLLMError(
            "TYPE_TEXT requires text from the text LLM gate (TEXT_LLM client not configured)",
        )

    target: CandidateElement | None = None
    if action.target_index is not None:
        target = observation.candidate_by_index(action.target_index)

    prompt = build_type_text_prompt(goal=goal, observation=observation, target=target)
    result = client.complete_type_text(prompt)
    if not result.text:
        raise TextLLMError("Text LLM returned empty typeable text")
    action.params.text = result.text
    return result


def _nearby_labels(
    observation: BrowserObservation,
    target_index: int | None,
    *,
    window: int = 3,
) -> list[str]:
    """Collect short non-secret labels near the target candidate.

    Args:
        observation: Current page observation.
        target_index: Selected element index, if any.
        window: How many candidates before/after to include by list order.

    Returns:
        Deduplicated label strings (already length-capped).
    """
    labels: list[str] = []
    if not observation.candidates:
        return labels
    if target_index is None:
        for candidate in observation.candidates[:8]:
            label = candidate.criteria_label()
            if label and label not in labels:
                labels.append(label)
        return labels

    ordered = list(observation.candidates)
    try:
        pos = next(i for i, c in enumerate(ordered) if c.index == target_index)
    except StopIteration:
        pos = 0
    start = max(0, pos - window)
    end = min(len(ordered), pos + window + 1)
    for candidate in ordered[start:end]:
        if candidate.index == target_index:
            continue
        label = candidate.criteria_label()
        if label and label not in labels:
            labels.append(label)
    return labels


def _format_field(field: Mapping[str, Any] | dict[str, Any]) -> str:
    """Format field context for the user prompt.

    Args:
        field: Redacted field metadata.

    Returns:
        Single-line description.
    """
    parts: list[str] = []
    index = field.get("index")
    if index is not None:
        parts.append(f"index={index}")
    for key in ("tag", "role", "name", "input_type"):
        value = field.get(key)
        if value:
            parts.append(f"{key}={value}")
    if field.get("is_editable"):
        parts.append("editable")
    return ", ".join(parts) if parts else "(unknown field)"


def _extract_chat_content(data: Any) -> str:
    """Pull assistant content from an OpenAI-compatible completion body.

    Args:
        data: Parsed JSON response.

    Returns:
        Raw content string.

    Raises:
        TextLLMError: When the shape is unexpected or content is missing.
    """
    if not isinstance(data, Mapping):
        raise TextLLMError("Text LLM response was not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TextLLMError("Text LLM response missing choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise TextLLMError("Text LLM choice was not an object")
    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Multimodal content parts — concatenate text segments.
            bits: list[str] = []
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    bits.append(part["text"])
                elif isinstance(part, str):
                    bits.append(part)
            if bits:
                return "".join(bits)
    text = first.get("text")
    if isinstance(text, str):
        return text
    raise TextLLMError("Text LLM response missing message content")


def _clean_type_text(raw: str) -> str:
    """Normalize model output into a single typeable string.

    Args:
        raw: Raw model content.

    Returns:
        Stripped text without wrapping quotes; empty when nothing usable.
    """
    text = raw.strip()
    if not text:
        return ""
    # Prefer the first non-empty line (models sometimes add a trailing note).
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            text = candidate
            break
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "`"}:
        text = text[1:-1].strip()
    # Hard cap so a runaway completion cannot dump an essay into the page.
    if len(text) > 500:
        text = text[:500].rstrip()
    return text


def _redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run :func:`redact_for_audit` and coerce to a dict.

    Args:
        payload: Nested metadata.

    Returns:
        Redacted dictionary suitable for audit events.
    """
    redacted = redact_for_audit(dict(payload))
    if isinstance(redacted, dict):
        return redacted
    return {"value": redacted}


def _optional_int(value: Any) -> int | None:
    """Coerce a value to ``int`` when possible.

    Args:
        value: Raw usage field.

    Returns:
        Integer or ``None``.
    """
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None
