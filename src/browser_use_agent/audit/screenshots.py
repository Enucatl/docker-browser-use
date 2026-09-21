"""Event-driven screenshot capture as compressed image artifacts (T019).

Screenshots are stored in the artifact store (never in Postgres). Capture is
aligned with important transitions — first page, URL/title changes, approvals,
errors, destructive actions, and periodic heartbeats — not every action.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import Any

from sqlalchemy.orm import Session

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.checkpoints import StateFingerprint, fingerprint_observation
from browser_use_agent.audit.writer import AuditAppend
from browser_use_agent.policy.actions import ActionKind, BrowserObservation

logger = logging.getLogger(__name__)

SCREENSHOT_ARTIFACT_KIND = "screenshot"
SCREENSHOT_EVENT_TYPE = "screenshot_captured"

DEFAULT_QUALITY = 80
DEFAULT_INTERVAL_STEPS = 20

# Actions treated as high-impact for forced capture (not every CLICK/SCROLL).
DESTRUCTIVE_ACTION_KINDS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.NAVIGATE,
        ActionKind.BITWARDEN_LOGIN,
        ActionKind.BITWARDEN_IDENTITY,
        ActionKind.BITWARDEN_CARD,
    }
)

ScreenshotCaptureFn = Callable[[], Awaitable[bytes] | bytes]


class ScreenshotFormat(StrEnum):
    """On-disk image encoding for routine screenshots."""

    WEBP = "webp"
    JPEG = "jpeg"


class ScreenshotReason(StrEnum):
    """Why a screenshot was captured."""

    FIRST = "first"
    URL_CHANGE = "url_change"
    TITLE_CHANGE = "title_change"
    APPROVAL = "approval"
    ERROR = "error"
    DESTRUCTIVE = "destructive"
    PERIODIC = "periodic"
    FORCE = "force"


@dataclass(frozen=True, slots=True)
class ScreenshotSettings:
    """Configurable screenshot capture policy.

    Attributes:
        enabled: When False, never capture screenshots.
        format: WebP or JPEG encoding for stored blobs.
        quality: Encoder quality 1-100 (higher is larger / sharper).
        interval_steps: Heartbeat interval in observations (``0`` disables).
    """

    enabled: bool = True
    format: ScreenshotFormat = ScreenshotFormat.WEBP
    quality: int = DEFAULT_QUALITY
    interval_steps: int = DEFAULT_INTERVAL_STEPS


def load_screenshot_settings() -> ScreenshotSettings:
    """Load screenshot policy from the environment.

    Environment variables:

    * ``SCREENSHOT_ENABLED`` — ``1``/``true``/``yes``/``on`` (default true).
    * ``SCREENSHOT_FORMAT`` — ``webp`` or ``jpeg`` (default ``webp``).
    * ``SCREENSHOT_QUALITY`` — integer 1-100 (default 80).
    * ``SCREENSHOT_INTERVAL_STEPS`` — heartbeat interval (default 20; ``0`` off).

    Returns:
        Immutable policy settings.
    """
    enabled = _env_bool("SCREENSHOT_ENABLED", True)
    raw_fmt = os.environ.get("SCREENSHOT_FORMAT", ScreenshotFormat.WEBP.value).strip().lower()
    try:
        fmt = ScreenshotFormat(raw_fmt)
    except ValueError:
        fmt = ScreenshotFormat.WEBP
    try:
        quality = int(os.environ.get("SCREENSHOT_QUALITY", str(DEFAULT_QUALITY)))
    except ValueError:
        quality = DEFAULT_QUALITY
    quality = min(100, max(1, quality))
    try:
        interval = int(os.environ.get("SCREENSHOT_INTERVAL_STEPS", str(DEFAULT_INTERVAL_STEPS)))
    except ValueError:
        interval = DEFAULT_INTERVAL_STEPS
    interval = max(0, interval)
    return ScreenshotSettings(
        enabled=enabled,
        format=fmt,
        quality=quality,
        interval_steps=interval,
    )


@dataclass(frozen=True, slots=True)
class ScreenshotDecision:
    """Outcome of the screenshot capture heuristic.

    Attributes:
        should_capture: Whether a screenshot should be stored.
        reason: Primary reason when ``should_capture`` is True.
    """

    should_capture: bool
    reason: ScreenshotReason | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotResult:
    """Result of capturing and storing one screenshot.

    Attributes:
        reason: Why the screenshot was taken.
        artifact_id: Metadata row id when a DB session was provided.
        storage_key: Content-addressed blob key.
        sha256: Digest of the encoded image bytes.
        size_bytes: Encoded payload size.
        media_type: MIME type of the stored image.
        event_id: Audit event id for the small screenshot breadcrumb.
        skipped: True when the policy decided not to capture.
    """

    reason: ScreenshotReason | None
    artifact_id: uuid.UUID | None = None
    storage_key: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    media_type: str | None = None
    event_id: uuid.UUID | None = None
    skipped: bool = False


def is_destructive_action(kind: ActionKind | str) -> bool:
    """Return whether an action kind warrants a forced screenshot.

    Args:
        kind: Action kind enum or string value.

    Returns:
        True for navigate / Bitwarden kinds.
    """
    if isinstance(kind, ActionKind):
        return kind in DESTRUCTIVE_ACTION_KINDS
    try:
        return ActionKind(str(kind)) in DESTRUCTIVE_ACTION_KINDS
    except ValueError:
        return False


def media_type_for(fmt: ScreenshotFormat) -> str:
    """Return the MIME type for a screenshot format.

    Args:
        fmt: Target encoding.

    Returns:
        ``image/webp`` or ``image/jpeg``.
    """
    if fmt == ScreenshotFormat.JPEG:
        return "image/jpeg"
    return "image/webp"


def encode_screenshot(
    raw: bytes,
    *,
    fmt: ScreenshotFormat = ScreenshotFormat.WEBP,
    quality: int = DEFAULT_QUALITY,
) -> tuple[bytes, str]:
    """Encode image bytes as WebP or JPEG and strip EXIF metadata.

    Accepts any Pillow-readable input (PNG, JPEG, WebP, …). Output never
    carries EXIF; RGBA is flattened for JPEG.

    Args:
        raw: Source image bytes (fake or CDP capture).
        fmt: Target encoding.
        quality: Encoder quality 1-100.

    Returns:
        ``(encoded_bytes, media_type)``.

    Raises:
        ValueError: If ``raw`` is empty or not a recognizable image.
    """
    if not raw:
        raise ValueError("screenshot bytes must be non-empty")
    quality = min(100, max(1, quality))
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - pillow is a declared dep
        raise RuntimeError("Pillow is required for screenshot encoding") from exc

    try:
        with Image.open(BytesIO(raw)) as img:
            # Load pixels then drop any EXIF / ancillary chunks by rebuilding.
            img.load()
            working = img.convert("RGBA") if img.mode in {"P", "LA"} else img.copy()
            if fmt == ScreenshotFormat.JPEG:
                if working.mode in {"RGBA", "LA"}:
                    background = Image.new("RGB", working.size, (255, 255, 255))
                    alpha = working.split()[-1] if working.mode == "RGBA" else None
                    rgb = working.convert("RGB")
                    if alpha is not None:
                        background.paste(rgb, mask=alpha)
                        working = background
                    else:
                        working = rgb
                elif working.mode != "RGB":
                    working = working.convert("RGB")
                out = BytesIO()
                working.save(out, format="JPEG", quality=quality, optimize=True)
                return out.getvalue(), media_type_for(fmt)

            if working.mode not in {"RGB", "RGBA"}:
                working = working.convert("RGBA")
            out = BytesIO()
            working.save(out, format="WEBP", quality=quality, method=4)
            return out.getvalue(), media_type_for(fmt)
    except Exception as exc:
        raise ValueError(f"unrecognized or corrupt screenshot bytes: {exc}") from exc


def decide_screenshot(
    previous: StateFingerprint | None,
    current: StateFingerprint,
    *,
    steps_since_screenshot: int,
    settings: ScreenshotSettings,
    force_reason: str | None = None,
) -> ScreenshotDecision:
    """Apply the event-driven screenshot heuristic.

    Captures on: first observation, URL/title change, approval/error/destructive
    force reasons, and periodic heartbeats. Deliberately skips routine DOM-only
    churn so screenshots are not taken on every action.

    Args:
        previous: Fingerprint of the last captured observation, or ``None``.
        current: Fingerprint of the latest observation.
        steps_since_screenshot: Observations since the last capture (inclusive
            of the current one when counting toward the interval).
        settings: Policy tunables.
        force_reason: Optional override (``approval``, ``error``,
            ``destructive``, or free text).

    Returns:
        Whether to capture and the primary reason.
    """
    if not settings.enabled:
        return ScreenshotDecision(should_capture=False)

    if force_reason:
        normalized = force_reason.strip().lower()
        if normalized == ScreenshotReason.APPROVAL.value:
            return ScreenshotDecision(True, ScreenshotReason.APPROVAL)
        if normalized == ScreenshotReason.ERROR.value:
            return ScreenshotDecision(True, ScreenshotReason.ERROR)
        if normalized == ScreenshotReason.DESTRUCTIVE.value:
            return ScreenshotDecision(True, ScreenshotReason.DESTRUCTIVE)
        return ScreenshotDecision(True, ScreenshotReason.FORCE)

    if previous is None:
        return ScreenshotDecision(True, ScreenshotReason.FIRST)

    if current.url != previous.url:
        return ScreenshotDecision(True, ScreenshotReason.URL_CHANGE)

    if current.title != previous.title:
        return ScreenshotDecision(True, ScreenshotReason.TITLE_CHANGE)

    if settings.interval_steps > 0 and steps_since_screenshot >= settings.interval_steps:
        return ScreenshotDecision(True, ScreenshotReason.PERIODIC)

    return ScreenshotDecision(should_capture=False)


class ScreenshotWriter:
    """Decide, encode, store, and audit event-driven screenshots.

    Call as ``await writer(run_id, step_id, observation, force_reason=None)``
    from the agent loop, or pass ``image_bytes`` in tests to avoid Chrome.

    Attributes:
        audit: Audit append sink for the small breadcrumb event.
        store: Artifact store for encoded image blobs.
        capture: Async/sync callable returning raw image bytes when needed.
        session: Optional SQLAlchemy session for ``artifacts`` metadata rows.
        settings: Screenshot policy.
    """

    def __init__(
        self,
        audit: AuditAppend,
        store: FilesystemArtifactStore,
        *,
        capture: ScreenshotCaptureFn | None = None,
        session: Session | None = None,
        settings: ScreenshotSettings | None = None,
        commit: Any | None = None,
    ) -> None:
        """Create a screenshot writer.

        Args:
            audit: Audit writer (must redact; :class:`AuditWriter` does).
            store: Filesystem artifact store.
            capture: Optional raw-image provider (Browser Use / CDP / fake).
            session: Optional DB session for artifact metadata.
            settings: Policy; loads from the environment when omitted.
            commit: Optional callback after a successful write (e.g. commit).
        """
        self.audit = audit
        self.store = store
        self.capture = capture
        self.session = session
        self.settings = settings if settings is not None else load_screenshot_settings()
        self._commit = commit
        self._last_fingerprint: StateFingerprint | None = None
        self._steps_since_screenshot = 0

    async def __call__(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        force_reason: str | None = None,
    ) -> None:
        """Evaluate and optionally capture a screenshot (async ScreenshotHook).

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Latest browser observation.
            force_reason: Optional force reason (``approval``, ``error``, …).
        """
        await self.maybe_screenshot(
            run_id,
            step_id,
            observation,
            force_reason=force_reason,
        )

    async def maybe_screenshot(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        *,
        force_reason: str | None = None,
        image_bytes: bytes | None = None,
    ) -> ScreenshotResult:
        """Capture when the policy says the transition is important.

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Latest browser observation (for fingerprint + URL).
            force_reason: Optional force reason from approval/error/destructive.
            image_bytes: Optional pre-captured bytes (unit tests / fakes).

        Returns:
            Write result, or a skipped result when capture is not warranted.

        Raises:
            RuntimeError: When capture is required but no bytes/provider exist.
            ValueError: When image bytes cannot be encoded.
        """
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

        raw = image_bytes
        if raw is None:
            raw = await self._capture_raw()
        return self._write(
            run_id=run_id,
            step_id=step_id,
            observation=observation,
            fingerprint=current,
            reason=decision.reason,
            raw=raw,
        )

    def maybe_screenshot_sync(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        *,
        force_reason: str | None = None,
        image_bytes: bytes,
    ) -> ScreenshotResult:
        """Synchronous capture path for tests that supply fake image bytes.

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Latest browser observation.
            force_reason: Optional force reason.
            image_bytes: Fake or pre-captured image bytes (required).

        Returns:
            Write result, or a skipped result when capture is not warranted.
        """
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
        return self._write(
            run_id=run_id,
            step_id=step_id,
            observation=observation,
            fingerprint=current,
            reason=decision.reason,
            raw=image_bytes,
        )

    async def _capture_raw(self) -> bytes:
        """Invoke the configured capture provider.

        Returns:
            Raw image bytes from Browser Use / CDP / fake.

        Raises:
            RuntimeError: When no capture provider is configured.
        """
        if self.capture is None:
            raise RuntimeError(
                "ScreenshotWriter requires image_bytes or a capture provider",
            )
        result = self.capture()
        if isinstance(result, Awaitable):
            data = await result
        else:
            data = result
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("screenshot capture must return bytes")
        return bytes(data)

    def _write(
        self,
        *,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        fingerprint: StateFingerprint,
        reason: ScreenshotReason,
        raw: bytes,
    ) -> ScreenshotResult:
        """Encode, store, and audit one screenshot.

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Observation context for the audit URL.
            fingerprint: Current fingerprint.
            reason: Capture reason.
            raw: Source image bytes.

        Returns:
            Write result with artifact references.
        """
        encoded, media_type = encode_screenshot(
            raw,
            fmt=self.settings.format,
            quality=self.settings.quality,
        )
        put = self.store.put(
            encoded,
            media_type=media_type,
            kind=SCREENSHOT_ARTIFACT_KIND,
            run_id=run_id,
            metadata={
                "reason": reason.value,
                "format": self.settings.format.value,
                "quality": self.settings.quality,
                "step_id": str(step_id),
                "url": fingerprint.url,
                "title": fingerprint.title,
            },
            session=self.session,
        )

        event_payload = {
            "artifact_id": str(put.artifact_id) if put.artifact_id is not None else None,
            "storage_key": put.storage_key,
            "sha256": put.sha256,
            "size_bytes": put.size_bytes,
            "media_type": media_type,
            "reason": reason.value,
            "format": self.settings.format.value,
            "quality": self.settings.quality,
            "url": fingerprint.url,
            "title": fingerprint.title,
        }
        event = self.audit.append(
            run_id,
            SCREENSHOT_EVENT_TYPE,
            event_payload,
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
        )
        if self._commit is not None:
            self._commit()

        self._last_fingerprint = fingerprint
        self._steps_since_screenshot = 0
        event_id = getattr(event, "id", None)
        logger.debug(
            "Screenshot written run=%s reason=%s key=%s",
            run_id,
            reason.value,
            put.storage_key,
        )
        return ScreenshotResult(
            reason=reason,
            artifact_id=put.artifact_id,
            storage_key=put.storage_key,
            sha256=put.sha256,
            size_bytes=put.size_bytes,
            media_type=media_type,
            event_id=event_id if isinstance(event_id, uuid.UUID) else None,
            skipped=False,
        )


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset.

    Returns:
        Parsed boolean.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
