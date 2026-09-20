"""In-process pub/sub bridging AuditWriter appends to WebSocket clients."""

from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from queue import Empty, SimpleQueue
from typing import Any

from browser_use_agent.db.models import AgentEvent

# Bound WS JSON metadata size; large state belongs in artifacts (T007).
MAX_WS_METADATA_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class RunEventMessage:
    """JSON-serializable run progress message for WebSocket clients.

    Attributes:
        type: Message kind (``event``, ``replay_start``, ``replay_end``, ``error``).
        run_id: Owning run id.
        seq: Per-run sequence when ``type`` is ``event``.
        event_id: Audit event primary key when applicable.
        event_type: Logical audit event kind when applicable.
        actor: Event actor when applicable.
        occurred_at: Event timestamp (ISO-8601) when applicable.
        payload: Redacted metadata (possibly truncated).
        source: ``replay`` for historical rows, ``live`` for bus publishes.
        truncated: True when payload was size-capped.
        detail: Optional human-readable note for control/error messages.
    """

    type: str
    run_id: uuid.UUID
    seq: int | None = None
    event_id: uuid.UUID | None = None
    event_type: str | None = None
    actor: str | None = None
    occurred_at: str | None = None
    payload: Mapping[str, Any] | None = None
    source: str | None = None
    truncated: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dictionary (omits null optional fields).

        Returns:
            Dict suitable for ``WebSocket.send_json``.
        """
        data: dict[str, Any] = {"type": self.type, "run_id": str(self.run_id)}
        if self.seq is not None:
            data["seq"] = self.seq
        if self.event_id is not None:
            data["event_id"] = str(self.event_id)
        if self.event_type is not None:
            data["event_type"] = self.event_type
        if self.actor is not None:
            data["actor"] = self.actor
        if self.occurred_at is not None:
            data["occurred_at"] = self.occurred_at
        if self.payload is not None:
            data["payload"] = dict(self.payload)
        if self.source is not None:
            data["source"] = self.source
        if self.truncated:
            data["truncated"] = True
        if self.detail is not None:
            data["detail"] = self.detail
        return data


def _iso_utc(value: datetime) -> str:
    """Format a datetime as UTC ISO-8601 text."""
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def bound_payload(metadata: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Return metadata safe for WS, truncating oversized JSON.

    Prefer keeping artifact id / sha256 keys so clients can fetch large state
    from the artifact store instead of receiving DOM dumps inline.

    Args:
        metadata: Redacted audit metadata (may be empty).

    Returns:
        A ``(payload, truncated)`` pair.
    """
    raw: dict[str, Any] = dict(metadata) if metadata is not None else {}
    encoded = json.dumps(raw, default=str, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_WS_METADATA_BYTES:
        return raw, False

    slim: dict[str, Any] = {"_truncated": True, "_original_keys": sorted(raw.keys())}
    for key, value in raw.items():
        key_l = key.lower()
        if (
            key_l in {"artifact_id", "artifact_ids", "sha256", "storage_key"}
            or key_l.endswith("_artifact_id")
            or "artifact" in key_l
        ):
            slim[key] = value
    return slim, True


def message_from_event(event: AgentEvent, *, source: str) -> RunEventMessage:
    """Build a WS message from a persisted (already redacted) audit event.

    Args:
        event: ``agent_events`` row after AuditWriter append.
        source: ``replay`` or ``live``.

    Returns:
        Client-facing :class:`RunEventMessage`.
    """
    payload, truncated = bound_payload(event.metadata_)
    return RunEventMessage(
        type="event",
        run_id=event.run_id,
        seq=int(event.seq),
        event_id=event.id,
        event_type=event.event_type,
        actor=event.actor,
        occurred_at=_iso_utc(event.occurred_at),
        payload=payload,
        source=source,
        truncated=truncated,
    )


class RunEventBus:
    """Thread-safe in-process fan-out of run events to subscriber queues.

    One process / worker only. Multi-replica deployments would need
    Postgres LISTEN/NOTIFY or an external broker (out of scope for T011).

    Attributes:
        None publicly mutable; use :meth:`subscribe` / :meth:`publish`.
    """

    def __init__(self) -> None:
        """Create an empty subscriber registry."""
        self._lock = threading.Lock()
        self._subscribers: dict[uuid.UUID, list[SimpleQueue[RunEventMessage]]] = defaultdict(list)

    def subscribe(self, run_id: uuid.UUID) -> SimpleQueue[RunEventMessage]:
        """Register a new queue for live events on ``run_id``.

        Args:
            run_id: Run to follow.

        Returns:
            Queue that receives :class:`RunEventMessage` instances.
        """
        queue: SimpleQueue[RunEventMessage] = SimpleQueue()
        with self._lock:
            self._subscribers[run_id].append(queue)
        return queue

    def unsubscribe(self, run_id: uuid.UUID, queue: SimpleQueue[RunEventMessage]) -> None:
        """Remove a previously registered subscriber queue.

        Args:
            run_id: Run the queue was subscribed to.
            queue: Queue returned by :meth:`subscribe`.
        """
        with self._lock:
            subscribers = self._subscribers.get(run_id)
            if not subscribers:
                return
            try:
                subscribers.remove(queue)
            except ValueError:
                return
            if not subscribers:
                del self._subscribers[run_id]

    def publish(self, message: RunEventMessage) -> None:
        """Fan out a message to all subscribers of its run.

        Args:
            message: Event or control message (typically ``type=event``).
        """
        with self._lock:
            subscribers = list(self._subscribers.get(message.run_id, ()))
        for queue in subscribers:
            queue.put(message)

    def publish_event(self, event: AgentEvent, *, source: str = "live") -> None:
        """Publish an audit row as a live (or tagged) WS event message.

        Args:
            event: Persisted redacted audit event.
            source: Usually ``live`` for bus publishes.
        """
        self.publish(message_from_event(event, source=source))

    def drain(
        self,
        queue: SimpleQueue[RunEventMessage],
        *,
        max_items: int = 64,
    ) -> list[RunEventMessage]:
        """Non-blocking drain of up to ``max_items`` queued messages.

        Args:
            queue: Subscriber queue.
            max_items: Cap per poll to keep the WS loop responsive.

        Returns:
            Messages in arrival order (may be empty).
        """
        items: list[RunEventMessage] = []
        for _ in range(max_items):
            try:
                items.append(queue.get_nowait())
            except Empty:
                break
        return items


_bus = RunEventBus()


def get_event_bus() -> RunEventBus:
    """Return the process-wide run event bus.

    Returns:
        Shared :class:`RunEventBus` instance.
    """
    return _bus


def reset_event_bus_for_tests() -> RunEventBus:
    """Replace the process bus (tests only).

    Returns:
        The new empty bus instance.
    """
    global _bus
    _bus = RunEventBus()
    return _bus
