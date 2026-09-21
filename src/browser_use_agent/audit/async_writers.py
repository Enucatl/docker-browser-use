"""Async adapters for the existing redacting audit writers."""

from __future__ import annotations

import inspect
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from browser_use_agent.artifacts.store import ArtifactStore
from browser_use_agent.audit.browser_actions import BrowserActionWriter
from browser_use_agent.audit.checkpoints import (
    CheckpointSettings,
    CheckpointWriter,
    fingerprint_observation,
)
from browser_use_agent.audit.model_calls import ModelCallWriter
from browser_use_agent.audit.screenshots import (
    ScreenshotResult,
    ScreenshotSettings,
    ScreenshotWriter,
    decide_screenshot,
)
from browser_use_agent.audit.writer import AuditWriter


class AsyncAuditWriter:
    """Run the synchronous audit implementation through an async session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, *args: Any, **kwargs: Any) -> Any:
        return await self.session.run_sync(
            lambda sync: AuditWriter(sync).append(*args, **kwargs)
        )


class AsyncModelCallWriter:
    """Async adapter for :class:`ModelCallWriter`."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.session = session
        self.artifact_store = artifact_store

    async def record(self, **kwargs: Any) -> Any:
        return await self.session.run_sync(
            lambda sync: ModelCallWriter(
                sync,
                artifact_store=self.artifact_store,
            ).record(**kwargs)
        )


class AsyncBrowserActionWriter:
    """Async adapter for :class:`BrowserActionWriter`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, **kwargs: Any) -> Any:
        return await self.session.run_sync(
            lambda sync: BrowserActionWriter(sync).record(**kwargs)
        )


class AsyncCheckpointWriter:
    """Async-session adapter that preserves checkpoint policy state."""

    def __init__(
        self,
        session: AsyncSession,
        store: ArtifactStore,
        *,
        settings: CheckpointSettings,
    ) -> None:
        self.session = session
        self.store = store
        self.settings = settings
        self._last_fingerprint = None
        self._steps_since_checkpoint = 0

    async def __call__(
        self,
        run_id: Any,
        step_id: Any,
        observation: Any,
        force_reason: str | None = None,
    ) -> Any:
        def write(sync: Any) -> Any:
            writer = CheckpointWriter(
                AuditWriter(sync),
                self.store,
                session=sync,
                settings=self.settings,
                commit=sync.commit,
            )
            writer._last_fingerprint = self._last_fingerprint
            writer._steps_since_checkpoint = self._steps_since_checkpoint
            result = writer.maybe_checkpoint(
                run_id,
                step_id,
                observation,
                force_reason=force_reason,
            )
            self._last_fingerprint = writer._last_fingerprint
            self._steps_since_checkpoint = writer._steps_since_checkpoint
            return result

        return await self.session.run_sync(write)


class AsyncScreenshotWriter:
    """Async-session adapter for screenshot artifact metadata and audit."""

    def __init__(
        self,
        session: AsyncSession,
        store: ArtifactStore,
        *,
        capture: Any,
        settings: ScreenshotSettings,
    ) -> None:
        self.session = session
        self.store = store
        self.capture = capture
        self.settings = settings
        self._last_fingerprint = None
        self._steps_since_screenshot = 0

    async def __call__(
        self,
        run_id: Any,
        step_id: Any,
        observation: Any,
        force_reason: str | None = None,
    ) -> Any:
        self._steps_since_screenshot += 1
        current = fingerprint_observation(observation)
        decision = decide_screenshot(
            self._last_fingerprint,
            current,
            steps_since_screenshot=self._steps_since_screenshot,
            settings=self.settings,
            force_reason=force_reason,
        )
        if not decision.should_capture or decision.reason is None:
            return ScreenshotResult(reason=None, skipped=True)

        raw = self.capture()
        if inspect.isawaitable(raw):
            raw = await raw
        if not isinstance(raw, (bytes, bytearray)):
            raise RuntimeError("screenshot capture must return bytes")

        def write(sync: Any) -> Any:
            sync_writer = ScreenshotWriter(
                AuditWriter(sync),
                self.store,
                settings=self.settings,
                session=sync,
                commit=sync.commit,
            )
            sync_writer._last_fingerprint = self._last_fingerprint
            sync_writer._steps_since_screenshot = self._steps_since_screenshot - 1
            result = sync_writer.maybe_screenshot_sync(
                run_id,
                step_id,
                observation,
                force_reason=force_reason,
                image_bytes=raw,
            )
            self._last_fingerprint = sync_writer._last_fingerprint
            self._steps_since_screenshot = sync_writer._steps_since_screenshot
            return result

        return await self.session.run_sync(write)
