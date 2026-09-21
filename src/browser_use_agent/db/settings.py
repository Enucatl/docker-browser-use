"""Database connection settings from environment and Docker secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def read_secret_file(name: str, default: str | None = None) -> str | None:
    """Read a secret from the path in ``NAME_FILE`` only.

    Args:
        name: Base environment variable name (e.g. ``JEV_API_KEY``).
        default: Value when ``NAME_FILE`` is not set.

    Returns:
        File contents (newline-stripped), or ``default`` when no path is set.

    Raises:
        FileNotFoundError: When ``NAME_FILE`` points to a missing file.
    """
    file_name = os.environ.get(f"{name}_FILE")
    if not file_name:
        return default
    return Path(file_name).read_text(encoding="utf-8").rstrip("\r\n")


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Postgres connection parameters for the controller.

    Attributes:
        host: Database hostname (compose service name ``db`` in production).
        port: Database port.
        name: Database name.
        user: Database role.
        password: Password from the configured secret file; may be empty for local stubs.
    """

    host: str
    port: int
    name: str
    user: str
    password: str


def load_database_settings() -> DatabaseSettings | None:
    """Load database settings when host, name, and user are all configured.

    Returns:
        Populated settings, or ``None`` when required pieces are missing.
    """
    host = os.environ.get("DATABASE_HOST")
    name = os.environ.get("DATABASE_NAME")
    user = os.environ.get("DATABASE_USER")
    if not host or not name or not user:
        return None

    port_raw = os.environ.get("DATABASE_PORT", "5432")
    password = read_secret_file("DATABASE_PASSWORD") or ""
    return DatabaseSettings(
        host=host,
        port=int(port_raw),
        name=name,
        user=user,
        password=password,
    )


def build_database_url(settings: DatabaseSettings | None = None) -> str | None:
    """Build a ``postgres://`` URL from settings or the environment.

    Args:
        settings: Explicit settings; loads from the environment when omitted.

    Returns:
        Connection URL, or ``None`` when settings are incomplete.
    """
    resolved = settings if settings is not None else load_database_settings()
    if resolved is None:
        return None

    auth = quote(resolved.user, safe="")
    if resolved.password:
        auth = f"{auth}:{quote(resolved.password, safe='')}"
    return f"postgres://{auth}@{resolved.host}:{resolved.port}/{quote(resolved.name, safe='')}"
