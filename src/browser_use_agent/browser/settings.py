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


def load_browser_settings() -> BrowserSettings:
    """Load browser session settings from the environment.

    Returns:
        Immutable browser settings snapshot.
    """
    project_dir = os.environ.get("BROWSER_COMPOSE_PROJECT_DIR", "").strip() or None
    return BrowserSettings(
        cdp_url=os.environ.get("BROWSER_CDP_URL", "http://browser:9222").rstrip("/"),
        default_profile=os.environ.get("BROWSER_PROFILE_NAME", "default").strip() or "default",
        user_data_dir=os.environ.get("CHROME_USER_DATA_DIR", "/data/chrome-profile"),
        downloads_dir=os.environ.get("CHROME_DOWNLOAD_DIR", "/data/downloads"),
        idle_ttl_seconds=_env_float("BROWSER_IDLE_TTL_SECONDS", 300.0),
        cdp_ready_timeout_seconds=_env_float("BROWSER_CDP_READY_TIMEOUT_SECONDS", 60.0),
        cdp_poll_interval_seconds=_env_float("BROWSER_CDP_POLL_INTERVAL_SECONDS", 0.5),
        compose_control=_env_bool("BROWSER_COMPOSE_CONTROL", False),
        compose_project_dir=project_dir,
        compose_browser_service=os.environ.get("BROWSER_COMPOSE_SERVICE", "browser").strip()
        or "browser",
        stop_chrome_on_idle=_env_bool("BROWSER_STOP_ON_IDLE", False),
    )
