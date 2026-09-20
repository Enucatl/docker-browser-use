"""FastAPI application factory for the agent controller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from browser_use_agent.agent.worker import RunWorker, load_run_worker_settings
from browser_use_agent.api.auth import RemoteUserAuthMiddleware
from browser_use_agent.api.csrf import CsrfOriginMiddleware
from browser_use_agent.api.events_bus import get_event_bus
from browser_use_agent.api.routes.runs import router as runs_router
from browser_use_agent.api.ws import router as ws_router
from browser_use_agent.audit.writer import AuditWriter, set_append_hook
from browser_use_agent.browser.session import BrowserSessionManager
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Detach Browser Use on shutdown without killing Chromium."""
        yield
        manager: BrowserSessionManager | None = getattr(app.state, "browser_session_manager", None)
        if manager is not None:
            await manager.shutdown()

    app = FastAPI(title="browser-use agent controller", version="0.1.0", lifespan=lifespan)
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

    app.state.browser_session_manager = BrowserSessionManager(
        resolved.browser,
        audit_factory=AuditWriter if db_engine is not None else None,
        session_factory=app.state.session_factory,
    )
    if app.state.session_factory is not None:
        app.state.run_worker = RunWorker(
            app.state.session_factory,
            settings=load_run_worker_settings(),
            browser_manager=app.state.browser_session_manager,
        )
    else:
        app.state.run_worker = None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness probe for Docker healthchecks (no sensitive data)."""
        return {"status": "ok"}

    app.include_router(runs_router)
    app.include_router(ws_router)

    # Middleware is applied outermost-last: TrustedHost → CSRF → Remote-User → routes.
    app.add_middleware(RemoteUserAuthMiddleware, auth_required=resolved.auth_required)
    app.add_middleware(
        CsrfOriginMiddleware,
        trusted_origins=resolved.csrf_trusted_origins,
        enforce=resolved.auth_required,
    )
    # Host checks only in prod-like mode so local TestClient (Host: testserver) works.
    if resolved.auth_required and resolved.allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(resolved.allowed_hosts),
        )

    return app
