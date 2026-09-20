"""Application configuration from environment and Docker secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass

from browser_use_agent.db.settings import DatabaseSettings, load_database_settings


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Runtime settings for the agent controller API.

    Attributes:
        host: Bind address for uvicorn.
        port: Bind port (Traefik loadbalancer target).
        database: Postgres connection settings when configured.
    """

    host: str
    port: int
    database: DatabaseSettings | None


def load_app_settings() -> AppSettings:
    """Load controller settings from the environment.

    Uses ``HOST`` / ``PORT`` for the HTTP server and the existing
    ``DATABASE_*`` / ``*_FILE`` helpers for Postgres.

    Returns:
        Immutable application settings snapshot.
    """
    host = os.environ.get("HOST", "0.0.0.0")
    port_raw = os.environ.get("PORT", "8000")
    return AppSettings(
        host=host,
        port=int(port_raw),
        database=load_database_settings(),
    )
