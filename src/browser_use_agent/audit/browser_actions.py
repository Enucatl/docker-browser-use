"""Writer for normalized ``browser_actions`` rows linked to ``agent_events``."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from browser_use_agent.audit.payloads import redact_mapping, redact_optional_text
from browser_use_agent.db.models import AgentEvent, BrowserAction


class BrowserActionWriter:
    """Persist forensic browser-action traces against an existing audit event.

    Always redacts ``metadata`` / text fields before insert. Large before/after
    state blobs belong in the artifact store (T018); this writer only records
    optional artifact id references.

    Attributes:
        session: SQLAlchemy session (caller owns the transaction).
    """

    def __init__(self, session: Session) -> None:
        """Bind the writer to a database session.

        Args:
            session: Active SQLAlchemy session.
        """
        self.session = session

    def record(
        self,
        *,
        event: AgentEvent,
        action_type: str,
        status: str,
        target: str | None = None,
        url: str | None = None,
        title: str | None = None,
        tab_id: str | None = None,
        element_index: int | None = None,
        page_changed: bool | None = None,
        result: str | None = None,
        duration_ms: int | None = None,
        before_artifact_id: uuid.UUID | None = None,
        after_artifact_id: uuid.UUID | None = None,
        metadata: Mapping[str, Any] | None = None,
        row_id: uuid.UUID | None = None,
    ) -> BrowserAction:
        """Insert one redacted ``browser_actions`` row linked to ``event``.

        Preferred ``metadata`` keys (when available): ``attributes``,
        ``accessible_name``, ``role``, ``bounds``, ``browser_use_ids``,
        ``params``, ``error``.

        Args:
            event: Source ``agent_events`` row (must already be flushed).
            action_type: Action verb/kind (e.g. ``CLICK``).
            status: ``requested``, ``completed``, or ``failed``.
            target: Semantic target description (never a secret value).
            url: Page URL when relevant.
            title: Document title when known.
            tab_id: Browser tab identifier.
            element_index: Snapshot-local interactable index.
            page_changed: Whether URL/title changed after the action.
            result: Short outcome summary.
            duration_ms: Action duration; falls back to ``event.duration_ms``.
            before_artifact_id: Optional pre-action state artifact.
            after_artifact_id: Optional post-action state artifact.
            metadata: Extra searchable forensic fields.
            row_id: Optional primary key; a new UUID is generated otherwise.

        Returns:
            The inserted :class:`~browser_use_agent.db.models.BrowserAction`.

        Raises:
            ValueError: When ``action_type`` or ``status`` is empty.
        """
        if not action_type:
            raise ValueError("action_type must be a non-empty string")
        if not status:
            raise ValueError("status must be a non-empty string")

        safe_url = redact_optional_text(url if url is not None else event.url)
        safe_tab = tab_id if tab_id is not None else event.tab_id
        meta = redact_mapping(metadata)

        row = BrowserAction(
            id=row_id or uuid.uuid4(),
            run_id=event.run_id,
            event_id=event.id,
            step_id=event.step_id,
            event_seq=int(event.seq) if event.seq is not None else None,
            action_type=action_type,
            status=status,
            target=redact_optional_text(target),
            url=safe_url,
            title=redact_optional_text(title),
            tab_id=safe_tab,
            element_index=element_index,
            page_changed=page_changed,
            result=redact_optional_text(result),
            duration_ms=duration_ms if duration_ms is not None else event.duration_ms,
            before_artifact_id=before_artifact_id,
            after_artifact_id=after_artifact_id,
            metadata_=meta,
        )
        self.session.add(row)
        self.session.flush()
        return row
