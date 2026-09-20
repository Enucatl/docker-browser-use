"""API route modules."""

from browser_use_agent.api.routes.approvals import router as approvals_router
from browser_use_agent.api.routes.run_controls import router as run_controls_router
from browser_use_agent.api.routes.runs import router as runs_router
from browser_use_agent.api.routes.takeover import router as takeover_router

__all__ = [
    "approvals_router",
    "run_controls_router",
    "runs_router",
    "takeover_router",
]
