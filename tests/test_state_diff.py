"""Tests for the optional state diff codec."""

from browser_use_agent.artifacts.state_diff import (
    StateDiffSettings,
    apply_state_diff,
    build_state_payload,
    diff_states,
    restore_state,
)


def test_state_diff_round_trip_and_periodic_full_baseline() -> None:
    """Diffs reconstruct state and emit a full baseline at the interval."""
    first = {"url": "https://example.test", "nodes": [1, 2], "form": {"valid": False}}
    second = {"url": "https://example.test/next", "nodes": [1, 2, 3], "form": {"valid": True}}
    settings = StateDiffSettings(enabled=True, full_every=3)

    diff = build_state_payload(second, sequence=1, previous=first, settings=settings)
    assert diff["kind"] == "state_diff"
    assert restore_state(diff, first) == second

    full = build_state_payload(second, sequence=3, previous=first, settings=settings)
    assert full["kind"] == "state_full"
    assert restore_state(full) == second


def test_state_diffs_are_off_by_default() -> None:
    """The default payload is a full state for safe rollout."""
    payload = build_state_payload(
        {"ready": True},
        sequence=1,
        previous={"ready": False},
        settings=StateDiffSettings(),
    )
    assert payload["kind"] == "state_full"


def test_root_diff_replacement_is_supported() -> None:
    """Scalar or shape changes can replace the root state."""
    operations = diff_states({"a": 1}, [1, 2])
    assert apply_state_diff({"a": 1}, operations) == [1, 2]
