"""Tests for central secret redaction (T008)."""

from __future__ import annotations

from browser_use_agent.security import (
    REDACTED,
    is_allow_field,
    is_deny_field,
    mask_pan,
    redact_browser_state,
    redact_for_audit,
    redact_mapping,
    redact_text,
)

# Well-known test card number (Stripe/Visa test PAN); Luhn-valid.
_VISA_TEST = "4111111111111111"
_VISA_MASKED = "****1111"

# Synthetic JWT-shaped token (header.payload.sig) — not a real credential.
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepaddingvaluehere"
)


def test_deny_and_allow_field_helpers() -> None:
    """Deny/allow lists cover password and structural UI fields."""
    assert is_deny_field("password")
    assert is_deny_field("Set-Cookie")
    assert is_deny_field("Authorization")
    assert is_allow_field("role")
    assert is_allow_field("aria-label")
    assert is_allow_field("last4")
    assert not is_deny_field("role")
    assert not is_allow_field("password")


def test_redact_mapping_password_fields() -> None:
    """Password-like keys become the stable placeholder."""
    payload = {
        "username": "alice",
        "password": "hunter2-secret",
        "nested": {"passwd": "another-secret", "label": "Sign in"},
    }
    out = redact_mapping(payload)
    assert out["username"] == "alice"
    assert out["password"] == REDACTED
    assert out["nested"]["passwd"] == REDACTED
    assert out["nested"]["label"] == "Sign in"
    assert "hunter2" not in str(out)
    assert "another-secret" not in str(out)


def test_redact_mapping_cookies_and_authorization() -> None:
    """Cookie and Authorization headers never survive."""
    payload = {
        "headers": {
            "Authorization": "Bearer super-secret-token-value",
            "Cookie": "session=abc123; theme=dark",
            "Set-Cookie": "session=abc123; HttpOnly",
            "Content-Type": "application/json",
        },
        "cookies": ["session=abc123", "prefs=1"],
    }
    out = redact_mapping(payload)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["Cookie"] == REDACTED
    assert out["headers"]["Set-Cookie"] == REDACTED
    assert out["headers"]["Content-Type"] == "application/json"
    assert out["cookies"] == [REDACTED, REDACTED]
    assert "abc123" not in str(out)
    assert "super-secret-token-value" not in str(out)


def test_redact_text_jwt_and_bearer() -> None:
    """JWT and Bearer tokens in free text are stripped."""
    text = f"token={_FAKE_JWT} and Authorization: Bearer abcdefghijklmnop"
    out = redact_text(text)
    assert _FAKE_JWT not in out
    assert "abcdefghijklmnop" not in out
    assert REDACTED in out
    assert "Bearer" in out


def test_redact_text_password_assignment() -> None:
    """password=... assignments in text are redacted."""
    out = redact_text("login failed password=s3cretValue ok")
    assert "s3cretValue" not in out
    assert "password=" + REDACTED in out


def test_redact_card_numbers_masked_last4() -> None:
    """Luhn-valid PANs become ****last4; invalid digit runs stay."""
    assert mask_pan(_VISA_TEST) == _VISA_MASKED
    out = redact_text(f"card {_VISA_TEST} charged")
    assert _VISA_TEST not in out
    assert _VISA_MASKED in out
    # Non-Luhn 16-digit run should not be treated as a PAN.
    fake = "1234567890123456"
    assert redact_text(f"id={fake}") == f"id={fake}"


def test_already_masked_pan_and_last4_field_preserved() -> None:
    """Intentionally masked last-4 forms are not over-redacted."""
    assert redact_text("****4242") == "****4242"
    out = redact_mapping({"last4": "4242", "masked_pan": "****4242", "status": "ok"})
    assert out["last4"] == "4242"
    assert out["masked_pan"] == "****4242"
    assert out["status"] == "ok"
    card = redact_mapping({"card_number": _VISA_TEST})
    assert card["card_number"] == _VISA_MASKED


def test_nested_dom_attribute_bags() -> None:
    """Nested DOM attribute bags redact secrets but keep roles and labels."""
    state = {
        "elements": [
            {
                "role": "textbox",
                "name": "Email",
                "attributes": {
                    "type": "email",
                    "placeholder": "you@example.com",
                    "value": "user@example.com",
                },
            },
            {
                "role": "textbox",
                "name": "Password",
                "attributes": {
                    "type": "password",
                    "value": "correct-horse-battery",
                    "autocomplete": "current-password",
                },
            },
            {
                "role": "textbox",
                "name": "Card",
                "attributes": {
                    "type": "text",
                    "value": _VISA_TEST,
                },
            },
        ]
    }
    out = redact_browser_state(state)
    email = out["elements"][0]
    assert email["role"] == "textbox"
    assert email["attributes"]["placeholder"] == "you@example.com"
    assert email["attributes"]["value"] == "user@example.com"

    pwd = out["elements"][1]
    assert pwd["attributes"]["type"] == "password"
    assert pwd["attributes"]["value"] == REDACTED
    assert "correct-horse-battery" not in str(out)

    card = out["elements"][2]
    assert _VISA_TEST not in str(card)
    assert card["attributes"]["value"] == _VISA_MASKED


def test_do_not_over_redact_ordinary_ui_text() -> None:
    """Harmless UI copy, URLs, and roles survive redaction."""
    payload = {
        "url": "https://shop.example/checkout/shipping",
        "role": "button",
        "label": "Continue to payment",
        "text": "Order summary: 2 items, ships tomorrow",
        "action": "click",
        "event_type": "browser.action",
    }
    out = redact_mapping(payload)
    assert out == payload
    assert redact_text("Click Continue to review your order") == (
        "Click Continue to review your order"
    )


def test_redact_for_audit_is_approved_entry_point() -> None:
    """redact_for_audit deep-redacts mappings used by audit writers."""
    payload = {
        "event_type": "bitwarden.fill",
        "success": True,
        "password": "should-not-persist",
        "cvv": "123",
        "url": "https://bank.example/login",
    }
    out = redact_for_audit(payload)
    assert out["event_type"] == "bitwarden.fill"
    assert out["success"] is True
    assert out["url"] == "https://bank.example/login"
    assert out["password"] == REDACTED
    assert out["cvv"] == REDACTED
    assert "should-not-persist" not in str(out)


def test_redact_for_audit_browser_shaped_payload() -> None:
    """Browser-shaped payloads use the browser-state walker."""
    payload = {
        "dom": {
            "attributes": {"type": "password", "value": "vault-secret"},
            "role": "textbox",
        }
    }
    out = redact_for_audit(payload)
    assert out["dom"]["attributes"]["value"] == REDACTED
    assert out["dom"]["role"] == "textbox"
    assert "vault-secret" not in str(out)


def test_api_key_and_token_fields() -> None:
    """API keys and tokens in structured fields are fully redacted."""
    out = redact_mapping(
        {
            "api_key": "sk-live-abcdefghijklmnopqrstuvwxyz",
            "access_token": "tok_abc123456789",
            "client_secret": "cs_secret_value_here",
            "note": "api_key=sk-live-abcdefghijklmnopqrstuvwxyz",
        }
    )
    assert out["api_key"] == REDACTED
    assert out["access_token"] == REDACTED
    assert out["client_secret"] == REDACTED
    assert "sk-live" not in out["note"]
