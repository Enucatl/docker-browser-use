"""SQLAlchemy engine helpers built on database settings."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from browser_use_agent.db.settings import (
    DatabaseSettings,
    build_database_url,
    load_database_settings,
)


def build_sqlalchemy_url(settings: DatabaseSettings | None = None) -> str | None:
    """Build a SQLAlchemy ``postgresql+psycopg://`` URL from settings.

    Args:
        settings: Explicit settings; loads from the environment when omitted.

    Returns:
        Driver URL, or ``None`` when settings are incomplete.
    """
    url = build_database_url(settings)
    if url is None:
        return None
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def create_engine_from_settings(
    settings: DatabaseSettings | None = None,
    *,
    echo: bool = False,
) -> Engine | None:
    """Create a SQLAlchemy engine when database settings are available.

    Args:
        settings: Explicit settings; loads from the environment when omitted.
        echo: Forwarded to ``create_engine`` for SQL logging.

    Returns:
        Configured engine, or ``None`` when settings are incomplete.
    """
    resolved = settings if settings is not None else load_database_settings()
    url = build_sqlalchemy_url(resolved)
    if url is None:
        return None
    return create_engine(url, echo=echo, pool_pre_ping=True)
