"""Tests for database connection settings helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_use_agent.db import build_database_url, load_database_settings, read_env_or_file


def test_read_env_or_file_prefers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``NAME_FILE`` wins over a bare ``NAME`` env var."""
    secret = tmp_path / "password"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_PASSWORD", "from-env")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    assert read_env_or_file("DATABASE_PASSWORD") == "from-file"


def test_load_database_settings_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Required DATABASE_* vars plus password file produce settings."""
    secret = tmp_path / "postgres_password"
    secret.write_text("s3cret", encoding="utf-8")
    monkeypatch.setenv("DATABASE_HOST", "db")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "browser_use")
    monkeypatch.setenv("DATABASE_USER", "browser_use")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))

    settings = load_database_settings()
    assert settings is not None
    assert settings.host == "db"
    assert settings.port == 5432
    assert settings.name == "browser_use"
    assert settings.user == "browser_use"
    assert settings.password == "s3cret"
    assert build_database_url(settings) == ("postgres://browser_use:s3cret@db:5432/browser_use")


def test_load_database_settings_missing_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Incomplete env yields ``None`` (local stub without Postgres)."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.setenv("DATABASE_NAME", "browser_use")
    monkeypatch.setenv("DATABASE_USER", "browser_use")
    assert load_database_settings() is None
    assert build_database_url() is None
