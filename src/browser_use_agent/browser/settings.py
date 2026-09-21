"""Environment settings for the Browser Use session manager."""

from __future__ import annotations

import os
from dataclasses import dataclass


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


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        Parsed float.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """CDP and profile settings for on-demand Browser Use sessions.

    Attributes:
        cdp_url: Peer-facing HTTP CDP base (e.g. ``http://browser:9222``).
        default_profile: Profile key used when a run omits ``profile_id``.
        user_data_dir: Chrome user-data path inside the browser container.
        downloads_dir: Download directory shared with the controller when mounted.
        idle_ttl_seconds: Disconnect Browser Use after this idle period.
        cdp_ready_timeout_seconds: Max wait for CDP health in ``ensure_chrome``.
        cdp_poll_interval_seconds: Delay between CDP health polls.
        compose_control: When true, start/stop the Compose ``browser`` service.
        compose_project_dir: Working directory for ``docker compose`` (optional).
        compose_browser_service: Compose service name for Chromium.
        stop_chrome_on_idle: When true with compose control, stop the service on idle.
        headless_documented: Operator note — worker is headless until T022/Xvfb.
        profile_root: Root directory containing persistent profile subdirectories.
        profile_ids: Stable profile ids exposed by the registry.
        profile_cdp_urls: CDP URL overrides keyed by profile id.
    """

    cdp_url: str
    default_profile: str
    user_data_dir: str
    downloads_dir: str
    idle_ttl_seconds: float
    cdp_ready_timeout_seconds: float
    cdp_poll_interval_seconds: float
    compose_control: bool
    compose_project_dir: str | None
    compose_browser_service: str
    stop_chrome_on_idle: bool
    headless_documented: bool = True
    profile_root: str | None = None
    profile_ids: tuple[str, ...] = ()
    profile_cdp_urls: tuple[tuple[str, str], ...] = ()


def load_browser_settings() -> BrowserSettings:
    """Load browser session settings from the environment.

    Returns:
        Immutable browser settings snapshot.
    """
    project_dir = os.environ.get("BROWSER_COMPOSE_PROJECT_DIR", "").strip() or None
    default_profile = os.environ.get("BROWSER_PROFILE_NAME", "testing").strip() or "testing"
    configured_root = os.environ.get("BROWSER_PROFILE_ROOT")
    legacy_user_data_dir = os.environ.get("CHROME_USER_DATA_DIR")
    profile_root = (
        configured_root.strip()
        if configured_root is not None
        else None
        if legacy_user_data_dir is not None
        else "/data/chrome-profiles"
    )
    user_data_dir = legacy_user_data_dir or f"{profile_root}/{default_profile}"
    raw_ids = os.environ.get(
        "BROWSER_PROFILE_IDS",
        "default" if default_profile == "default" else "personal,work,testing",
    )
    profile_ids = tuple(part.strip() for part in raw_ids.split(",") if part.strip())
    raw_cdp_urls = os.environ.get(
        "BROWSER_PROFILE_CDP_URLS",
        "testing=http://browser:9222,personal=http://browser-personal:9222,"
        "work=http://browser-work:9222",
    )
    profile_cdp_urls = tuple(
        (profile_id.strip(), url.strip())
        for entry in raw_cdp_urls.split(",")
        if "=" in entry
        for profile_id, url in [entry.split("=", 1)]
        if profile_id.strip() and url.strip()
    )
    return BrowserSettings(
        cdp_url=os.environ.get("BROWSER_CDP_URL", "http://browser:9222").rstrip("/"),
        default_profile=default_profile,
        user_data_dir=user_data_dir,
        downloads_dir=os.environ.get("CHROME_DOWNLOAD_DIR", "/data/downloads"),
        idle_ttl_seconds=_env_float("BROWSER_IDLE_TTL_SECONDS", 300.0),
        cdp_ready_timeout_seconds=_env_float("BROWSER_CDP_READY_TIMEOUT_SECONDS", 60.0),
        cdp_poll_interval_seconds=_env_float("BROWSER_CDP_POLL_INTERVAL_SECONDS", 0.5),
        compose_control=_env_bool("BROWSER_COMPOSE_CONTROL", False),
        compose_project_dir=project_dir,
        compose_browser_service=os.environ.get("BROWSER_COMPOSE_SERVICE", "browser").strip()
        or "browser",
        stop_chrome_on_idle=_env_bool("BROWSER_STOP_ON_IDLE", False),
        profile_root=profile_root,
        profile_ids=profile_ids,
        profile_cdp_urls=profile_cdp_urls,
    )
