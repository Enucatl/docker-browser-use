"""Writer for normalized ``model_calls`` rows linked to ``agent_events``."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.payloads import (
    DEFAULT_INLINE_LIMIT_BYTES,
    maybe_offload_json,
    redact_optional_text,
)
from browser_use_agent.db.models import AgentEvent, ModelCall


class ModelCallWriter:
    """Persist forensic model-call traces against an existing audit event.

    Always redacts request/response metadata before insert. Large payloads may
    be offloaded to the artifact store when one is configured.

    Attributes:
        session: SQLAlchemy session (caller owns the transaction).
        artifact_store: Optional store for oversized prompt/response bodies.
        inline_limit_bytes: Size threshold for artifact offload.
    """

    def __init__(
        self,
        session: Session,
        *,
        artifact_store: FilesystemArtifactStore | None = None,
        inline_limit_bytes: int = DEFAULT_INLINE_LIMIT_BYTES,
    ) -> None:
        """Bind the writer to a session and optional artifact store.

        Args:
            session: Active SQLAlchemy session.
            artifact_store: When set, large JSON blobs become artifacts.
            inline_limit_bytes: UTF-8 byte threshold for offloading.
        """
        self.session = session
        self.artifact_store = artifact_store
        self.inline_limit_bytes = inline_limit_bytes

    def record(
        self,
        *,
        event: AgentEvent,
        call_kind: str,
        provider: str | None = None,
        model_name: str | None = None,
        status: str | None = "ok",
        retries: int | None = None,
        request_id: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int | None = None,
        cost_usd: Decimal | float | None = None,
        request_meta: Mapping[str, Any] | None = None,
        response_meta: Mapping[str, Any] | None = None,
        row_id: uuid.UUID | None = None,
    ) -> ModelCall:
        """Insert one redacted ``model_calls`` row linked to ``event``.

        Args:
            event: Source ``agent_events`` row (must already be flushed).
            call_kind: Logical caller (``jev``, ``text_llm``, …).
            provider: Provider name when known.
            model_name: Model / version string.
            status: ``ok`` or ``failed``.
            retries: Prior retry count for this logical call.
            request_id: Provider or HTTP request id.
            prompt_tokens: Optional input token count.
            completion_tokens: Optional output token count.
            latency_ms: End-to-end latency.
            cost_usd: Estimated or billed USD cost.
            request_meta: Full redacted request (prompt, state, questions, …).
            response_meta: Full redacted response (raw/parsed, candidates, …).
            row_id: Optional primary key; a new UUID is generated otherwise.

        Returns:
            The inserted :class:`~browser_use_agent.db.models.ModelCall`.

        Raises:
            ValueError: When ``call_kind`` is empty.
        """
        if not call_kind:
            raise ValueError("call_kind must be a non-empty string")

        req_meta, req_art = maybe_offload_json(
            request_meta if request_meta is not None else {},
            session=self.session,
            store=self.artifact_store,
            run_id=event.run_id,
            event_id=event.id,
            kind="model_request",
            inline_limit_bytes=self.inline_limit_bytes,
        )
        resp_meta, resp_art = maybe_offload_json(
            response_meta if response_meta is not None else {},
            session=self.session,
            store=self.artifact_store,
            run_id=event.run_id,
            event_id=event.id,
            kind="model_response",
            inline_limit_bytes=self.inline_limit_bytes,
        )

        cost: Decimal | None
        if cost_usd is None:
            cost = None
        elif isinstance(cost_usd, Decimal):
            cost = cost_usd
        else:
            cost = Decimal(str(cost_usd))

        row = ModelCall(
            id=row_id or uuid.uuid4(),
            run_id=event.run_id,
            event_id=event.id,
            step_id=event.step_id,
            event_seq=int(event.seq) if event.seq is not None else None,
            provider=redact_optional_text(provider),
            model_name=redact_optional_text(model_name),
            call_kind=call_kind,
            status=status,
            retries=retries,
            request_id=redact_optional_text(request_id),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms if latency_ms is not None else event.duration_ms,
            cost_usd=cost,
            request_meta=req_meta,
            response_meta=resp_meta,
            request_artifact_id=req_art,
            response_artifact_id=resp_art,
        )
        self.session.add(row)
        self.session.flush()
        return row
