"""Observable activity labels shared by the initial page and live events."""

from __future__ import annotations

from typing import Any

from browser_use_agent.db.models import AgentEvent

# These summaries describe recorded operations, never model reasoning.
_EVENT_LABELS = {
    "task_received": ("Task received", "Your task was submitted.", "run", "neutral"),
    "run_created": ("Run queued", "The run is waiting to start.", "run", "neutral"),
    "run_started": ("Run started", "The agent started this task.", "run", "neutral"),
    "run_paused": ("Run paused", "Execution is paused.", "run", "warning"),
    "run_resumed": ("Run resumed", "Execution has resumed.", "run", "neutral"),
    "run_cancelled": ("Run cancelled", "Execution was cancelled.", "run", "warning"),
    "run_failed": ("Run failed", "Execution stopped with an error.", "run", "error"),
    "run_succeeded": ("Run completed", "The task finished successfully.", "run", "success"),
    "run_retry_requested": (
        "Retry requested",
        "Another execution attempt was requested.",
        "run",
        "warning",
    ),
    "browser_started": (
        "Browser connected",
        "The agent connected to the persistent browser.",
        "browser",
        "success",
    ),
    "browser_stopped": (
        "Browser detached",
        "The agent detached from the browser.",
        "browser",
        "neutral",
    ),
    "observation_captured": (
        "Page observed",
        "The browser captured the current page.",
        "browser",
        "neutral",
    ),
    "decision": (
        "Action selected",
        "The model selected the next browser action.",
        "model",
        "neutral",
    ),
    "action_requested": (
        "Browser action started",
        "A browser action was requested.",
        "browser",
        "neutral",
    ),
    "action_completed": (
        "Browser action completed",
        "The browser action completed.",
        "browser",
        "success",
    ),
    "action_failed": (
        "Browser action failed",
        "The browser action could not complete.",
        "browser",
        "error",
    ),
    "step_retry": (
        "Step retry requested",
        "The agent will observe the page again.",
        "run",
        "warning",
    ),
    "model_call": (
        "Model call completed",
        "The text model returned a response.",
        "model",
        "success",
    ),
    "model_call_failed": ("Model call failed", "The text model request failed.", "model", "error"),
    "approval_requested": (
        "Approval required",
        "Execution is waiting for your approval.",
        "approval",
        "warning",
    ),
    "approval_granted": (
        "Action approved",
        "The operator approved the pending action.",
        "approval",
        "success",
    ),
    "approval_denied": (
        "Action rejected",
        "The operator rejected the pending action.",
        "approval",
        "error",
    ),
    "approval_timeout": (
        "Approval timed out",
        "No approval arrived before the timeout.",
        "approval",
        "warning",
    ),
    "takeover_started": (
        "Human control started",
        "The operator has control of the browser.",
        "approval",
        "warning",
    ),
    "takeover_ended": (
        "Agent control restored",
        "The operator returned control to the agent.",
        "approval",
        "success",
    ),
    "takeover_fresh_observe": (
        "Refreshing browser state",
        "The agent is observing the page after human control.",
        "browser",
        "neutral",
    ),
    "screenshot_captured": (
        "Screenshot saved",
        "A browser screenshot was recorded.",
        "artifact",
        "success",
    ),
    "browser_state_checkpoint": (
        "Browser state saved",
        "A browser checkpoint was recorded.",
        "artifact",
        "neutral",
    ),
    "bitwarden_fill_requested": (
        "Credential fill started",
        "The browser requested a credential fill.",
        "browser",
        "neutral",
    ),
    "bitwarden_fill_completed": (
        "Credential fill completed",
        "The credential fill completed.",
        "browser",
        "success",
    ),
    "bitwarden_fill_failed": (
        "Credential fill failed",
        "The credential fill failed.",
        "browser",
        "error",
    ),
}


_ACTION_TITLES = {
    "CLICK": ("Click requested", "Clicked"),
    "TYPE_TEXT": ("Text entry requested", "Text entered"),
    "SCROLL": ("Scroll requested", "Scrolled"),
    "GO_BACK": ("Back navigation requested", "Went back"),
    "NAVIGATE": ("Navigation requested", "Navigated"),
    "SWITCH_TAB": ("Tab switch requested", "Tab switched"),
    "DONE": ("Task completion requested", "Task action completed"),
    "BITWARDEN_LOGIN": ("Login fill requested", "Login filled"),
    "BITWARDEN_IDENTITY": ("Identity fill requested", "Identity filled"),
    "BITWARDEN_CARD": ("Card fill requested", "Card filled"),
}


def event_display(event: AgentEvent) -> dict[str, Any]:
    """Describe recorded execution using only allowlisted scalar metadata.

    Args:
        event: Persisted, redacted audit event.

    Returns:
        Human labels and details, excluding prompts, answers and rationale.
    """
    title, summary, category, tone = _EVENT_LABELS.get(
        event.event_type,
        ("Activity recorded", "Additional audit information is available.", "other", "neutral"),
    )
    payload = event.metadata_ or {}
    kind = payload.get("kind")
    action_titles = _ACTION_TITLES.get(kind.upper()) if isinstance(kind, str) else None
    if action_titles and event.event_type in {"action_requested", "action_completed"}:
        title = action_titles[1] if event.event_type == "action_completed" else action_titles[0]
    fields = {
        "kind": "Action",
        "target_index": "Element",
        "model": "Model",
        "provider": "Provider",
        "prompt_tokens": "Input tokens",
        "completion_tokens": "Output tokens",
        "size_bytes": "Size (bytes)",
        "media_type": "File type",
        "steps": "Steps",
        "status": "Status",
        "step_number": "Step",
        "timeout_seconds": "Timeout (seconds)",
    }
    details = {}
    for key, label in fields.items():
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            details[label] = value[:500] if isinstance(value, str) else value
    if event.url:
        details["URL"] = event.url[:2000]
    if event.duration_ms is not None:
        details["Duration (ms)"] = event.duration_ms
    # Output and errors are observable results; model response bodies stay raw-only.
    result_key = {
        "action_completed": "message",
        "action_failed": "error",
        "run_succeeded": "message",
        "run_failed": "error",
        "approval_requested": "reason",
        "model_call_failed": "error",
    }.get(event.event_type)
    result = payload.get(result_key) if result_key else None
    if isinstance(result, str) and result.strip():
        summary = result[:1000]
    return {
        "title": title,
        "summary": summary,
        "category": category,
        "tone": tone,
        "details": details,
    }
