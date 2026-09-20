"""RunWorker: one asyncio task per run with a global concurrency cap."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.agent.browser_port import BrowserPort, BrowserUsePort
from browser_use_agent.agent.loop import (
    AgentLoop,
    CheckpointHook,
    LoopOutcome,
    NeedsApprovalHook,
    default_needs_approval,
)
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.browser.session import BrowserSessionManager
from browser_use_agent.db.models import Run
from browser_use_agent.policy.jev_adapter import JevAdapter
from browser_use_agent.policy.jev_client import (
    FakeJevClient,
    HttpJevClient,
    JevClient,
    load_jev_client_settings,
)
from browser_use_agent.policy.text_llm import (
    OpenAICompatibleTextLLMClient,
    TextLLMClient,
    load_text_llm_settings,
)
from browser_use_agent.runs.status import TERMINAL_STATUSES, RunStatus
from browser_use_agent.services.runs import get_run

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
JevFactory = Callable[[], JevClient]
TextLLMFactory = Callable[[], TextLLMClient | None]


@dataclass(frozen=True, slots=True)
class RunWorkerSettings:
    """Tunables for the run worker pool.

    Attributes:
        max_concurrent_runs: Global cap on in-flight worker tasks.
        max_steps: Per-run observe→decide→execute cap.
    """

    max_concurrent_runs: int = 1
    max_steps: int = 50


def load_run_worker_settings() -> RunWorkerSettings:
    """Load worker settings from the environment.

    Uses ``AGENT_MAX_CONCURRENT_RUNS`` (default 1) and ``AGENT_MAX_STEPS``
    (default 50).

    Returns:
        Immutable worker settings.
    """
    concurrent_raw = os.environ.get("AGENT_MAX_CONCURRENT_RUNS", "1")
    steps_raw = os.environ.get("AGENT_MAX_STEPS", "50")
    try:
        concurrent = max(1, int(concurrent_raw))
    except ValueError:
        concurrent = 1
    try:
        steps = max(1, int(steps_raw))
    except ValueError:
        steps = 50
    return RunWorkerSettings(max_concurrent_runs=concurrent, max_steps=steps)


def default_jev_factory() -> JevClient:
    """Build a Jev client from environment settings.

    Returns ``HttpJevClient`` when an API key is configured; otherwise a
    :class:`FakeJevClient` that immediately returns ``DONE`` (safe for local
    smoke without credentials).

    Returns:
        Configured Jev client.
    """
    settings = load_jev_client_settings()
    if settings.api_key:
        return HttpJevClient(settings)
    return FakeJevClient()


def default_text_llm_factory() -> TextLLMClient | None:
    """Build a text LLM client when ``TEXT_LLM_API_KEY`` / ``*_FILE`` is set.

    Returns:
        :class:`OpenAICompatibleTextLLMClient` when configured; otherwise
        ``None`` (``TYPE_TEXT`` without pre-filled text fails closed).
    """
    settings = load_text_llm_settings()
    if not settings.api_key:
        return None
    return OpenAICompatibleTextLLMClient(settings)


class RunWorker:
    """Orchestrates agent loops: one task per run, global concurrency cap.

    Started when a run enters ``running``. Cooperative cancellation reads the
    run's persisted status between loop phases (T020 will add richer controls).

    Attributes:
        settings: Concurrency and step caps.
        session_factory: SQLAlchemy session factory for audit / status.
        browser_manager: Optional Browser Use session manager for live Chrome.
        jev_factory: Builds a Jev client per run.
        text_llm_factory: Builds an optional text LLM client per run (T016).
        needs_approval: Approval hook (default False).
        checkpoint: Optional T018 checkpoint hook.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        settings: RunWorkerSettings | None = None,
        browser_manager: BrowserSessionManager | None = None,
        jev_factory: JevFactory | None = None,
        text_llm_factory: TextLLMFactory | None = None,
        browser_port_factory: Callable[[uuid.UUID, Session], BrowserPort] | None = None,
        needs_approval: NeedsApprovalHook | None = None,
        checkpoint: CheckpointHook | None = None,
        adapter: JevAdapter | None = None,
    ) -> None:
        """Create a run worker.

        Args:
            session_factory: Callable returning a new SQLAlchemy session.
            settings: Optional worker settings; loaded from env when omitted.
            browser_manager: Live browser manager (required unless a port factory
                is injected for tests).
            jev_factory: Optional Jev client factory.
            text_llm_factory: Optional text LLM factory (T016).
            browser_port_factory: Optional test hook that returns a BrowserPort.
            needs_approval: Approval gate hook.
            checkpoint: Optional checkpoint hook.
            adapter: Optional shared Jev adapter.
        """
        self.settings = settings if settings is not None else load_run_worker_settings()
        self.session_factory = session_factory
        self.browser_manager = browser_manager
        self.jev_factory = jev_factory or default_jev_factory
        self.text_llm_factory = text_llm_factory or default_text_llm_factory
        self.browser_port_factory = browser_port_factory
        self.needs_approval = needs_approval or default_needs_approval
        self.checkpoint = checkpoint
        self.adapter = adapter if adapter is not None else JevAdapter()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_runs)
        self._tasks: dict[uuid.UUID, asyncio.Task[LoopOutcome]] = {}
        self._lock = asyncio.Lock()

    @property
    def active_run_ids(self) -> frozenset[uuid.UUID]:
        """Return run ids with an in-flight worker task."""
        return frozenset(self._tasks)

    async def start_run(self, run_id: uuid.UUID) -> asyncio.Task[LoopOutcome]:
        """Mark ``run_id`` running (if needed) and spawn its worker task.

        One worker task per run. Duplicate starts return the existing task.

        Args:
            run_id: Run to execute.

        Returns:
            asyncio Task that resolves to the loop outcome.

        Raises:
            RunNotFoundError: When the run does not exist.
            RuntimeError: When the run is already terminal.
        """
        async with self._lock:
            existing = self._tasks.get(run_id)
            if existing is not None and not existing.done():
                return existing

            session = self.session_factory()
            try:
                run = get_run(session, run_id)
                try:
                    status = RunStatus(run.status)
                except ValueError as exc:
                    raise RuntimeError(f"unknown run status {run.status!r}") from exc
                if status in TERMINAL_STATUSES:
                    raise RuntimeError(f"run {run_id} is already terminal ({status.value})")
                if status != RunStatus.RUNNING:
                    now = datetime.now(UTC)
                    run.status = RunStatus.RUNNING.value
                    run.started_at = run.started_at or now
                    run.updated_at = now
                    session.commit()
            finally:
                session.close()

            task = asyncio.create_task(self._guarded_run(run_id), name=f"run-worker-{run_id}")
            self._tasks[run_id] = task

            def _cleanup(done: asyncio.Task[LoopOutcome]) -> None:
                self._tasks.pop(run_id, None)
                try:
                    done.result()
                except asyncio.CancelledError:
                    logger.info("Worker task for run %s cancelled", run_id)
                except Exception:
                    logger.exception("Worker task for run %s failed", run_id)

            task.add_done_callback(_cleanup)
            return task

    async def _guarded_run(self, run_id: uuid.UUID) -> LoopOutcome:
        """Acquire the global semaphore and execute one run.

        Args:
            run_id: Run to execute.

        Returns:
            Loop outcome after status persistence.
        """
        async with self._semaphore:
            return await self._execute_run(run_id)

    async def _execute_run(self, run_id: uuid.UUID) -> LoopOutcome:
        """Acquire browser (optional), run the loop, persist terminal status.

        Args:
            run_id: Run to execute.

        Returns:
            Final :class:`LoopOutcome`.
        """
        session = self.session_factory()
        browser_held = False
        try:
            run = get_run(session, run_id)
            goal = run.goal
            profile_id = run.profile_id
            audit = AuditWriter(session)

            if self.browser_port_factory is not None:
                browser = self.browser_port_factory(run_id, session)
            else:
                if self.browser_manager is None:
                    raise RuntimeError("RunWorker requires browser_manager or browser_port_factory")
                bu_session = await self.browser_manager.acquire_for_run(
                    run_id,
                    profile_name=profile_id,
                    db_session=session,
                )
                session.commit()
                browser_held = True
                browser = BrowserUsePort(bu_session)

            def is_cancelled() -> bool:
                """Re-read run status for cooperative cancel (T020)."""
                check = self.session_factory()
                try:
                    row = check.get(Run, run_id)
                    if row is None:
                        return True
                    return row.status == RunStatus.CANCELLED.value
                finally:
                    check.close()

            loop = AgentLoop(
                run_id=run_id,
                goal=goal,
                browser=browser,
                jev=self.jev_factory(),
                text_llm=self.text_llm_factory(),
                audit=audit,
                adapter=self.adapter,
                max_steps=self.settings.max_steps,
                needs_approval=self.needs_approval,
                checkpoint=self.checkpoint,
                is_cancelled=is_cancelled,
                commit=session.commit,
            )
            outcome = await loop.run()
            self._persist_outcome(session, run_id, outcome)
            return outcome
        except Exception as exc:
            logger.exception("Run %s aborted", run_id)
            try:
                AuditWriter(session).append(
                    run_id,
                    "run_failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                    actor="system",
                )
                self._persist_outcome(
                    session,
                    run_id,
                    LoopOutcome(status=RunStatus.FAILED, message=str(exc)),
                )
            except Exception:
                session.rollback()
                logger.exception("Failed to persist abort status for run %s", run_id)
            raise
        finally:
            if browser_held and self.browser_manager is not None:
                try:
                    await self.browser_manager.release(run_id, db_session=session)
                    session.commit()
                except Exception:
                    logger.exception("Failed to release browser for run %s", run_id)
            session.close()

    def _persist_outcome(self, session: Session, run_id: uuid.UUID, outcome: LoopOutcome) -> None:
        """Write terminal / waiting status onto the run row.

        Args:
            session: Active SQLAlchemy session.
            run_id: Run primary key.
            outcome: Loop result to persist.
        """
        run = session.get(Run, run_id)
        if run is None:
            return
        # Do not overwrite an operator cancel that raced the loop finish.
        if run.status == RunStatus.CANCELLED.value and outcome.status != RunStatus.CANCELLED:
            session.commit()
            return
        now = datetime.now(UTC)
        run.status = outcome.status.value
        run.updated_at = now
        if outcome.status in TERMINAL_STATUSES:
            run.finished_at = now
        session.commit()


def attach_run_worker(
    *,
    session_factory: sessionmaker[Session] | SessionFactory,
    browser_manager: BrowserSessionManager | None = None,
    settings: RunWorkerSettings | None = None,
    **kwargs: Any,
) -> RunWorker:
    """Construct a :class:`RunWorker` for FastAPI app state.

    Args:
        session_factory: DB session factory.
        browser_manager: Optional browser session manager.
        settings: Optional worker settings.
        **kwargs: Forwarded to :class:`RunWorker`.

    Returns:
        Configured worker instance.
    """
    factory: SessionFactory
    if isinstance(session_factory, sessionmaker):
        factory = session_factory
    else:
        factory = session_factory
    return RunWorker(
        factory,
        settings=settings,
        browser_manager=browser_manager,
        **kwargs,
    )
