"""Persistent Chrome profile path configuration."""

from __future__ import annotations

from dataclasses import dataclass

from browser_use_agent.browser.settings import BrowserSettings, load_browser_settings


@dataclass(frozen=True, slots=True)
class BrowserProfileConfig:
    """One agent Chrome profile backed by a Docker volume path.

    Attributes:
        name: Stable profile key (single ``default`` until T031).
        user_data_dir: Absolute path inside the browser container volume.
        downloads_dir: Absolute downloads path shared for artifact ingestion.
    """

    name: str
    user_data_dir: str
    downloads_dir: str


def get_profile(
    name: str | None = None,
    *,
    settings: BrowserSettings | None = None,
) -> BrowserProfileConfig:
    """Resolve a profile by name.

    Args:
        name: Profile key; defaults to ``settings.default_profile``.
        settings: Optional preloaded browser settings.

    Returns:
        Matching :class:`BrowserProfileConfig`.

    Raises:
        KeyError: When the profile name is unknown.
    """
    resolved = settings if settings is not None else load_browser_settings()
    key = name or resolved.default_profile
    if key != resolved.default_profile:
        raise KeyError(f"unknown browser profile {key!r}; known={[resolved.default_profile]}")
    return BrowserProfileConfig(
        name=resolved.default_profile,
        user_data_dir=resolved.user_data_dir,
        downloads_dir=resolved.downloads_dir,
    )
