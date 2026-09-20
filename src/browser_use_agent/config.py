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


def _split_csv(name: str) -> tuple[str, ...] | None:
    """Parse a comma-separated environment variable.

    Args:
        name: Environment variable name.

    Returns:
        Non-empty stripped parts, or ``None`` when the variable is unset.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _public_hostname() -> str | None:
    """Resolve the Traefik public hostname for this stack.

    Prefers ``BROWSER_USE_HOST``, then ``browser-use.${DOCKER_DOMAIN}`` when
    ``DOCKER_DOMAIN`` is set (Compose usually expands the former).

    Returns:
        Hostname without scheme, or ``None`` when unknown.
    """
    host = os.environ.get("BROWSER_USE_HOST", "").strip()
    if host:
        return host
    domain = os.environ.get("DOCKER_DOMAIN", "").strip()
    if domain:
        return f"browser-use.{domain}"
    return None


def _default_allowed_hosts(public_host: str | None, *, auth_required: bool) -> tuple[str, ...]:
    """Build default allowed Host values.

    Always includes loopback names so Docker ``/healthz`` probes succeed.
    When a public hostname is known, it is included. Empty when auth is off
    and no host is configured (local tests skip TrustedHost).

    Args:
        public_host: Traefik hostname, if known.
        auth_required: Production auth gate.

    Returns:
        Hostnames acceptable to :class:`~starlette.middleware.trustedhost`.
    """
    hosts: list[str] = []
    if public_host:
        hosts.append(public_host)
    if auth_required or public_host:
        for loopback in ("localhost", "127.0.0.1"):
            if loopback not in hosts:
                hosts.append(loopback)
    return tuple(hosts)


def _default_csrf_origins(public_host: str | None) -> tuple[str, ...]:
    """Build default CSRF trusted origins from the public hostname.

    Args:
        public_host: Traefik hostname, if known.

    Returns:
        ``https://`` origins, or empty when hostname is unknown.
    """
    if not public_host:
        return ()
    return (f"https://{public_host}",)


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Runtime settings for the agent controller API.

    Attributes:
        host: Bind address for uvicorn.
        port: Bind port (Traefik loadbalancer target).
        database: Postgres connection settings when configured.
        auth_required: When true, API/WS require Authelia ``Remote-User``.
            Compose production sets this true; leave false for local pytest.
        allowed_hosts: Hostnames accepted by TrustedHost middleware.
        csrf_trusted_origins: Browser origins allowed for unsafe methods / WS.
    """

    host: str
    port: int
    database: DatabaseSettings | None
    auth_required: bool = False
    allowed_hosts: tuple[str, ...] = ()
    csrf_trusted_origins: tuple[str, ...] = ()


def load_app_settings() -> AppSettings:
    """Load controller settings from the environment.

    Uses ``HOST`` / ``PORT`` for the HTTP server and the existing
    ``DATABASE_*`` / ``*_FILE`` helpers for Postgres. Auth and CSRF settings
    come from ``AUTH_REQUIRED``, ``ALLOWED_HOSTS``, ``CSRF_TRUSTED_ORIGINS``,
    and ``BROWSER_USE_HOST`` / ``DOCKER_DOMAIN``.

    Returns:
        Immutable application settings snapshot.
    """
    host = os.environ.get("HOST", "0.0.0.0")
    port_raw = os.environ.get("PORT", "8000")
    auth_required = _env_bool("AUTH_REQUIRED", False)
    public_host = _public_hostname()

    allowed = _split_csv("ALLOWED_HOSTS")
    if allowed is None:
        allowed = _default_allowed_hosts(public_host, auth_required=auth_required)

    origins = _split_csv("CSRF_TRUSTED_ORIGINS")
    if origins is None:
        origins = _default_csrf_origins(public_host)

    return AppSettings(
        host=host,
        port=int(port_raw),
        database=load_database_settings(),
        auth_required=auth_required,
        allowed_hosts=allowed,
        csrf_trusted_origins=origins,
    )
