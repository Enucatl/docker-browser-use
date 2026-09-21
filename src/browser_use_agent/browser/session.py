"""On-demand Browser Use session manager against the persistent Chrome profile."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from browser_use import BrowserSession
from sqlalchemy.orm import Session

from browser_use_agent.browser.cdp import (
    CdpUnavailableError,
    resolve_cdp_websocket_url,
    wait_for_cdp,
)
from browser_use_agent.browser.compose_control import (
    ComposeControlError,
    compose_stop_browser,
    compose_up_browser,
)
from browser_use_agent.browser.profiles import BrowserProfileConfig, get_profile
from browser_use_agent.browser.settings import BrowserSettings, load_browser_settings

if TYPE_CHECKING:
    from browser_use_agent.audit.writer import AuditWriter

logger = logging.getLogger(__name__)

AuditFactory = Callable[[Session], "AuditWriter"]


class BrowserSessionManagerError(RuntimeError):
    """Base error for browser session management failures."""


class BrowserSessionBusyError(BrowserSessionManagerError):
    """Raised when another run already holds the interactive browser session."""


class BrowserSessionManager:
    """Ensure Chrome via CDP and attach one exclusive session per profile.

    Concurrency: each profile has one active interactive holder. Idle TTL disconnects
    Browser Use with ``stop()`` (does not kill Chromium), avoiding profile
    corruption from abrupt CDP teardown. Optional Compose control can start or
    stop the ``browser`` service when Docker CLI access is available.

    Attributes:
        settings: Browser / CDP configuration.
    """

    def __init__(
        self,
        settings: BrowserSettings | None = None,
        *,
        audit_factory: AuditFactory | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        """Create a session manager.

        Args:
            settings: Optional browser settings; loads from the environment when omitted.
            audit_factory: Optional ``session -> AuditWriter`` for start/stop events.
            session_factory: Optional DB session factory used with ``audit_factory``.
        """
        self.settings = settings if settings is not None else load_browser_settings()
        self._audit_factory = audit_factory
        self._session_factory = session_factory
        self._lock = asyncio.Lock()
        self._browsers: dict[str, BrowserSession] = {}
        self._holder_run_ids: dict[str, uuid.UUID] = {}
        self._run_profiles: dict[uuid.UUID, str] = {}
        self._profiles: dict[str, BrowserProfileConfig] = {}
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._chrome_started_via_compose = False

    @property
    def is_attached(self) -> bool:
        """Return whether a Browser Use session is currently attached."""
        return bool(self._browsers)

    @property
    def holder_run_id(self) -> uuid.UUID | None:
        """Return the run that currently holds the interactive session, if any."""
        return next(iter(self._holder_run_ids.values()), None)

    async def ensure_chrome(self, profile_name: str | None = None) -> BrowserProfileConfig:
        """Ensure Chromium CDP is reachable for the given profile.

        Optionally starts the Compose ``browser`` service when
        ``BROWSER_COMPOSE_CONTROL`` is enabled. Does not attach Browser Use yet.

        Args:
            profile_name: Profile key; defaults to the configured default.

        Returns:
            Resolved profile configuration.

        Raises:
            CdpUnavailableError: When CDP never becomes ready.
            ComposeControlError: When compose auto-start fails.
            KeyError: When the profile name is unknown.
        """
        profile = get_profile(profile_name, settings=self.settings)
        if self.settings.compose_control:
            try:
                await wait_for_cdp(
                    profile.cdp_url,
                    timeout=min(2.0, self.settings.cdp_ready_timeout_seconds),
                    poll_interval=self.settings.cdp_poll_interval_seconds,
                )
                return profile
            except CdpUnavailableError:
                logger.info(
                    "CDP not ready; starting Compose service %s for profile %s",
                    self.settings.compose_browser_service,
                    profile.name,
                )
                await compose_up_browser(self.settings)
                self._chrome_started_via_compose = True

        await wait_for_cdp(
            profile.cdp_url,
            timeout=self.settings.cdp_ready_timeout_seconds,
            poll_interval=self.settings.cdp_poll_interval_seconds,
        )
        return profile

    async def acquire_for_run(
        self,
        run_id: uuid.UUID,
        *,
        profile_name: str | None = None,
        db_session: Session | None = None,
    ) -> BrowserSession:
        """Attach Browser Use for ``run_id`` (single interactive session).

        Args:
            run_id: Owning agent run.
            profile_name: Optional profile key.
            db_session: Optional SQLAlchemy session for audit events.

        Returns:
            Connected :class:`~browser_use.BrowserSession`.

        Raises:
            BrowserSessionBusyError: When another run holds the session.
            CdpUnavailableError: When Chrome/CDP is unreachable.
            BrowserSessionManagerError: On attach failure.
        """
        async with self._lock:
            profile = get_profile(profile_name, settings=self.settings)
            holder = self._holder_run_ids.get(profile.id)
            if not self.settings.profile_ids and self._holder_run_ids:
                holder = next(iter(self._holder_run_ids.values()))
            if holder is not None and holder != run_id:
                raise BrowserSessionBusyError(
                    f"browser profile {profile.id!r} held by run {holder}; "
                    f"refusing acquire for {run_id}"
                )
            self._cancel_idle_timer(profile.id)
            if holder == run_id and profile.id in self._browsers:
                return self._browsers[profile.id]

            profile = await self.ensure_chrome(profile.id)
            browser = await self._attach(profile)
            self._browsers[profile.id] = browser
            self._holder_run_ids[profile.id] = run_id
            self._run_profiles[run_id] = profile.id
            self._profiles[profile.id] = profile
            self._emit_audit(
                run_id,
                "browser_started",
                {
                    "profile": profile.name,
                    "user_data_dir": profile.user_data_dir,
                    "downloads_dir": profile.downloads_dir,
                    "cdp_url": profile.cdp_url,
                    "headless": self.settings.headless_documented,
                },
                db_session=db_session,
            )
            return browser

    async def release(
        self,
        run_id: uuid.UUID,
        *,
        db_session: Session | None = None,
        shutdown_now: bool = False,
    ) -> None:
        """Release the interactive hold for ``run_id`` and arm idle disconnect.

        Args:
            run_id: Run that previously acquired the session.
            db_session: Optional SQLAlchemy session for audit on immediate shutdown.
            shutdown_now: When true, disconnect immediately instead of waiting for idle TTL.
        """
        async with self._lock:
            profile_id = self._run_profiles.get(run_id)
            if profile_id is None or self._holder_run_ids.get(profile_id) != run_id:
                return
            self._holder_run_ids.pop(profile_id, None)
            self._run_profiles.pop(run_id, None)
            if shutdown_now or self.settings.idle_ttl_seconds <= 0:
                await self._disconnect(
                    profile_id=profile_id,
                    run_id=run_id,
                    db_session=db_session,
                )
            else:
                self._arm_idle_timer(profile_id=profile_id, last_run_id=run_id)

    async def shutdown(self, *, db_session: Session | None = None) -> None:
        """Disconnect Browser Use and cancel idle timers.

        Args:
            db_session: Optional SQLAlchemy session for a final audit event.
        """
        async with self._lock:
            for profile_id in tuple(self._idle_tasks):
                self._cancel_idle_timer(profile_id)
            for profile_id, run_id in tuple(self._holder_run_ids.items()):
                await self._disconnect(
                    profile_id=profile_id,
                    run_id=run_id,
                    db_session=db_session,
                )
            for profile_id in tuple(self._browsers):
                await self._disconnect(profile_id=profile_id, run_id=None, db_session=db_session)
            self._holder_run_ids.clear()
            self._run_profiles.clear()

    async def smoke_about_blank(self, *, profile_name: str | None = None) -> dict[str, Any]:
        """Integration helper: attach, open ``about:blank``, observe URL once.

        Args:
            profile_name: Optional profile key.

        Returns:
            Observation dict with ``url``, ``title``, and ``profile``.
        """
        run_id = uuid.uuid4()
        browser = await self.acquire_for_run(run_id, profile_name=profile_name)
        try:
            await browser.navigate_to("about:blank")
            url = await browser.get_current_page_url()
            title = await browser.get_current_page_title()
            return {
                "url": url,
                "title": title,
                "profile": profile_name or self.settings.default_profile,
                "cdp_url": get_profile(profile_name, settings=self.settings).cdp_url,
            }
        finally:
            await self.release(run_id, shutdown_now=True)

    async def _attach(self, profile: BrowserProfileConfig) -> BrowserSession:
        """Create and start a Browser Use session against rewritten CDP WebSocket.

        Args:
            profile: Profile paths for downloads metadata.

        Returns:
            Started Browser Use session.

        Raises:
            BrowserSessionManagerError: When start fails.
        """
        if profile.id in self._browsers:
            return self._browsers[profile.id]

        try:
            ws_url = await asyncio.to_thread(
                resolve_cdp_websocket_url,
                profile.cdp_url,
            )
        except Exception as exc:
            raise BrowserSessionManagerError(f"failed to resolve CDP websocket: {exc}") from exc

        logger.info(
            "Attaching Browser Use to %s (profile=%s downloads=%s)",
            profile.cdp_url,
            profile.name,
            profile.downloads_dir,
        )
        # Remote CDP: Chrome already uses the persistent volume in the browser
        # container. Pass downloads_path for Browser Use download watchdogs.
        browser = BrowserSession(
            cdp_url=ws_url,
            is_local=False,
            downloads_path=profile.downloads_dir,
            keep_alive=True,
        )
        try:
            await browser.start()
        except Exception as exc:
            raise BrowserSessionManagerError(f"Browser Use start failed: {exc}") from exc
        return browser

    async def _disconnect(
        self,
        *,
        profile_id: str,
        run_id: uuid.UUID | None,
        db_session: Session | None,
    ) -> None:
        """Gracefully stop Browser Use without killing Chromium.

        Args:
            run_id: Optional run id for audit attribution.
            db_session: Optional SQLAlchemy session for audit.
        """
        browser = self._browsers.pop(profile_id, None)
        profile = self._profiles.pop(profile_id, None)
        if browser is not None:
            try:
                # stop() keeps Chromium alive so the persistent profile stays intact.
                await browser.stop()
            except Exception:
                logger.exception("Browser Use stop() failed; profile should still be intact")
            if run_id is not None:
                self._emit_audit(
                    run_id,
                    "browser_stopped",
                    {
                        "profile": profile.name if profile else None,
                        "user_data_dir": profile.user_data_dir if profile else None,
                        "cdp_url": profile.cdp_url if profile else None,
                        "mode": "detach",
                    },
                    db_session=db_session,
                )

        if (
            self.settings.compose_control
            and self.settings.stop_chrome_on_idle
            and self._chrome_started_via_compose
        ):
            try:
                await compose_stop_browser(self.settings)
                self._chrome_started_via_compose = False
                logger.info("Compose browser service stopped after idle detach")
            except ComposeControlError:
                logger.exception("Failed to stop Compose browser service after idle")

    def _arm_idle_timer(self, *, profile_id: str, last_run_id: uuid.UUID) -> None:
        """Schedule idle disconnect after ``idle_ttl_seconds``.

        Args:
            last_run_id: Run id used for audit when the timer fires.
        """
        self._cancel_idle_timer(profile_id)
        ttl = self.settings.idle_ttl_seconds

        async def _idle() -> None:
            try:
                await asyncio.sleep(ttl)
                async with self._lock:
                    if profile_id in self._holder_run_ids:
                        return
                    await self._disconnect(
                        profile_id=profile_id,
                        run_id=last_run_id,
                        db_session=None,
                    )
            except asyncio.CancelledError:
                raise

        self._idle_tasks[profile_id] = asyncio.create_task(
            _idle(), name=f"browser-idle-ttl-{profile_id}"
        )

    def _cancel_idle_timer(self, profile_id: str) -> None:
        """Cancel a pending idle disconnect task."""
        task = self._idle_tasks.pop(profile_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _emit_audit(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        db_session: Session | None,
    ) -> None:
        """Append an audit event when a writer can be constructed.

        Args:
            run_id: Owning run.
            event_type: ``browser_started`` or ``browser_stopped``.
            payload: Structured event metadata.
            db_session: Explicit session, or one from ``session_factory``.
        """
        if self._audit_factory is None:
            return
        session = db_session
        owns_session = False
        if session is None and self._session_factory is not None:
            session = self._session_factory()
            owns_session = True
        if session is None:
            logger.debug("Skipping audit %s; no DB session available", event_type)
            return
        try:
            writer = self._audit_factory(session)
            writer.append(run_id, event_type, payload, actor="system")
            if owns_session:
                session.commit()
        except Exception:
            logger.exception("Failed to write audit event %s", event_type)
            if owns_session:
                session.rollback()
        finally:
            if owns_session:
                session.close()
