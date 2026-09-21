"""HTML routes for the minimal Agent Web UI (T024)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from browser_use_agent.agent.worker import RunWorker
from browser_use_agent.api.deps import CurrentUser, get_session
from browser_use_agent.api.events_bus import bound_payload
from browser_use_agent.audit.costs import recent_cost_summary, run_cost
from browser_use_agent.browser.profiles import get_profile, list_profiles
from browser_use_agent.db.models import AgentEvent, HumanApproval, Run
from browser_use_agent.runs.status import TERMINAL_STATUSES, RunStatus
from browser_use_agent.services import approvals as approval_service
from browser_use_agent.services import runs as run_service
from browser_use_agent.services import takeover as takeover_service
from browser_use_agent.web.paths import STATIC_DIR, TEMPLATES_DIR

router = APIRouter(tags=["web-ui"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# How many audit events to seed into the run page before the WS stream.
_BOOTSTRAP_EVENT_LIMIT = 100
_HISTORY_LIMIT = 50


def mount_web_ui(app: Any) -> None:
    """Mount HTML routes and static assets on the FastAPI app.

    Args:
        app: FastAPI application instance.
    """
    app.include_router(router)
    static_path = Path(STATIC_DIR)
    if static_path.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


def _worker(request: Request) -> RunWorker | None:
    """Return the app run worker when configured.

    Args:
        request: Current request.

    Returns:
        :class:`RunWorker` or ``None``.
    """
    return getattr(request.app.state, "run_worker", None)


def _iso(value: datetime | None) -> str | None:
    """Format a datetime for templates.

    Args:
        value: Timestamp or ``None``.

    Returns:
        ISO-8601 text, or ``None``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def _event_row(event: AgentEvent) -> dict[str, Any]:
    """Map an audit event to a template/JSON-friendly dict.

    Args:
        event: Persisted agent event.

    Returns:
        Serializable event summary.
    """
    payload, truncated = bound_payload(event.metadata_)
    return {
        "seq": int(event.seq),
        "event_type": event.event_type,
        "actor": event.actor,
        "occurred_at": _iso(event.occurred_at),
        "payload": payload,
        "truncated": truncated,
    }


async def _run_context(
    run: Run,
    session: AsyncSession,
    *,
    pending: HumanApproval | None = None,
) -> dict[str, Any]:
    """Build template context fields for one run.

    Args:
        run: Persisted run row.
        session: Open SQLAlchemy session for cost totals.
        pending: Optional pending human approval row.

    Returns:
        Context dict with status flags and control availability.
    """
    try:
        status_enum = RunStatus(run.status)
    except ValueError:
        status_enum = None

    terminal = status_enum in TERMINAL_STATUSES if status_enum is not None else False
    awaiting_approval = run.status == RunStatus.AWAITING_APPROVAL.value
    awaiting_human = run.status == RunStatus.AWAITING_HUMAN.value
    is_paused = run.status == RunStatus.PAUSED.value
    is_running = run.status == RunStatus.RUNNING.value
    is_queued = run.status == RunStatus.QUEUED.value

    return {
        "id": str(run.id),
        "goal": run.goal,
        "status": run.status,
        "profile_id": run.profile_id,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "cost": await session.run_sync(lambda sync: run_cost(sync, run.id)),
        "terminal": terminal,
        "awaiting_approval": awaiting_approval,
        "awaiting_human": awaiting_human,
        "can_pause": is_running,
        "can_resume": is_paused,
        "can_cancel": not terminal,
        "can_approve": awaiting_approval,
        "can_take_control": is_running or is_paused,
        "can_release_control": awaiting_human,
        "is_active": is_queued or is_running or is_paused or awaiting_approval or awaiting_human,
        "pending_approval": (
            {
                "id": str(pending.id),
                "reason": pending.reason,
                "reason_code": (pending.metadata_ or {}).get("reason_code"),
                "metadata": dict(pending.metadata_ or {}),
                "created_at": _iso(pending.created_at),
            }
            if pending is not None
            else None
        ),
    }


async def _load_events(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Load recent audit events for bootstrap display.

    Args:
        session: Open SQLAlchemy session.
        run_id: Run to load.

    Returns:
        Event dicts ordered by ascending ``seq``.
    """
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.run_id == run_id)
        .order_by(AgentEvent.seq.desc())
        .limit(_BOOTSTRAP_EVENT_LIMIT)
    )
    rows = list(reversed(list((await session.scalars(stmt)).all())))
    return [_event_row(event) for event in rows]


def _flash_redirect(url: str, *, error: str | None = None) -> RedirectResponse:
    """Build a 303 redirect, optionally carrying an error query param.

    Args:
        url: Destination path.
        error: Optional short error message.

    Returns:
        Redirect response.
    """
    if error:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}error={quote(error, safe='')}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> Response:
    """Render new-run form and recent run history."""
    runs = await session.run_sync(lambda sync: run_service.list_runs(sync, limit=_HISTORY_LIMIT))
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "runs": [await _run_context(run, session) for run in runs],
            "profiles": list_profiles(settings=request.app.state.settings.browser),
            "default_profile": request.app.state.settings.browser.default_profile,
            "error": request.query_params.get("error"),
        },
    )


@router.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> Response:
    """Render run history list (same data as home, focused view)."""
    runs = await session.run_sync(lambda sync: run_service.list_runs(sync, limit=_HISTORY_LIMIT))
    summary = await session.run_sync(lambda sync: recent_cost_summary(sync))
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "runs": [await _run_context(run, session) for run in runs],
            "cost_summary": summary,
        },
    )


@router.post("/runs", response_class=HTMLResponse)
async def create_run_form(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
    goal: Annotated[str, Form()],
    profile_id: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Create a run from the new-run form and start the worker."""
    del user  # Identity enforced by middleware; actor is Authelia Remote-User.
    stripped = goal.strip()
    if not stripped:
        return _flash_redirect("/", error="Goal is required")

    try:
        profile = get_profile(profile_id, settings=request.app.state.settings.browser)
    except KeyError as exc:
        return _flash_redirect("/", error=str(exc))
    run = await session.run_sync(
        lambda sync: run_service.create_run(sync, stripped, profile_id=profile.id)
    )
    await session.commit()

    worker = _worker(request)
    if worker is not None:
        await worker.start_run(run.id)

    return RedirectResponse(
        url=f"/runs/{run.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> Response:
    """Render active/detail run page with controls and live event stream."""
    try:
        run = await session.get(Run, run_id)
        if run is None:
            raise run_service.RunNotFoundError(f"run {run_id} does not exist")
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    pending = await session.run_sync(
        lambda sync: approval_service.get_pending_approval(sync, run_id)
    )
    events = await _load_events(session, run_id)
    ctx = await _run_context(run, session, pending=pending)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "user": user,
            "run": ctx,
            "events": events,
            "events_json": json.dumps(events),
            "error": request.query_params.get("error"),
            "vnc_url": "/vnc/",
        },
    )


async def _control_action(
    request: Request,
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    action: str,
    actor: str,
    reason: str | None = None,
) -> RedirectResponse:
    """Apply a run control / approval / takeover action and redirect.

    Args:
        request: Current request (for worker access).
        session: DB session.
        run_id: Target run.
        action: Action name (``pause``, ``approve``, …).
        actor: Authelia username.
        reason: Optional approval note.

    Returns:
        Redirect to the run detail page.
    """
    dest = f"/runs/{run_id}"
    try:
        if action == "pause":
            await session.run_sync(lambda sync: run_service.pause_run(sync, run_id))
        elif action == "resume":
            await session.run_sync(lambda sync: run_service.resume_run(sync, run_id))
        elif action == "cancel":
            await session.run_sync(lambda sync: run_service.cancel_run(sync, run_id))
        elif action == "approve":
            await session.run_sync(
                lambda sync: approval_service.approve_run(sync, run_id, actor=actor, reason=reason)
            )
        elif action == "reject":
            await session.run_sync(
                lambda sync: approval_service.reject_run(sync, run_id, actor=actor, reason=reason)
            )
        elif action == "take-control":
            await session.run_sync(
                lambda sync: takeover_service.take_control(sync, run_id, actor=actor)
            )
        elif action == "release-control":
            await session.run_sync(
                lambda sync: takeover_service.release_control(sync, run_id, actor=actor)
            )
        elif action == "retry":
            run = await session.run_sync(lambda sync: run_service.retry_run(sync, run_id))
            await session.commit()
            if run.status == RunStatus.QUEUED.value:
                worker = _worker(request)
                if worker is not None:
                    await worker.start_run(run_id)
        else:
            return _flash_redirect(dest, error="Unknown action")
    except run_service.RunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from None
    except run_service.RunControlError as exc:
        return _flash_redirect(dest, error=str(exc))

    return RedirectResponse(url=dest, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/runs/{run_id}/pause")
async def ui_pause(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Pause the run from the Web UI."""
    return await _control_action(request, session, run_id, action="pause", actor=user.username)


@router.post("/runs/{run_id}/resume")
async def ui_resume(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Resume the run from the Web UI."""
    return await _control_action(request, session, run_id, action="resume", actor=user.username)


@router.post("/runs/{run_id}/cancel")
async def ui_cancel(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Cancel the run from the Web UI."""
    return await _control_action(request, session, run_id, action="cancel", actor=user.username)


@router.post("/runs/{run_id}/approve")
async def ui_approve(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
    reason: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Approve a pending high-impact action from the Web UI."""
    note = reason.strip() if reason else None
    return await _control_action(
        request,
        session,
        run_id,
        action="approve",
        actor=user.username,
        reason=note or None,
    )


@router.post("/runs/{run_id}/reject")
async def ui_reject(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
    reason: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Reject a pending high-impact action from the Web UI."""
    note = reason.strip() if reason else None
    return await _control_action(
        request,
        session,
        run_id,
        action="reject",
        actor=user.username,
        reason=note or None,
    )


@router.post("/runs/{run_id}/take-control")
async def ui_take_control(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Hand the browser to the operator from the Web UI."""
    return await _control_action(
        request,
        session,
        run_id,
        action="take-control",
        actor=user.username,
    )


@router.post("/runs/{run_id}/release-control")
async def ui_release_control(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Return control to the agent from the Web UI."""
    return await _control_action(
        request,
        session,
        run_id,
        action="release-control",
        actor=user.username,
    )


@router.post("/runs/{run_id}/retry")
async def ui_retry(
    request: Request,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RedirectResponse:
    """Retry a failed run from the Web UI."""
    return await _control_action(request, session, run_id, action="retry", actor=user.username)
