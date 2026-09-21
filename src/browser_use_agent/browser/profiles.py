"""Persistent Chrome profile registry and path configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from browser_use_agent.browser.settings import BrowserSettings, load_browser_settings


@dataclass(frozen=True, slots=True)
class BrowserProfileConfig:
    """One persistent Chrome profile exposed to the controller.

    Attributes:
        id: Stable profile key.
        display_name: Human-readable name for the UI.
        user_data_dir: Absolute path inside the browser container volume.
        notes: Operator-facing description.
        downloads_dir: Absolute downloads path shared for artifact ingestion.
        cdp_url: Internal CDP endpoint for this profile's Chrome service.
    """

    id: str
    display_name: str
    user_data_dir: str
    notes: str
    downloads_dir: str
    cdp_url: str

    @property
    def name(self) -> str:
        """Return the profile id for T013 callers."""
        return self.id


_PROFILE_DETAILS = {
    "default": ("Primary", "Primary persistent browser state."),
    "personal": ("Personal", "Personal cookies and browsing state."),
    "work": ("Work", "Work cookies and browsing state."),
    "testing": ("Testing", "Isolated smoke-test profile; default."),
}


def list_profiles(
    *,
    settings: BrowserSettings | None = None,
) -> tuple[BrowserProfileConfig, ...]:
    """Return the configured persistent Chrome profile registry.

    Args:
        settings: Optional preloaded browser settings.

    Returns:
        Profiles in configured order.
    """
    resolved = settings if settings is not None else load_browser_settings()
    ids = resolved.profile_ids or (resolved.default_profile,)
    root = (
        Path(resolved.profile_root)
        if resolved.profile_root
        else Path(resolved.user_data_dir).parent
    )
    cdp_urls = dict(resolved.profile_cdp_urls)
    profiles: list[BrowserProfileConfig] = []
    for profile_id in ids:
        display_name, notes = _PROFILE_DETAILS.get(
            profile_id, (profile_id.replace("-", " ").title(), "Configured Chrome profile.")
        )
        user_data_dir = (
            resolved.user_data_dir
            if profile_id == resolved.default_profile and not resolved.profile_root
            else str(root / profile_id)
        )
        profiles.append(
            BrowserProfileConfig(
                id=profile_id,
                display_name=display_name,
                user_data_dir=user_data_dir,
                notes=notes,
                downloads_dir=resolved.downloads_dir,
                cdp_url=cdp_urls.get(profile_id, resolved.cdp_url),
            )
        )
    return tuple(profiles)


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
    for profile in list_profiles(settings=resolved):
        if profile.id == key:
            return profile
    known = [profile.id for profile in list_profiles(settings=resolved)]
    raise KeyError(f"unknown browser profile {key!r}; known={known}")
