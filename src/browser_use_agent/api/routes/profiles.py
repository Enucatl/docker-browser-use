"""REST route for the configured persistent Chrome profiles."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from browser_use_agent.browser.profiles import list_profiles
from browser_use_agent.config import AppSettings

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileResponse(BaseModel):
    """Public persistent browser profile metadata."""

    id: str
    display_name: str
    user_data_dir: str
    notes: str


@router.get("", response_model=list[ProfileResponse])
def get_profiles(request: Request) -> list[ProfileResponse]:
    """List profiles available for a new run."""
    settings: AppSettings = request.app.state.settings
    return [
        ProfileResponse(
            id=profile.id,
            display_name=profile.display_name,
            user_data_dir=profile.user_data_dir,
            notes=profile.notes,
        )
        for profile in list_profiles(settings=settings.browser)
    ]
