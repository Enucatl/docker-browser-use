"""WebSocket run event streaming (replay + live AuditWriter bridge)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from browser_use_agent.api.auth import accept_websocket_identity
from browser_use_agent.api.csrf import check_websocket_origin
from browser_use_agent.api.events_bus import (
    RunEventMessage,
    get_event_bus,
    message_from_event,
)
from browser_use_agent.config import AppSettings
from browser_use_agent.db.models import AgentEvent, Run

router = APIRouter(tags=["runs-ws"])

# Default how many historical events to replay on connect.
DEFAULT_REPLAY_LIMIT = 200
MAX_REPLAY_LIMIT = 500


def _session_factory(websocket: WebSocket) -> async_sessionmaker[AsyncSession]:
    """Return the app session factory or raise if DB is not configured.

    Args:
        websocket: Active WebSocket with ``app.state``.

    Returns:
        Bound SQLAlchemy ``sessionmaker``.

    Raises:
        RuntimeError: When the app has no database engine.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        websocket.app.state, "async_session_factory", None
    )
    if factory is None:
        raise RuntimeError("Database is not configured; set DATABASE_HOST and related vars")
    return factory


async def _load_replay_events(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    after_seq: int,
    limit: int,
) -> list[AgentEvent]:
    """Load historical redacted events for reconnect replay.

    Args:
        session: Open SQLAlchemy session.
        run_id: Run to load.
        after_seq: Exclusive lower bound on ``seq`` (0 = from the start).
        limit: Maximum rows to return.

    Returns:
        Events ordered by ascending ``seq``.
    """
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.run_id == run_id, AgentEvent.seq > after_seq)
        .order_by(AgentEvent.seq.asc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


@router.websocket("/api/runs/{run_id}/events")
async def run_events_ws(
    websocket: WebSocket,
    run_id: uuid.UUID,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    replay_limit: Annotated[int, Query(ge=1, le=MAX_REPLAY_LIMIT)] = DEFAULT_REPLAY_LIMIT,
) -> None:
    """Stream audit/progress events for a run (replay then live).

    Query params:

    - ``after_seq``: exclusive lower bound; use last seen ``seq`` on reconnect.
    - ``replay_limit``: max historical events before switching to live.

    Auth: see :mod:`browser_use_agent.api.auth` and ``docs/ws-events.md``.
    """
    settings: AppSettings = websocket.app.state.settings
    if not check_websocket_origin(
        websocket,
        trusted_origins=settings.csrf_trusted_origins,
        enforce=settings.auth_required,
    ):
        await websocket.close(code=4403, reason="Origin not trusted")
        return

    identity = await accept_websocket_identity(websocket, auth_required=settings.auth_required)
    if identity is None:
        return

    factory = _session_factory(websocket)
    bus = get_event_bus()
    # Subscribe before DB replay so live appends during reconnect are not missed;
    # duplicates are filtered by ``last_seq`` below.
    queue = bus.subscribe(run_id)
    last_seq = after_seq

    session = factory()
    try:
        run = await session.get(Run, run_id)
        if run is None:
            await websocket.send_json(
                RunEventMessage(
                    type="error",
                    run_id=run_id,
                    detail=f"run {run_id} does not exist",
                ).to_dict()
            )
            await websocket.close(code=4404, reason="Run not found")
            bus.unsubscribe(run_id, queue)
            return

        replay = await _load_replay_events(session, run_id, after_seq=after_seq, limit=replay_limit)
    finally:
        await session.close()

    try:
        await websocket.send_json(
            RunEventMessage(
                type="replay_start",
                run_id=run_id,
                detail=f"identity={identity.username}",
            ).to_dict()
        )
        for event in replay:
            msg = message_from_event(event, source="replay")
            await websocket.send_json(msg.to_dict())
            if msg.seq is not None:
                last_seq = msg.seq

        await websocket.send_json(
            RunEventMessage(
                type="replay_end",
                run_id=run_id,
                seq=last_seq,
            ).to_dict()
        )

        while True:
            for message in bus.drain(queue):
                if message.seq is not None and message.seq <= last_seq:
                    continue
                await websocket.send_json(message.to_dict())
                if message.seq is not None:
                    last_seq = message.seq
            try:
                # Detect client disconnect without blocking forever on recv.
                await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
            except TimeoutError:
                await asyncio.sleep(0.05)
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        return
    finally:
        bus.unsubscribe(run_id, queue)
