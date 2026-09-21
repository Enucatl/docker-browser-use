"""Unit tests for Jev action space and Browser Use ↔ Jev adapter (T014)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from browser_use_agent.policy import (
    ActionKind,
    AgentAction,
    BrowserObservation,
    CandidateElement,
    FakeDecision,
    FakeJevClient,
    HttpJevClient,
    JevAdapter,
    JevAdapterError,
    JevClientNotConfiguredError,
    JevClientSettings,
    JevRequest,
    JevResponse,
    ScrollDirection,
    load_jev_client_settings,
    observation_from_browser_state,
)
from browser_use_agent.security import REDACTED


def _sample_observation(*, empty: bool = False) -> BrowserObservation:
    """Build a small observation for adapter tests."""
    if empty:
        return BrowserObservation(
            url="https://example.test/",
            title="Empty",
            candidates=[],
            suggested_urls=["https://example.test/home"],
        )
    return BrowserObservation(
        url="https://example.test/login",
        title="Sign in",
        candidates=[
            CandidateElement(
                index=0,
                tag="a",
                role="link",
                name="Home",
                href="https://example.test/",
            ),
            CandidateElement(
                index=2,
                tag="button",
                role="button",
                name="Submit",
            ),
            CandidateElement(
                index=5,
                tag="input",
                name="Email",
                input_type="email",
                is_editable=True,
            ),
            CandidateElement(
                index=6,
                tag="input",
                name="password",
                input_type="password",
                is_editable=True,
                is_password_field=True,
            ),
        ],
        pixels_below=400,
        suggested_urls=["https://example.test/app"],
    )


def test_jev_api_key_is_file_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jev ignores a bare environment API key."""
    secret = tmp_path / "key"
    secret.write_text("jv-test-from-file\n", encoding="utf-8")
    monkeypatch.setenv("JEV_API_KEY", "ignored-env-key")
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    assert load_jev_client_settings().api_key is None

    monkeypatch.setenv("JEV_API_KEY_FILE", str(secret))
    assert load_jev_client_settings().api_key == "jv-test-from-file"


def test_action_space_encoded_once() -> None:
    """Core and Bitwarden kinds are declared exactly once on ActionKind."""
    assert ActionKind.CLICK.value == "CLICK"
    assert ActionKind.DONE.value == "DONE"
    assert ActionKind.BITWARDEN_LOGIN.value == "BITWARDEN_LOGIN"
    assert {k.value for k in ActionKind} == {
        "CLICK",
        "TYPE_TEXT",
        "SCROLL",
        "GO_BACK",
        "NAVIGATE",
        "SWITCH_TAB",
        "DONE",
        "BITWARDEN_LOGIN",
        "BITWARDEN_IDENTITY",
        "BITWARDEN_CARD",
    }


def test_to_jev_request_never_includes_password_values() -> None:
    """Adapter state lists password fields as controls, never values."""
    adapter = JevAdapter(model="jev-test")
    obs = _sample_observation()
    # Poison the observation name with a secret-shaped value — still no password key.
    obs.candidates[3] = CandidateElement(
        index=6,
        tag="input",
        name="••••",
        input_type="password",
        is_editable=True,
        is_password_field=True,
    )
    req = adapter.to_jev_request(obs, goal="Log in without leaking secrets", history_summary="")
    dumped = req.model_dump(mode="json")
    assert "password" not in _all_keys(dumped["state"])
    blob = str(dumped).lower()
    assert "hunter2" not in blob
    assert "cvv" not in blob
    # Password field is advertised safely.
    pw = next(c for c in dumped["state"]["candidates"] if c["index"] == 6)
    assert pw["is_password_field"] is True
    assert pw["input_type"] == "password"
    assert "operation" in req.questions
    assert "click_target" in req.questions
    assert "type_target" in req.questions


def test_jev_request_rejects_secret_keys_in_state() -> None:
    """JevRequest validation fails closed on secret field names."""
    with pytest.raises(ValueError, match="secret field"):
        JevRequest(
            state={"goal": "x", "password": "nope"},
            questions={
                "operation": {
                    "type": "choice",
                    "instructions": "pick",
                    "criteria": {"DONE": "done"},
                }
            },
        )


async def test_empty_candidates_drops_targeted_ops_and_fake_done() -> None:
    """Empty candidates omit CLICK/TYPE; fake client returns DONE when script ends."""
    adapter = JevAdapter()
    obs = _sample_observation(empty=True)
    req = adapter.to_jev_request(obs, goal="Nothing left to do")
    op_criteria = req.questions["operation"].criteria
    assert "CLICK" not in op_criteria
    assert "TYPE_TEXT" not in op_criteria
    assert "DONE" in op_criteria
    assert "click_target" not in req.questions

    client = FakeJevClient(script=[])
    resp = await client.decide(req)
    action = adapter.from_jev_response(resp, observation=obs)
    assert action.kind == ActionKind.DONE
    assert action.confidence == pytest.approx(0.99)
    assert action.probabilities


def test_goal_urls_become_navigation_choices() -> None:
    """Bare and explicit URLs in a goal are available from the initial blank page."""
    adapter = JevAdapter()
    obs = BrowserObservation(url="about:blank", candidates=[])

    req = adapter.to_jev_request(
        obs,
        goal="Compare ansa.it/news with https://www.corriere.it/ today.",
    )

    assert req.state["goal_urls"] == [
        "https://ansa.it/news",
        "https://www.corriere.it/",
    ]
    assert req.questions["navigate_url"].criteria == {
        "https://ansa.it/news": "https://ansa.it/news",
        "https://www.corriere.it/": "https://www.corriere.it/",
    }


def test_navigation_is_not_advertised_without_a_url() -> None:
    """Jev cannot choose an invalid navigation operation with no target."""
    req = JevAdapter().to_jev_request(
        BrowserObservation(url="about:blank", candidates=[]),
        goal="Summarize the current page.",
    )

    assert "NAVIGATE" not in req.questions["operation"].criteria
    assert "navigate_url" not in req.questions


async def test_fake_client_click_preserves_confidence_and_probabilities() -> None:
    """Fake Jev drives a CLICK decision with audit-ready confidence fields."""
    adapter = JevAdapter()
    obs = _sample_observation()
    req = adapter.to_jev_request(obs, goal="Click submit", history_summary="opened login")
    client = FakeJevClient(
        script=[
            FakeDecision(
                operation="CLICK",
                target_key="2",
                confidence=0.87,
                probabilities={"CLICK": 0.87, "DONE": 0.13},
                target_confidence=0.91,
                target_probabilities={"0": 0.09, "2": 0.91},
            )
        ]
    )
    resp = await client.decide(req)
    action = adapter.from_jev_response(resp, observation=obs)
    assert action.kind == ActionKind.CLICK
    assert action.target_index == 2
    assert action.confidence == pytest.approx(0.87)
    assert action.probabilities["CLICK"] == pytest.approx(0.87)
    assert action.target_confidence == pytest.approx(0.91)
    assert action.target_probabilities["2"] == pytest.approx(0.91)
    assert "DONE" in action.alternatives
    assert action.raw_answers


async def test_low_confidence_decision_still_maps() -> None:
    """Low confidence is preserved; adapter does not raise."""
    adapter = JevAdapter(low_confidence_threshold=0.5)
    obs = _sample_observation()
    req = adapter.to_jev_request(obs, goal="Uncertain click")
    client = FakeJevClient(
        script=[
            FakeDecision(
                operation="CLICK",
                target_key="0",
                confidence=0.22,
                probabilities={"CLICK": 0.22, "SCROLL": 0.2, "DONE": 0.58},
            )
        ]
    )
    action = adapter.from_jev_response(await client.decide(req), observation=obs)
    assert action.confidence == pytest.approx(0.22)
    assert adapter.is_low_confidence(action)
    assert action.alternatives[0] == "DONE"


async def test_done_and_scroll_and_navigate_mapping() -> None:
    """DONE / SCROLL / NAVIGATE map params without requiring element targets."""
    adapter = JevAdapter()
    obs = _sample_observation()
    req = adapter.to_jev_request(obs, goal="Finish or move")

    client = FakeJevClient(
        script=[
            FakeDecision(operation="DONE", confidence=0.99, done_message="all good"),
            FakeDecision(operation="SCROLL", scroll_direction="UP", confidence=0.8),
            FakeDecision(
                operation="NAVIGATE",
                navigate_url="https://example.test/app",
                confidence=0.7,
            ),
        ]
    )
    done = adapter.from_jev_response(await client.decide(req), observation=obs)
    assert done.kind == ActionKind.DONE
    assert done.params.message == "all good"
    assert done.target_index is None

    scroll = adapter.from_jev_response(await client.decide(req), observation=obs)
    assert scroll.kind == ActionKind.SCROLL
    assert scroll.params.direction == ScrollDirection.UP

    nav = adapter.from_jev_response(await client.decide(req), observation=obs)
    assert nav.kind == ActionKind.NAVIGATE
    assert nav.params.url == "https://example.test/app"


def test_missing_target_for_click_raises() -> None:
    """CLICK without a target head fails closed."""
    adapter = JevAdapter()
    obs = _sample_observation()
    resp = JevResponse(
        model="x",
        answers={
            "operation": {
                "type": "choice",
                "choice": "CLICK",
                "confidence": 0.9,
                "probabilities": {"CLICK": 0.9},
            }
        },
    )
    with pytest.raises(JevAdapterError, match="requires a target"):
        adapter.from_jev_response(resp, observation=obs)


def test_invalid_target_index_raises() -> None:
    """Target index absent from observation candidates is rejected."""
    adapter = JevAdapter()
    obs = _sample_observation()
    resp = JevResponse(
        model="x",
        answers={
            "operation": {
                "type": "choice",
                "choice": "CLICK",
                "confidence": 0.9,
                "probabilities": {"CLICK": 1.0},
            },
            "click_target": {
                "type": "choice",
                "choice": "99",
                "confidence": 0.5,
                "probabilities": {"99": 1.0},
            },
        },
    )
    with pytest.raises(JevAdapterError, match="not present"):
        adapter.from_jev_response(resp, observation=obs)


def test_observation_from_mapping_and_agent_action_roundtrip_fields() -> None:
    """Dict observation helper and AgentAction confidence fields validate."""
    obs = observation_from_browser_state(
        {
            "url": "https://example.test/",
            "title": "T",
            "candidates": [{"index": 1, "tag": "button", "name": "Go"}],
        }
    )
    assert obs.candidates[0].index == 1
    action = AgentAction(
        kind=ActionKind.GO_BACK,
        confidence=0.6,
        probabilities={"GO_BACK": 0.6, "DONE": 0.4},
        alternatives=["DONE"],
    )
    assert action.requires_target() is False
    with pytest.raises(ValidationError):
        AgentAction(kind=ActionKind.CLICK, confidence=1.5)


def test_modal_controls_and_popup_tabs_are_visible_to_jev() -> None:
    """DOM dialog controls and background popup tabs get explicit signals."""
    dialog = SimpleNamespace(
        node_name="div",
        attributes={"role": "dialog", "aria-label": "Cookie consent"},
        parent_node=None,
        get_meaningful_text_for_llm=lambda: "Cookie consent",
    )
    button = SimpleNamespace(
        node_name="button",
        attributes={"type": "button"},
        parent_node=dialog,
        get_meaningful_text_for_llm=lambda: "Accept cookies",
    )
    underlying_link = SimpleNamespace(
        node_name="a",
        attributes={"href": "/article"},
        parent_node=None,
        get_meaningful_text_for_llm=lambda: "Article under dialog",
    )
    observation = observation_from_browser_state(
        SimpleNamespace(
            url="https://example.test/",
            title="Example",
            tabs=[
                SimpleNamespace(
                    target_id="target-current",
                    url="https://example.test/",
                    title="Example",
                ),
                SimpleNamespace(
                    target_id="target-popup",
                    url="https://consent.example.test/",
                    title="Consent",
                ),
            ],
            dom_state=SimpleNamespace(selector_map={7: button, 8: underlying_link}),
        )
    )

    assert observation.candidates[0].is_modal_control is True
    assert [candidate.index for candidate in observation.candidates] == [7]
    assert observation.candidates[0].modal_context == "Cookie consent"
    request = JevAdapter().to_jev_request(observation, goal="Accept cookies")
    assert "modal-control" in (request.questions["click_target"].criteria["7"] or "")
    assert request.questions["tab_target"].criteria == {
        "opup": "Consent (https://consent.example.test/)"
    }

    action = JevAdapter().from_jev_response(
        JevResponse(
            answers={
                "operation": {"type": "choice", "choice": "SWITCH_TAB"},
                "tab_target": {"type": "choice", "choice": "opup"},
            }
        ),
        observation=observation,
    )
    assert action.kind == ActionKind.SWITCH_TAB
    assert action.params.tab_id == "opup"


async def test_http_client_requires_api_key() -> None:
    """Live client refuses to call without credentials."""
    client = HttpJevClient(settings=JevClientSettings(api_key=None))
    with pytest.raises(JevClientNotConfiguredError):
        await client.decide(
            JevRequest(
                state={"goal": "x"},
                questions={
                    "operation": {
                        "type": "choice",
                        "instructions": "pick",
                        "criteria": {"DONE": "done"},
                    }
                },
            )
        )


async def test_http_client_posts_with_niquests() -> None:
    """Async Jev calls use niquests without the sync convenience function."""
    client = HttpJevClient(
        settings=JevClientSettings(api_key="jv-test", base_url="http://jev.test"),
    )
    request = JevRequest(
        state={"goal": "x"},
        questions={
            "operation": {
                "type": "choice",
                "instructions": "pick",
                "criteria": {"DONE": "done"},
            }
        },
    )
    response = MagicMock()
    response.json.return_value = {
        "model": "jev-test",
        "answers": {"operation": {"type": "choice", "choice": "DONE"}},
    }
    with patch("niquests.apost", new_callable=AsyncMock) as post:
        post.return_value = response
        result = await client.decide(request)

    assert result.model == "jev-test"
    post.assert_awaited_once()
    assert post.await_args.kwargs["headers"]["Authorization"] == "Bearer jv-test"


def test_criteria_label_caps_length() -> None:
    """Candidate criteria labels stay short for Jev token budgets."""
    long_name = "x" * 200
    label = CandidateElement(index=1, tag="button", name=long_name).criteria_label()
    assert len(label) < 120
    assert "..." in label


def test_redacted_goal_shapes_do_not_leak_bearer() -> None:
    """Bearer-shaped goal text is redacted before entering Jev state."""
    adapter = JevAdapter()
    obs = _sample_observation(empty=True)
    req = adapter.to_jev_request(
        obs,
        goal="Use Authorization: Bearer super-secret-token-value-here",
    )
    assert REDACTED in str(req.state["goal"])
    assert "super-secret-token-value-here" not in str(req.state)


def _all_keys(value: object) -> set[str]:
    """Collect nested mapping keys as lowercase strings."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys |= _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _all_keys(child)
    return keys
