"""Unit and integration tests for the Browser Use session manager (T013)."""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import URLError

import pytest

from browser_use_agent.browser.cdp import (
    CdpUnavailableError,
    rewrite_cdp_websocket_url,
    wait_for_cdp,
)
from browser_use_agent.browser.profiles import get_profile
from browser_use_agent.browser.session import BrowserSessionBusyError, BrowserSessionManager
from browser_use_agent.browser.settings import BrowserSettings, load_browser_settings


def _settings(**overrides: object) -> BrowserSettings:
    base = {
        "cdp_url": "http://browser:9222",
        "default_profile": "default",
        "user_data_dir": "/data/chrome-profile",
        "downloads_dir": "/data/downloads",
        "idle_ttl_seconds": 0.05,
        "cdp_ready_timeout_seconds": 1.0,
        "cdp_poll_interval_seconds": 0.01,
        "compose_control": False,
        "compose_project_dir": None,
        "compose_browser_service": "browser",
        "stop_chrome_on_idle": False,
    }
    base.update(overrides)
    return BrowserSettings(**base)  # type: ignore[arg-type]


def test_rewrite_cdp_websocket_url_peer_host() -> None:
    """Loopback DevTools URL is rewritten to the Compose peer host/port."""
    rewritten = rewrite_cdp_websocket_url(
        "http://browser:9222",
        "ws://127.0.0.1:9223/devtools/browser/abc",
    )
    assert rewritten == "ws://browser:9222/devtools/browser/abc"


def test_load_browser_profiles_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single default profile comes from env paths."""
    monkeypatch.setenv("BROWSER_PROFILE_NAME", "default")
    monkeypatch.setenv("CHROME_USER_DATA_DIR", "/data/chrome-profile")
    monkeypatch.setenv("CHROME_DOWNLOAD_DIR", "/data/downloads")
    profile = get_profile("default", settings=load_browser_settings())
    assert profile.user_data_dir == "/data/chrome-profile"
    assert profile.downloads_dir == "/data/downloads"


def test_acquire_rejects_second_run() -> None:
    """Only one interactive session holder is allowed."""

    async def _run() -> None:
        manager = BrowserSessionManager(_settings())
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()
        fake.navigate_to = AsyncMock()
        fake.get_current_page_url = AsyncMock(return_value="about:blank")
        fake.get_current_page_title = AsyncMock(return_value="")

        with (
            patch.object(manager, "ensure_chrome", AsyncMock(return_value=get_profile())),
            patch(
                "browser_use_agent.browser.session.resolve_cdp_websocket_url",
                return_value="ws://browser:9222/devtools/browser/x",
            ),
            patch("browser_use_agent.browser.session.BrowserSession", return_value=fake),
        ):
            run_a = uuid.uuid4()
            run_b = uuid.uuid4()
            await manager.acquire_for_run(run_a)
            with pytest.raises(BrowserSessionBusyError):
                await manager.acquire_for_run(run_b)
            await manager.release(run_a, shutdown_now=True)

    asyncio.run(_run())


def test_idle_detach_calls_stop_not_kill() -> None:
    """Idle TTL disconnects via stop() so Chromium/profile stay intact."""

    async def _run() -> None:
        manager = BrowserSessionManager(_settings(idle_ttl_seconds=0.05))
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()
        fake.kill = AsyncMock()

        with (
            patch.object(
                manager,
                "ensure_chrome",
                AsyncMock(return_value=get_profile(settings=_settings())),
            ),
            patch(
                "browser_use_agent.browser.session.resolve_cdp_websocket_url",
                return_value="ws://browser:9222/devtools/browser/x",
            ),
            patch("browser_use_agent.browser.session.BrowserSession", return_value=fake),
        ):
            run_id = uuid.uuid4()
            await manager.acquire_for_run(run_id)
            await manager.release(run_id)
            await asyncio.sleep(0.15)
            fake.stop.assert_awaited()
            fake.kill.assert_not_called()
            assert manager.is_attached is False

    asyncio.run(_run())


def test_wait_for_cdp_times_out() -> None:
    """wait_for_cdp raises when the endpoint never answers."""

    async def _run() -> None:
        with patch(
            "browser_use_agent.browser.cdp.cdp_version",
            side_effect=URLError("down"),
        ):
            with pytest.raises(CdpUnavailableError):
                await wait_for_cdp("http://browser:9222", timeout=0.05, poll_interval=0.01)

    asyncio.run(_run())


def _cdp_reachable(url: str) -> bool:
    try:
        from browser_use_agent.browser.cdp import cdp_version

        cdp_version(url, timeout=2.0)
        return True
    except Exception:
        return False


@pytest.mark.integration
def test_smoke_about_blank_against_live_cdp() -> None:
    """Open about:blank via Browser Use when Compose CDP is reachable.

    Manual: ``docker compose up -d browser`` then run this test with
    ``BROWSER_CDP_URL=http://browser:9222`` from a peer on the Compose network.
    """
    cdp_url = os.environ.get("BROWSER_CDP_URL", "http://browser:9222")
    if not _cdp_reachable(cdp_url):
        pytest.skip(f"CDP not reachable at {cdp_url}")

    async def _run() -> None:
        manager = BrowserSessionManager(
            _settings(cdp_url=cdp_url, idle_ttl_seconds=0, cdp_ready_timeout_seconds=30.0)
        )
        result = await manager.smoke_about_blank()
        assert "about:blank" in (result.get("url") or "")

    asyncio.run(_run())
