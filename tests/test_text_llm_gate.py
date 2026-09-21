"""Tests for the TYPE_TEXT-only small text LLM gate (T016)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.policy.actions import (
    ActionKind,
    ActionParams,
    AgentAction,
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.policy.text_llm import (
    FakeTextLLMClient,
    OpenAICompatibleTextLLMClient,
    TextLLMError,
    TextLLMSettings,
    build_type_text_prompt,
    load_text_llm_settings,
    maybe_fill_type_text,
)
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import REDACTED, redact_for_audit


@dataclass
class _RecordedEvent:
    """One audit event captured by :class:`RecordingAuditWriter`."""

    run_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    actor: str
    step_id: uuid.UUID | None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class RecordingAuditWriter:
    """In-memory audit sink that always redacts payloads."""

    def __init__(self) -> None:
        """Create an empty recording writer."""
        self.events: list[_RecordedEvent] = []

    def append(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        step_id: uuid.UUID | None = None,
        parent_event_id: uuid.UUID | None = None,
        url: str | None = None,
        tab_id: str | None = None,
        duration_ms: int | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> _RecordedEvent:
        """Append a redacted event.

        Args:
            run_id: Owning run.
            event_type: Logical event kind.
            payload: Structured metadata.
            actor: Causing actor.
            step_id: Optional step grouping.
            parent_event_id: Unused (API parity).
            url: Unused (API parity).
            tab_id: Unused (API parity).
            duration_ms: Unused (API parity).
            occurred_at: Unused (API parity).
            event_id: Optional event id.

        Returns:
            Recorded event handle.
        """
        del parent_event_id, url, tab_id, duration_ms, occurred_at
        raw = dict(payload or {})
        redacted = redact_for_audit(raw)
        if not isinstance(redacted, dict):
            redacted = {"value": redacted}
        event = _RecordedEvent(
            run_id=run_id,
            event_type=event_type,
            payload=redacted,
            actor=actor,
            step_id=step_id,
            id=event_id or uuid.uuid4(),
        )
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        """Return event_type values in append order."""
        return [e.event_type for e in self.events]


def _form_obs() -> BrowserObservation:
    """Return a form observation with an editable search field."""
    return BrowserObservation(
        url="https://example.com/search",
        title="Search",
        candidates=[
            CandidateElement(index=1, tag="button", name="Go"),
            CandidateElement(index=2, tag="input", name="q", is_editable=True),
            CandidateElement(index=3, tag="a", name="Help", href="/help"),
        ],
        page_summary="Site search form",
    )


async def test_non_type_text_never_calls_text_llm() -> None:
    """CLICK / DONE never invoke the text LLM client."""
    client = FakeTextLLMClient(texts=("should-not-appear",))
    action = AgentAction(kind=ActionKind.CLICK, target_index=1)
    result = await maybe_fill_type_text(
        action,
        goal="click submit",
        observation=_form_obs(),
        client=client,
    )
    assert result is None
    assert client.calls == []
    assert action.params.text is None


async def test_type_text_with_existing_text_skips_llm() -> None:
    """Pre-filled TYPE_TEXT params skip the LLM."""
    client = FakeTextLLMClient(texts=("ignored",))
    action = AgentAction(
        kind=ActionKind.TYPE_TEXT,
        target_index=2,
        params=ActionParams(text="already-set"),
    )
    result = await maybe_fill_type_text(
        action,
        goal="type query",
        observation=_form_obs(),
        client=client,
    )
    assert result is None
    assert client.calls == []
    assert action.params.text == "already-set"


async def test_fake_client_fills_type_text() -> None:
    """Fake client proposes text and mutates the action."""
    client = FakeTextLLMClient(texts=("invoice March 2026",))
    action = AgentAction(kind=ActionKind.TYPE_TEXT, target_index=2)
    result = await maybe_fill_type_text(
        action,
        goal="Enter the invoice month",
        observation=_form_obs(),
        client=client,
    )
    assert result is not None
    assert result.text == "invoice March 2026"
    assert action.params.text == "invoice March 2026"
    assert len(client.calls) == 1
    assert "Goal:" in client.calls[0].user


def test_prompts_are_redacted() -> None:
    """Prompt metadata redacts secret-looking goal content."""
    obs = BrowserObservation(
        url="https://example.com/login",
        title="Login",
        candidates=[
            CandidateElement(index=0, tag="input", name="email", is_editable=True),
        ],
    )
    prompt = build_type_text_prompt(
        goal="Use password=hunter2 in the email field label only as context",
        observation=obs,
        target=obs.candidates[0],
    )
    blob = str(prompt.meta) + prompt.user + prompt.system
    assert "hunter2" not in blob
    assert REDACTED in str(prompt.meta.get("goal", "")) or REDACTED in prompt.user


async def test_password_field_rejected() -> None:
    """Password fields must not go through the text LLM."""
    obs = BrowserObservation(
        url="https://example.com/login",
        title="Login",
        candidates=[
            CandidateElement(
                index=4,
                tag="input",
                name="Password",
                is_password_field=True,
                is_editable=True,
            ),
        ],
    )
    action = AgentAction(kind=ActionKind.TYPE_TEXT, target_index=4)
    with pytest.raises(TextLLMError, match="Bitwarden"):
        await maybe_fill_type_text(
            action,
            goal="log in",
            observation=obs,
            client=FakeTextLLMClient(),
        )


async def test_missing_client_fails_closed() -> None:
    """TYPE_TEXT without a client and without text raises."""
    action = AgentAction(kind=ActionKind.TYPE_TEXT, target_index=2)
    with pytest.raises(TextLLMError, match="not configured"):
        await maybe_fill_type_text(
            action,
            goal="type",
            observation=_form_obs(),
            client=None,
        )


async def test_agent_loop_type_text_uses_fake_llm() -> None:
    """Loop fills TYPE_TEXT via FakeTextLLMClient and audits the model call."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_form_obs(), _form_obs()])
        jev = FakeJevClient(
            [
                FakeDecision(operation="TYPE_TEXT", target_key="2", confidence=0.9),
                FakeDecision(operation="DONE", done_message="ok", confidence=0.99),
            ],
        )
        text_llm = FakeTextLLMClient(texts=("summer sale",))

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Search for summer sale",
            browser=browser,
            jev=jev,
            text_llm=text_llm,
            audit=audit,
        ).run()

        assert outcome.status == RunStatus.SUCCEEDED
        assert "model_call" in audit.types()
        assert len(text_llm.calls) == 1
        typed = browser.executed[0]
        assert typed.kind == ActionKind.TYPE_TEXT
        assert typed.params.text == "summer sale"
        model_events = [e for e in audit.events if e.event_type == "model_call"]
        assert model_events[0].payload.get("call_kind") == "text_llm"
        # Ensure non-TYPE_TEXT path did not add extra LLM calls on DONE.
        assert len(text_llm.calls) == 1

    await _run()


async def test_agent_loop_click_never_calls_text_llm() -> None:
    """CLICK decisions leave the text LLM untouched."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_form_obs()])
        jev = FakeJevClient(
            [FakeDecision(operation="CLICK", target_key="1", confidence=0.95)],
        )
        # After click, loop continues; script a DONE on second observe.
        browser.observations.append(
            BrowserObservation(url="https://example.com/done", title="Done"),
        )
        jev = FakeJevClient(
            [
                FakeDecision(operation="CLICK", target_key="1", confidence=0.95),
                FakeDecision(operation="DONE", done_message="done", confidence=0.99),
            ],
        )
        text_llm = FakeTextLLMClient(texts=("nope",))

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Click Go",
            browser=browser,
            jev=jev,
            text_llm=text_llm,
            audit=audit,
            max_steps=5,
        ).run()

        assert outcome.status == RunStatus.SUCCEEDED
        assert text_llm.calls == []
        assert "model_call" not in audit.types()

    await _run()


async def test_agent_loop_text_llm_failure_audited() -> None:
    """Text LLM failures surface as run errors with model_call_failed audit."""

    async def _run() -> None:
        run_id = uuid.uuid4()
        audit = RecordingAuditWriter()
        browser = FakeBrowserPort([_form_obs()])
        jev = FakeJevClient(
            [FakeDecision(operation="TYPE_TEXT", target_key="2", confidence=0.9)],
        )
        text_llm = FakeTextLLMClient(fail_with="upstream timeout")

        outcome = await AgentLoop(
            run_id=run_id,
            goal="Type into search",
            browser=browser,
            jev=jev,
            text_llm=text_llm,
            audit=audit,
        ).run()

        assert outcome.status == RunStatus.FAILED
        assert "upstream timeout" in (outcome.message or "")
        assert "model_call_failed" in audit.types()
        assert "run_failed" in audit.types()
        assert browser.executed == []

    await _run()


def test_load_text_llm_settings_from_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """API key is loaded only from TEXT_LLM_API_KEY_FILE."""
    secret = tmp_path / "key"
    secret.write_text("sk-test-from-file\n", encoding="utf-8")
    monkeypatch.delenv("TEXT_LLM_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_LLM_API_KEY_FILE", str(secret))
    monkeypatch.setenv("TEXT_LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.setenv("TEXT_LLM_MODEL", "tiny-form")
    monkeypatch.setenv("TEXT_LLM_MAX_TOKENS", "32")

    settings = load_text_llm_settings()
    assert settings.api_key == "sk-test-from-file"
    assert settings.base_url == "http://llm.local/v1"
    assert settings.model == "tiny-form"
    assert settings.max_tokens == 32


def test_load_text_llm_settings_ignores_bare_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare text-LLM API key is not a supported credential source."""
    monkeypatch.setenv("TEXT_LLM_API_KEY", "ignored-env-key")
    monkeypatch.delenv("TEXT_LLM_API_KEY_FILE", raising=False)
    assert load_text_llm_settings().api_key is None


async def test_openai_compatible_client_posts_chat_completions() -> None:
    """Live adapter posts to /chat/completions via niquests (mocked)."""
    settings = TextLLMSettings(
        base_url="http://llm.test/v1",
        api_key="sk-test",
        model="tiny",
        max_tokens=16,
    )
    client = OpenAICompatibleTextLLMClient(settings)
    prompt = build_type_text_prompt(
        goal="search cats",
        observation=_form_obs(),
        target=_form_obs().candidates[1],
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "model": "tiny",
        "choices": [{"message": {"content": "  cats  "}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }

    with patch("niquests.apost", new_callable=AsyncMock) as post:
        post.return_value = mock_response
        result = await client.complete_type_text(prompt)

    assert result.text == "cats"
    assert result.prompt_tokens == 10
    post.assert_awaited_once()
    args, kwargs = post.await_args.args, post.await_args.kwargs
    assert args[0] == "http://llm.test/v1/chat/completions"
    assert kwargs["json"]["max_tokens"] == 16
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"


async def test_openai_compatible_requires_api_key() -> None:
    """Missing API key fails closed before HTTP."""
    client = OpenAICompatibleTextLLMClient(
        TextLLMSettings(api_key=None, base_url="http://llm.test/v1"),
    )
    prompt = build_type_text_prompt(
        goal="x",
        observation=_form_obs(),
        target=_form_obs().candidates[1],
    )
    with pytest.raises(TextLLMError, match="TEXT_LLM_API_KEY"):
        await client.complete_type_text(prompt)
