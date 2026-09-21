"""Replay compressed browser checkpoints through Jev without a browser."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from browser_use_agent.artifacts.store import create_artifact_store
from browser_use_agent.audit.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    load_checkpoint_payload,
    observation_from_checkpoint,
)
from browser_use_agent.db.engine import create_engine_from_settings
from browser_use_agent.db.models import AgentEvent, Run
from browser_use_agent.policy.jev_adapter import JevAdapter
from browser_use_agent.policy.jev_client import (
    FakeDecision,
    FakeJevClient,
    HttpJevClient,
    JevClient,
    load_jev_client_settings,
)


@dataclass(frozen=True, slots=True)
class JevReplayReport:
    """Small, JSON-serializable result of one checkpoint replay."""

    run_id: str
    checkpoint: str
    client: str
    model: str
    selected_action: str
    selected_target: int | None
    confidence: float | None
    original_action: str | None
    original_target: int | None
    match: bool | None

    def as_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-compatible mapping."""
        return asdict(self)


def replay_checkpoint(
    checkpoint: bytes | bytearray | Path | str | dict[str, Any],
    *,
    run_id: uuid.UUID | str,
    client: JevClient,
    client_name: str = "A",
    goal: str | None = None,
    history_summary: str | None = None,
    historical_decision: dict[str, Any] | str | None = None,
) -> JevReplayReport:
    """Replay one compressed checkpoint through an injected Jev client.

    Args:
        checkpoint: Compressed checkpoint bytes, path, storage key, or payload.
        run_id: Expected owning run id.
        client: Jev client; use :class:`FakeJevClient` for offline evaluation.
        client_name: Label included in the report.
        goal: Optional override for the recorded goal.
        history_summary: Optional override for recorded history.
        historical_decision: Optional audit decision override.

    Returns:
        Decision metadata suitable for JSON or Markdown output.

    Raises:
        ValueError: If the run id or checkpoint envelope is invalid.
    """
    payload, checkpoint_name = _load_checkpoint(checkpoint)
    expected_run_id = str(run_id)
    if payload.get("run_id") != expected_run_id:
        raise ValueError(
            f"checkpoint run_id {payload.get('run_id')!r} does not match {expected_run_id!r}"
        )

    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    recorded_goal = goal if goal is not None else extra.get("goal", payload.get("goal"))
    if not isinstance(recorded_goal, str) or not recorded_goal:
        raise ValueError("checkpoint does not contain a goal; pass --goal")
    recorded_history = (
        history_summary if history_summary is not None else extra.get("history_summary", "")
    )
    if not isinstance(recorded_history, str):
        recorded_history = ""

    observation = observation_from_checkpoint(payload)
    model = getattr(client, "model", None) or getattr(
        getattr(client, "settings", None), "model", "jev-latest"
    )
    adapter = JevAdapter(model=model)
    request = adapter.to_jev_request(observation, recorded_goal, recorded_history)
    response = client.decide(request)
    action = adapter.from_jev_response(response, observation=observation)
    original = (
        historical_decision if historical_decision is not None else _original_decision(payload)
    )
    original_action = _decision_action(original)
    original_target = _decision_target(original)
    match = None
    if original_action is not None:
        match = action.kind.value == original_action and action.target_index == original_target

    return JevReplayReport(
        run_id=expected_run_id,
        checkpoint=checkpoint_name,
        client=client_name,
        model=response.model,
        selected_action=action.kind.value,
        selected_target=action.target_index,
        confidence=action.confidence,
        original_action=original_action,
        original_target=original_target,
        match=match,
    )


def render_markdown(report: JevReplayReport) -> str:
    """Render one replay report as a compact Markdown table."""
    values = report.as_dict()
    rows = "\n".join(f"| {key} | {value} |" for key, value in values.items())
    return "| Field | Value |\n| --- | --- |\n" + rows + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the ``evaluate-jev`` command."""
    parser = argparse.ArgumentParser(description="Replay a recorded Jev checkpoint offline.")
    parser.add_argument("--run-id", required=True, help="Expected checkpoint run UUID.")
    parser.add_argument("--checkpoint", required=True, help="Checkpoint path or artifact key.")
    parser.add_argument("--client", required=True, choices=("A", "B"))
    parser.add_argument("--goal", help="Override the checkpoint's recorded goal.")
    parser.add_argument("--history-summary", default="")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        payload, _ = _load_checkpoint(args.checkpoint)
        db_goal, db_decision = _load_run_context(payload, args.run_id)
        client = _client_for_label(args.client, payload)
        report = replay_checkpoint(
            args.checkpoint,
            run_id=args.run_id,
            client=client,
            client_name=args.client,
            goal=args.goal or db_goal,
            history_summary=args.history_summary or None,
            historical_decision=db_decision,
        )
        output = (
            json.dumps(report.as_dict(), indent=2, sort_keys=True)
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            args.output.write_text(
                output + ("\n" if not output.endswith("\n") else ""), encoding="utf-8"
            )
        else:
            print(output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"evaluate-jev: {exc}", file=sys.stderr)
        return 2
    return 0


def _load_checkpoint(
    checkpoint: bytes | bytearray | Path | str | dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Load and schema-validate a checkpoint from bytes, path, or artifact key."""
    if isinstance(checkpoint, dict):
        payload = checkpoint
        name = "<mapping>"
    else:
        if isinstance(checkpoint, (bytes, bytearray)):
            data = bytes(checkpoint)
            name = "<bytes>"
        else:
            path = Path(checkpoint)
            if path.is_file():
                data = path.read_bytes()
                name = str(path)
            else:
                data = create_artifact_store().get(str(checkpoint))
                name = str(checkpoint)
        payload = load_checkpoint_payload(data)
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported checkpoint schema_version {payload.get('schema_version')!r}; "
            f"expected {CHECKPOINT_SCHEMA_VERSION}"
        )
    return payload, name


def _original_decision(payload: dict[str, Any]) -> dict[str, Any] | str | None:
    """Find the redacted historical decision retained with a fixture/artifact."""
    extra = payload.get("extra")
    candidates = [
        payload.get("original_decision"),
        payload.get("historical_decision"),
        extra.get("original_decision") if isinstance(extra, dict) else None,
        extra.get("historical_decision") if isinstance(extra, dict) else None,
        extra.get("decision") if isinstance(extra, dict) else None,
    ]
    return next((item for item in candidates if isinstance(item, (dict, str))), None)


def _decision_action(decision: dict[str, Any] | str | None) -> str | None:
    """Read an action kind from an audit-style decision value."""
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict):
        value = decision.get("kind", decision.get("action"))
        return str(value) if value is not None else None
    return None


def _decision_target(decision: dict[str, Any] | str | None) -> int | None:
    """Read a historical target index when one was recorded."""
    if not isinstance(decision, dict):
        return None
    value = decision.get("target_index", decision.get("selected_target"))
    return int(value) if value is not None else None


def _client_for_label(label: str, payload: dict[str, Any]) -> JevClient:
    """Build a fixture fake or configured live client for A/B."""
    extra = payload.get("extra")
    scripts = extra.get("replay_clients") if isinstance(extra, dict) else None
    if not os.environ.get("JEV_API_KEY_FILE") and isinstance(scripts, dict):
        script = scripts.get(label)
        if isinstance(script, dict):
            return FakeJevClient([FakeDecision(**script)], model=f"jev-{label.lower()}")
    settings = load_jev_client_settings()
    model = os.environ.get(f"JEV_MODEL_{label}", settings.model)
    return HttpJevClient(replace(settings, model=model))


def _load_run_context(
    payload: dict[str, Any],
    run_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Load goal and historical decision from audit rows when needed."""
    extra = payload.get("extra")
    has_goal = isinstance(extra, dict) and isinstance(extra.get("goal"), str)
    has_decision = _original_decision(payload) is not None
    if has_goal and has_decision:
        return None, None

    engine = create_engine_from_settings()
    if engine is None:
        return None, None
    try:
        run_uuid = uuid.UUID(run_id)
        step_id = uuid.UUID(str(payload["step_id"]))
        with Session(engine) as session:
            run = session.scalar(select(Run).where(Run.id == run_uuid))
            event = session.scalar(
                select(AgentEvent)
                .where(
                    AgentEvent.run_id == run_uuid,
                    AgentEvent.step_id == step_id,
                    AgentEvent.event_type == "decision",
                )
                .order_by(AgentEvent.seq.asc())
            )
            decision = event.metadata_ if event is not None else None
            return (
                run.goal if run is not None else None,
                decision if isinstance(decision, dict) else None,
            )
    except (ValueError, SQLAlchemyError) as exc:
        raise RuntimeError(f"could not load historical Jev decision: {exc}") from exc
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
