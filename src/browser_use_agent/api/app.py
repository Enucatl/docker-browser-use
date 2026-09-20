"""FastAPI application factory for the agent controller."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.api.events_bus import get_event_bus
from browser_use_agent.api.routes.runs import router as runs_router
from browser_use_agent.api.ws import router as ws_router
from browser_use_agent.audit.writer import set_append_hook
from browser_use_agent.config import AppSettings, load_app_settings
from browser_use_agent.db.engine import create_engine_from_settings
from browser_use_agent.db.models import AgentEvent


def _bridge_audit_to_event_bus(event: AgentEvent) -> None:
    """Publish a redacted audit append onto the in-process run event bus.

    Args:
        event: Flushed :class:`~browser_use_agent.db.models.AgentEvent` row.
    """
    get_event_bus().publish_event(event, source="live")


def create_app(
    settings: AppSettings | None = None,
    *,
    engine: Engine | None = None,
) -> FastAPI:
    """Build the controller FastAPI app.

    Args:
        settings: Optional preloaded settings; loads from the environment when omitted.
        engine: Optional SQLAlchemy engine (tests inject a migrated test engine).

    Returns:
        Configured :class:`~fastapi.FastAPI` instance.
    """
    resolved = settings if settings is not None else load_app_settings()
    app = FastAPI(title="browser-use agent controller", version="0.1.0")
    app.state.settings = resolved
    app.state.event_bus = get_event_bus()
    set_append_hook(_bridge_audit_to_event_bus)

    db_engine = engine
    if db_engine is None and resolved.database is not None:
        db_engine = create_engine_from_settings(resolved.database)

    if db_engine is not None:
        app.state.engine = db_engine
        app.state.session_factory = sessionmaker(
            bind=db_engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )
    else:
        app.state.engine = None
        app.state.session_factory = None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness probe for Docker healthchecks (no sensitive data)."""
        return {"status": "ok"}

    app.include_router(runs_router)
    app.include_router(ws_router)
    return app
