"""Optional JSON state diffs for browser checkpoint payloads."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any

STATE_DIFF_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class StateDiffSettings:
    """Control optional checkpoint diffs.

    Attributes:
        enabled: Whether diff payloads may be emitted.
        full_every: Number of states between full baselines.
    """

    enabled: bool = False
    full_every: int = 10

    @classmethod
    def from_env(cls) -> StateDiffSettings:
        """Load the diff flag and baseline interval from the environment."""
        raw_enabled = os.environ.get("STATE_DIFFS_ENABLED", "false")
        enabled = raw_enabled.strip().lower() in {"1", "true", "yes", "on"}
        full_every = int(os.environ.get("STATE_DIFF_FULL_EVERY", "10"))
        if full_every < 1:
            raise ValueError("STATE_DIFF_FULL_EVERY must be at least 1")
        return cls(enabled=enabled, full_every=full_every)


def _pointer_part(value: str) -> str:
    """Escape one JSON Pointer path component."""
    return value.replace("~", "~0").replace("/", "~1")


def _diff(previous: Any, current: Any, path: str, operations: list[dict[str, Any]]) -> None:
    """Append minimal object-level JSON Patch operations."""
    if isinstance(previous, dict) and isinstance(current, dict):
        for key in sorted(previous.keys() - current.keys()):
            operations.append({"op": "remove", "path": f"{path}/{_pointer_part(key)}"})
        for key in sorted(current.keys() - previous.keys()):
            operations.append(
                {"op": "add", "path": f"{path}/{_pointer_part(key)}", "value": current[key]}
            )
        for key in sorted(previous.keys() & current.keys()):
            _diff(previous[key], current[key], f"{path}/{_pointer_part(key)}", operations)
        return
    if previous != current:
        operations.append({"op": "replace", "path": path, "value": current})


def diff_states(previous: Any, current: Any) -> list[dict[str, Any]]:
    """Return JSON Patch-like operations that turn ``previous`` into ``current``.

    Lists are replaced as a unit; browser state is primarily object-shaped and
    this keeps reconstruction deterministic without another dependency.
    """
    operations: list[dict[str, Any]] = []
    _diff(previous, current, "", operations)
    return operations


def apply_state_diff(previous: Any, operations: list[dict[str, Any]]) -> Any:
    """Apply diff operations and return a reconstructed state."""
    state = copy.deepcopy(previous)
    for operation in operations:
        path = operation["path"]
        if path == "":
            state = copy.deepcopy(operation.get("value"))
            continue
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        parent = state
        for part in parts[:-1]:
            parent = parent[part]
        key = parts[-1]
        if operation["op"] == "remove":
            del parent[key]
        elif operation["op"] in {"add", "replace"}:
            parent[key] = copy.deepcopy(operation["value"])
        else:
            raise ValueError(f"unsupported state diff operation: {operation['op']!r}")
    return state


def build_state_payload(
    current: Any,
    *,
    sequence: int,
    previous: Any | None = None,
    base_sequence: int | None = None,
    settings: StateDiffSettings | None = None,
) -> dict[str, Any]:
    """Build a full or diff state envelope according to the feature flag."""
    resolved = settings if settings is not None else StateDiffSettings.from_env()
    full = not resolved.enabled or previous is None or sequence % resolved.full_every == 0
    if full:
        return {
            "schema_version": STATE_DIFF_SCHEMA_VERSION,
            "kind": "state_full",
            "sequence": sequence,
            "state": current,
        }
    return {
        "schema_version": STATE_DIFF_SCHEMA_VERSION,
        "kind": "state_diff",
        "sequence": sequence,
        "base_sequence": sequence - 1 if base_sequence is None else base_sequence,
        "operations": diff_states(previous, current),
    }


def restore_state(payload: dict[str, Any], previous: Any | None = None) -> Any:
    """Restore a full envelope or apply a diff envelope to its base state."""
    if payload.get("kind") == "state_full":
        return copy.deepcopy(payload["state"])
    if payload.get("kind") != "state_diff" or previous is None:
        raise ValueError("a state diff requires a previous state")
    return apply_state_diff(previous, payload["operations"])
