"""Application configuration from environment and Docker secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass

from browser_use_agent.db.settings import DatabaseSettings, load_database_settings


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset.

    Returns:
        Parsed boolean (``1``/``true``/``yes``/``on`` are true).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Runtime settings for the agent controller API.

    Attributes:
        host: Bind address for uvicorn.
        port: Bind port (Traefik loadbalancer target).
        database: Postgres connection settings when configured.
        auth_required: When true, API/WS require Authelia ``Remote-User``
            (T012). Default false for local/tests; enable in production compose.
    """

    host: str
    port: int
    database: DatabaseSettings | None
    auth_required: bool = False


def load_app_settings() -> AppSettings:
    """Load controller settings from the environment.

    Uses ``HOST`` / ``PORT`` for the HTTP server and the existing
    ``DATABASE_*`` / ``*_FILE`` helpers for Postgres. ``AUTH_REQUIRED`` gates
    anonymous access until Authelia header trust lands in T012.

    Returns:
        Immutable application settings snapshot.
    """
    host = os.environ.get("HOST", "0.0.0.0")
    port_raw = os.environ.get("PORT", "8000")
    return AppSettings(
        host=host,
        port=int(port_raw),
        database=load_database_settings(),
        auth_required=_env_bool("AUTH_REQUIRED", False),
    )
