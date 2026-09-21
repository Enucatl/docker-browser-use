"""Tests for T031 profile registry and exclusive profile locks."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_use_agent.browser.profiles import get_profile, list_profiles
from browser_use_agent.browser.session import BrowserSessionBusyError, BrowserSessionManager
from browser_use_agent.browser.settings import BrowserSettings


def _settings() -> BrowserSettings:
    """Build deterministic settings for profile tests."""
    return BrowserSettings(
        cdp_url="http://browser:9222",
        default_profile="testing",
        user_data_dir="/data/chrome-profiles/testing",
        downloads_dir="/data/downloads",
        idle_ttl_seconds=0,
        cdp_ready_timeout_seconds=1,
        cdp_poll_interval_seconds=0.01,
        compose_control=False,
        compose_project_dir=None,
        compose_browser_service="browser",
        stop_chrome_on_idle=False,
        profile_root="/data/chrome-profiles",
        profile_ids=("personal", "work", "testing"),
        profile_cdp_urls=(
            ("personal", "http://browser-personal:9222"),
            ("work", "http://browser-work:9222"),
            ("testing", "http://browser:9222"),
        ),
    )


def test_profiles_have_isolated_paths_and_testing_default() -> None:
    """The registry exposes three distinct persistent paths."""
    settings = _settings()
    profiles = list_profiles(settings=settings)

    assert [profile.id for profile in profiles] == ["personal", "work", "testing"]
    assert len({profile.user_data_dir for profile in profiles}) == 3
    assert profiles[-1].user_data_dir == "/data/chrome-profiles/testing"
    assert get_profile(settings=settings).id == "testing"


async def test_profile_lock_is_exclusive_per_profile() -> None:
    """A second run cannot acquire a profile already held by another run."""

    async def run() -> None:
        manager = BrowserSessionManager(_settings())
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()

        with (
            patch.object(
                manager,
                "ensure_chrome",
                AsyncMock(side_effect=lambda name: get_profile(name, settings=_settings())),
            ),
            patch(
                "browser_use_agent.browser.session.resolve_cdp_websocket_url",
                return_value="ws://browser:9222/devtools/browser/test",
            ),
            patch("browser_use_agent.browser.session.BrowserSession", return_value=fake),
        ):
            personal_run = uuid.uuid4()
            work_run = uuid.uuid4()
            await manager.acquire_for_run(personal_run, profile_name="personal")
            with pytest.raises(BrowserSessionBusyError):
                await manager.acquire_for_run(uuid.uuid4(), profile_name="personal")
            await manager.acquire_for_run(work_run, profile_name="work")
            await manager.release(personal_run, shutdown_now=True)
            await manager.release(work_run, shutdown_now=True)

    await run()
