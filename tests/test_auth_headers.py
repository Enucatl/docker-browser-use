"""Tests for Authelia Remote-User trust and CSRF/origin hardening (T012)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from browser_use_agent.api.app import create_app
from browser_use_agent.api.auth import resolve_identity, user_from_authelia_headers
from browser_use_agent.api.csrf import check_csrf_headers, origin_is_trusted
from browser_use_agent.config import AppSettings, load_app_settings


def _settings(**overrides: object) -> AppSettings:
    """Build AppSettings for auth tests without a database.

    Args:
        **overrides: Fields to replace on the default local settings.

    Returns:
        Immutable settings snapshot.
    """
    base = AppSettings(
        host="127.0.0.1",
        port=8000,
        database=None,
        auth_required=False,
        allowed_hosts=(),
        csrf_trusted_origins=(),
    )
    data = {
        "host": base.host,
        "port": base.port,
        "database": base.database,
        "auth_required": base.auth_required,
        "allowed_hosts": base.allowed_hosts,
        "csrf_trusted_origins": base.csrf_trusted_origins,
    }
    data.update(overrides)
    return AppSettings(**data)  # type: ignore[arg-type]


def test_resolve_identity_requires_remote_user_when_auth_required() -> None:
    """AUTH_REQUIRED accepts only Remote-User, not the local-dev header."""
    assert resolve_identity(Headers({}), auth_required=True) is None
    assert resolve_identity(Headers({"x-browser-use-dev-user": "dev"}), auth_required=True) is None
    user = resolve_identity(Headers({"remote-user": "alice"}), auth_required=True)
    assert user is not None
    assert user.username == "alice"
    assert user.source == "remote-user"


def test_resolve_identity_local_bypass() -> None:
    """Without AUTH_REQUIRED, missing headers yield the anonymous stub."""
    anon = resolve_identity(Headers({}), auth_required=False)
    assert anon is not None
    assert anon.username == "anonymous"
    assert anon.source == "anonymous-stub"

    dev = resolve_identity(Headers({"x-browser-use-dev-user": "bob"}), auth_required=False)
    assert dev is not None
    assert dev.username == "bob"
    assert dev.source == "dev-header"


def test_user_from_authelia_headers_parses_groups() -> None:
    """Remote-Groups is split into a tuple; name and email are preserved."""
    user = user_from_authelia_headers(
        Headers(
            {
                "remote-user": "alice",
                "remote-groups": "admins, operators",
                "remote-name": "Alice",
                "remote-email": "alice@example.com",
            }
        )
    )
    assert user is not None
    assert user.groups == ("admins", "operators")
    assert user.name == "Alice"
    assert user.email == "alice@example.com"


def test_api_rejects_missing_remote_user_when_auth_required() -> None:
    """API routes return 401 without Remote-User when AUTH_REQUIRED is true."""
    app = create_app(
        _settings(
            auth_required=True,
            csrf_trusted_origins=("https://browser-use.docker.home.arpa",),
        )
    )
    client = TestClient(app)
    response = client.get("/api/runs")
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_api_accepts_remote_user_when_auth_required() -> None:
    """Present Remote-User allows the request past the auth gate."""
    app = create_app(
        _settings(
            auth_required=True,
            # Empty origins: skip CSRF so this test isolates identity only.
            csrf_trusted_origins=(),
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    # No DB configured → handler errors after auth; must not be 401.
    response = client.get("/api/runs", headers={"Remote-User": "alice"})
    assert response.status_code != 401


def test_healthz_exempt_when_auth_required() -> None:
    """GET /healthz stays open for Docker healthchecks."""
    app = create_app(_settings(auth_required=True))
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_csrf_rejects_untrusted_origin_on_post() -> None:
    """Unsafe methods with a foreign Origin are rejected when enforced."""
    origin = "https://browser-use.docker.home.arpa"
    app = create_app(
        _settings(
            auth_required=True,
            csrf_trusted_origins=(origin,),
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    bad = client.post(
        "/api/runs",
        json={"goal": "x"},
        headers={"Remote-User": "alice", "Origin": "https://evil.example"},
    )
    assert bad.status_code == 403
    assert bad.json()["detail"] == "Origin not trusted"

    # Without DB the handler errors after CSRF+auth; must not be 403/401.
    good = client.post(
        "/api/runs",
        json={"goal": "x"},
        headers={"Remote-User": "alice", "Origin": origin},
    )
    assert good.status_code not in {401, 403}


def test_origin_is_trusted_helper() -> None:
    """Origin matching is case-insensitive on scheme and host."""
    trusted = ("https://browser-use.docker.home.arpa",)
    assert origin_is_trusted("https://browser-use.docker.home.arpa", trusted)
    assert origin_is_trusted("HTTPS://Browser-Use.docker.home.arpa", trusted)
    assert not origin_is_trusted("https://evil.example", trusted)
    assert not origin_is_trusted(None, trusted)


def test_csrf_check_allows_safe_methods() -> None:
    """GET is never blocked by CSRF origin checks."""
    assert check_csrf_headers(
        method="GET",
        origin="https://evil.example",
        referer=None,
        trusted_origins=("https://browser-use.docker.home.arpa",),
        enforce=True,
    )


def test_load_app_settings_derives_host_and_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BROWSER_USE_HOST seeds ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS."""
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CSRF_TRUSTED_ORIGINS", raising=False)
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("BROWSER_USE_HOST", "browser-use.docker.home.arpa")
    settings = load_app_settings()
    assert "browser-use.docker.home.arpa" in settings.allowed_hosts
    assert "localhost" in settings.allowed_hosts
    assert settings.csrf_trusted_origins == ("https://browser-use.docker.home.arpa",)
