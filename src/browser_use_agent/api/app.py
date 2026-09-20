"""FastAPI application factory for the agent controller."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.api.routes.runs import router as runs_router
from browser_use_agent.config import AppSettings, load_app_settings
from browser_use_agent.db.engine import create_engine_from_settings


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
    return app
