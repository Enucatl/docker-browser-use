"""Compressed browser-state checkpoints on meaningful transitions (T018).

Full model-visible observations are redacted, serialized as Zstd JSON, and
stored in the artifact store. ``agent_events`` rows stay small and only
reference the artifact id / digest.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from browser_use_agent.artifacts.store import ArtifactStore
from browser_use_agent.artifacts.zstd import zstd_decode_json, zstd_encode_json
from browser_use_agent.audit.writer import AuditAppend
from browser_use_agent.policy.actions import BrowserObservation
from browser_use_agent.security.redaction import redact_for_audit

logger = logging.getLogger(__name__)

# Bump when the checkpoint envelope shape changes (T032 eval harness).
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_ARTIFACT_KIND = "browser_state"
CHECKPOINT_MEDIA_TYPE = "application/zstd+json"
CHECKPOINT_EVENT_TYPE = "browser_state_checkpoint"

# Default: treat candidate-set Jaccard distance at or above this as "major".
DEFAULT_DOM_CHANGE_RATIO = 0.25
DEFAULT_INTERVAL_STEPS = 10


class CheckpointReason(StrEnum):
    """Why a checkpoint was written."""

    FIRST = "first"
    URL_CHANGE = "url_change"
    TITLE_CHANGE = "title_change"
    DOM_HASH_CHANGE = "dom_hash_change"
    BROWSER_ERROR = "browser_error"
    APPROVAL = "approval"
    ERROR = "error"
    PERIODIC = "periodic"
    FORCE = "force"


@dataclass(frozen=True, slots=True)
class CheckpointSettings:
    """Configurable checkpoint policy.

    Attributes:
        enabled: When False, never write checkpoints.
        interval_steps: Write at least every N observations (``0`` disables).
        dom_change_ratio: Minimum Jaccard distance of candidate signatures to
            treat a DOM hash change as major (0-1).
    """

    enabled: bool = True
    interval_steps: int = DEFAULT_INTERVAL_STEPS
    dom_change_ratio: float = DEFAULT_DOM_CHANGE_RATIO


def load_checkpoint_settings() -> CheckpointSettings:
    """Load checkpoint policy from the environment.

    Environment variables:

    * ``CHECKPOINT_ENABLED`` — ``1``/``true``/``yes``/``on`` (default true).
    * ``CHECKPOINT_INTERVAL_STEPS`` — periodic interval (default 10; ``0`` off).
    * ``CHECKPOINT_DOM_CHANGE_RATIO`` — major DOM change threshold (default 0.25).

    Returns:
        Immutable policy settings.
    """
    enabled = _env_bool("CHECKPOINT_ENABLED", True)
    try:
        interval = int(os.environ.get("CHECKPOINT_INTERVAL_STEPS", str(DEFAULT_INTERVAL_STEPS)))
    except ValueError:
        interval = DEFAULT_INTERVAL_STEPS
    interval = max(0, interval)
    try:
        ratio = float(os.environ.get("CHECKPOINT_DOM_CHANGE_RATIO", str(DEFAULT_DOM_CHANGE_RATIO)))
    except ValueError:
        ratio = DEFAULT_DOM_CHANGE_RATIO
    ratio = min(1.0, max(0.0, ratio))
    return CheckpointSettings(enabled=enabled, interval_steps=interval, dom_change_ratio=ratio)


@dataclass(frozen=True, slots=True)
class StateFingerprint:
    """Compact fingerprint used to detect meaningful observation changes.

    Attributes:
        url: Page URL.
        title: Document title.
        dom_hash: SHA-256 of the structural candidate set.
        candidate_signatures: Stable per-candidate signatures for Jaccard.
        browser_error_count: Number of recorded browser/page errors.
    """

    url: str
    title: str
    dom_hash: str
    candidate_signatures: frozenset[str]
    browser_error_count: int


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    """Outcome of the meaningful-change heuristic.

    Attributes:
        should_write: Whether a checkpoint should be stored.
        reason: Primary reason when ``should_write`` is True.
    """

    should_write: bool
    reason: CheckpointReason | None = None


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Result of writing one checkpoint.

    Attributes:
        reason: Why the checkpoint was taken.
        artifact_id: Metadata row id when a DB session was provided.
        storage_key: Content-addressed blob key.
        sha256: Digest of the compressed payload.
        size_bytes: Compressed payload size.
        event_id: Audit event id for the small checkpoint breadcrumb.
        skipped: True when the policy decided not to write.
    """

    reason: CheckpointReason | None
    artifact_id: uuid.UUID | None = None
    storage_key: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    event_id: uuid.UUID | None = None
    skipped: bool = False


def candidate_signature(candidate: Mapping[str, Any] | Any) -> str:
    """Build a stable structural signature for one candidate element.

    Args:
        candidate: :class:`CandidateElement` or a mapping with the same fields.

    Returns:
        Pipe-delimited signature string (no secret field values).
    """
    if isinstance(candidate, Mapping):
        index = candidate.get("index", "")
        tag = candidate.get("tag") or ""
        role = candidate.get("role") or ""
        name = candidate.get("name") or ""
        href = candidate.get("href") or ""
        input_type = candidate.get("input_type") or ""
        editable = int(bool(candidate.get("is_editable")))
        password = int(bool(candidate.get("is_password_field")))
    else:
        index = getattr(candidate, "index", "")
        tag = getattr(candidate, "tag", None) or ""
        role = getattr(candidate, "role", None) or ""
        name = getattr(candidate, "name", None) or ""
        href = getattr(candidate, "href", None) or ""
        input_type = getattr(candidate, "input_type", None) or ""
        editable = int(bool(getattr(candidate, "is_editable", False)))
        password = int(bool(getattr(candidate, "is_password_field", False)))
    return f"{index}|{tag}|{role}|{name}|{href}|{input_type}|{editable}|{password}"


def compute_dom_hash(observation: BrowserObservation) -> str:
    """Hash the structural candidate set of an observation.

    Args:
        observation: Browser observation to fingerprint.

    Returns:
        Lowercase hex SHA-256 digest.
    """
    lines = [candidate_signature(c) for c in observation.candidates]
    raw = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def fingerprint_observation(observation: BrowserObservation) -> StateFingerprint:
    """Derive a :class:`StateFingerprint` from an observation.

    Args:
        observation: Current browser observation.

    Returns:
        Immutable fingerprint for change detection.
    """
    signatures = frozenset(candidate_signature(c) for c in observation.candidates)
    return StateFingerprint(
        url=observation.url or "",
        title=observation.title or "",
        dom_hash=compute_dom_hash(observation),
        candidate_signatures=signatures,
        browser_error_count=len(observation.browser_errors),
    )


def jaccard_distance(a: frozenset[str], b: frozenset[str]) -> float:
    """Return Jaccard distance between two signature sets.

    Args:
        a: Previous candidate signatures.
        b: Current candidate signatures.

    Returns:
        Distance in ``[0, 1]`` (``0`` identical, ``1`` disjoint). Empty union empty is 0.
    """
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    intersection = a & b
    return 1.0 - (len(intersection) / len(union))


def decide_checkpoint(
    previous: StateFingerprint | None,
    current: StateFingerprint,
    *,
    steps_since_checkpoint: int,
    settings: CheckpointSettings,
    force_reason: str | None = None,
) -> CheckpointDecision:
    """Apply the meaningful-change heuristic.

    Checkpoints on: first observation, URL/title change, major DOM hash change,
    new browser errors, approval/error force reasons, and periodic intervals.

    Args:
        previous: Fingerprint of the last checkpointed observation, or ``None``.
        current: Fingerprint of the latest observation.
        steps_since_checkpoint: Observations since the last write (inclusive of
            the current one when counting toward the interval).
        settings: Policy tunables.
        force_reason: Optional override (``approval``, ``error``, or free text).

    Returns:
        Whether to write and the primary reason.
    """
    if not settings.enabled:
        return CheckpointDecision(should_write=False)

    if force_reason:
        normalized = force_reason.strip().lower()
        if normalized == CheckpointReason.APPROVAL.value:
            return CheckpointDecision(True, CheckpointReason.APPROVAL)
        if normalized == CheckpointReason.ERROR.value:
            return CheckpointDecision(True, CheckpointReason.ERROR)
        return CheckpointDecision(True, CheckpointReason.FORCE)

    if previous is None:
        return CheckpointDecision(True, CheckpointReason.FIRST)

    if current.url != previous.url:
        return CheckpointDecision(True, CheckpointReason.URL_CHANGE)

    if current.title != previous.title:
        return CheckpointDecision(True, CheckpointReason.TITLE_CHANGE)

    if current.dom_hash != previous.dom_hash:
        distance = jaccard_distance(previous.candidate_signatures, current.candidate_signatures)
        if distance >= settings.dom_change_ratio:
            return CheckpointDecision(True, CheckpointReason.DOM_HASH_CHANGE)

    if current.browser_error_count > previous.browser_error_count:
        return CheckpointDecision(True, CheckpointReason.BROWSER_ERROR)

    if settings.interval_steps > 0 and steps_since_checkpoint >= settings.interval_steps:
        return CheckpointDecision(True, CheckpointReason.PERIODIC)

    return CheckpointDecision(should_write=False)


def build_checkpoint_payload(
    observation: BrowserObservation,
    *,
    run_id: uuid.UUID,
    step_id: uuid.UUID,
    reason: CheckpointReason,
    fingerprint: StateFingerprint,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-versioned checkpoint envelope (pre-redaction).

    Args:
        observation: Full observation to archive.
        run_id: Owning run id.
        step_id: Step grouping id.
        reason: Checkpoint reason.
        fingerprint: Fingerprint recorded with the payload.
        extra: Optional extra context (Jev I/O, retry metadata, …).

    Returns:
        JSON-serializable envelope including ``schema_version``.
    """
    payload: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": CHECKPOINT_ARTIFACT_KIND,
        "reason": reason.value,
        "run_id": str(run_id),
        "step_id": str(step_id),
        "captured_at": datetime.now(UTC).isoformat(),
        "fingerprint": {
            "url": fingerprint.url,
            "title": fingerprint.title,
            "dom_hash": fingerprint.dom_hash,
            "browser_error_count": fingerprint.browser_error_count,
            "candidate_count": len(fingerprint.candidate_signatures),
        },
        "observation": observation.model_dump(mode="json"),
    }
    if extra:
        payload["extra"] = dict(extra)
    return payload


def serialize_checkpoint(payload: Mapping[str, Any]) -> bytes:
    """Redact then Zstd-compress a checkpoint envelope.

    Args:
        payload: Unredacted checkpoint mapping.

    Returns:
        Zstd-compressed JSON bytes suitable for the artifact store.

    Raises:
        ValueError: If redaction does not yield a mapping.
    """
    redacted = redact_for_audit(dict(payload))
    if not isinstance(redacted, dict):
        raise ValueError("checkpoint redaction must produce a mapping")
    # Preserve schema_version even if redaction nested oddly.
    redacted.setdefault("schema_version", payload.get("schema_version", CHECKPOINT_SCHEMA_VERSION))
    return zstd_encode_json(redacted)


def load_checkpoint_payload(data: bytes) -> dict[str, Any]:
    """Decompress and validate a checkpoint artifact blob.

    Args:
        data: Zstd-compressed JSON bytes from the artifact store.

    Returns:
        Parsed checkpoint envelope.

    Raises:
        ValueError: If the payload is not a mapping or has an unknown version.
    """
    parsed = zstd_decode_json(data)
    if not isinstance(parsed, dict):
        raise ValueError("checkpoint payload must be a JSON object")
    version = parsed.get("schema_version")
    if version != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported checkpoint schema_version {version!r}; "
            f"expected {CHECKPOINT_SCHEMA_VERSION}"
        )
    return parsed


def observation_from_checkpoint(payload: Mapping[str, Any]) -> BrowserObservation:
    """Rebuild a :class:`BrowserObservation` from a checkpoint envelope.

    Args:
        payload: Decompressed checkpoint mapping.

    Returns:
        Validated observation suitable for Jev replay (T032).

    Raises:
        ValueError: If ``observation`` is missing or invalid.
        KeyError: If the ``observation`` key is absent.
    """
    raw = payload["observation"]
    if not isinstance(raw, dict):
        raise ValueError("checkpoint observation must be a mapping")
    return BrowserObservation.model_validate(raw)


class CheckpointWriter:
    """Decide, serialize, store, and audit browser-state checkpoints.

    Call as ``writer(run_id, step_id, observation, force_reason=None)`` from the
    agent loop. Unchanged observations are skipped (except forced / periodic).

    Attributes:
        audit: Audit append sink for the small breadcrumb event.
        store: Artifact store for compressed payloads.
        session: Optional SQLAlchemy session for ``artifacts`` metadata rows.
        settings: Checkpoint policy.
    """

    def __init__(
        self,
        audit: AuditAppend,
        store: ArtifactStore,
        *,
        session: Session | None = None,
        settings: CheckpointSettings | None = None,
        commit: Any | None = None,
    ) -> None:
        """Create a checkpoint writer.

        Args:
            audit: Audit writer (must redact; :class:`AuditWriter` does).
            store: Filesystem artifact store.
            session: Optional DB session for artifact metadata.
            settings: Policy; loads from the environment when omitted.
            commit: Optional callback after a successful write (e.g. commit).
        """
        self.audit = audit
        self.store = store
        self.session = session
        self.settings = settings if settings is not None else load_checkpoint_settings()
        self._commit = commit
        self._last_fingerprint: StateFingerprint | None = None
        self._steps_since_checkpoint = 0

    def __call__(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        force_reason: str | None = None,
    ) -> None:
        """Evaluate and optionally write a checkpoint (sync CheckpointHook).

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Latest browser observation.
            force_reason: Optional force reason (``approval``, ``error``, …).
        """
        self.maybe_checkpoint(
            run_id,
            step_id,
            observation,
            force_reason=force_reason,
        )

    def maybe_checkpoint(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        *,
        force_reason: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> CheckpointResult:
        """Write a checkpoint when the policy says the change is meaningful.

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Latest browser observation.
            force_reason: Optional force reason from approval/error boundaries.
            extra: Optional archival context attached under ``extra``.

        Returns:
            Write result, or a skipped result when nothing meaningful changed.
        """
        self._steps_since_checkpoint += 1
        current = fingerprint_observation(observation)
        decision = decide_checkpoint(
            self._last_fingerprint,
            current,
            steps_since_checkpoint=self._steps_since_checkpoint,
            settings=self.settings,
            force_reason=force_reason,
        )
        if not decision.should_write or decision.reason is None:
            return CheckpointResult(reason=None, skipped=True)

        return self._write(
            run_id=run_id,
            step_id=step_id,
            observation=observation,
            fingerprint=current,
            reason=decision.reason,
            extra=extra,
        )

    def load_by_storage_key(self, storage_key: str) -> dict[str, Any]:
        """Load and validate a checkpoint blob by storage key.

        Args:
            storage_key: Content-addressed key from a prior put.

        Returns:
            Parsed checkpoint envelope.
        """
        return load_checkpoint_payload(self.store.get(storage_key))

    def _write(
        self,
        *,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        observation: BrowserObservation,
        fingerprint: StateFingerprint,
        reason: CheckpointReason,
        extra: Mapping[str, Any] | None,
    ) -> CheckpointResult:
        """Serialize, store, and audit one checkpoint.

        Args:
            run_id: Owning run id.
            step_id: Step grouping id.
            observation: Observation to archive.
            fingerprint: Current fingerprint.
            reason: Checkpoint reason.
            extra: Optional extra context.

        Returns:
            Write result with artifact references.
        """
        envelope = build_checkpoint_payload(
            observation,
            run_id=run_id,
            step_id=step_id,
            reason=reason,
            fingerprint=fingerprint,
            extra=extra,
        )
        compressed = serialize_checkpoint(envelope)
        put = self.store.put(
            compressed,
            media_type=CHECKPOINT_MEDIA_TYPE,
            kind=CHECKPOINT_ARTIFACT_KIND,
            run_id=run_id,
            metadata={
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "reason": reason.value,
                "dom_hash": fingerprint.dom_hash,
                "step_id": str(step_id),
            },
            session=self.session,
        )

        event_payload = {
            "artifact_id": str(put.artifact_id) if put.artifact_id is not None else None,
            "storage_key": put.storage_key,
            "sha256": put.sha256,
            "size_bytes": put.size_bytes,
            "reason": reason.value,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "dom_hash": fingerprint.dom_hash,
            "url": fingerprint.url,
            "title": fingerprint.title,
            "candidate_count": len(observation.candidates),
        }
        event = self.audit.append(
            run_id,
            CHECKPOINT_EVENT_TYPE,
            event_payload,
            actor="agent",
            step_id=step_id,
            url=observation.url or None,
        )
        if self._commit is not None:
            self._commit()

        self._last_fingerprint = fingerprint
        self._steps_since_checkpoint = 0
        event_id = getattr(event, "id", None)
        logger.debug(
            "Checkpoint written run=%s reason=%s key=%s",
            run_id,
            reason.value,
            put.storage_key,
        )
        return CheckpointResult(
            reason=reason,
            artifact_id=put.artifact_id,
            storage_key=put.storage_key,
            sha256=put.sha256,
            size_bytes=put.size_bytes,
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
