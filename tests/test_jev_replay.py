"""Offline Jev replay tests (T032)."""

from __future__ import annotations

import json
from pathlib import Path

from browser_use_agent.eval.jev_replay import main, render_markdown, replay_checkpoint
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient

FIXTURE = Path(__file__).parent / "fixtures" / "sample_checkpoint.json.zst"
RUN_ID = "11111111-1111-4111-8111-111111111111"


async def test_replay_fixture_is_offline_and_matches_original() -> None:
    """A compressed fixture replays through the fake client without a network."""
    report = await replay_checkpoint(
        FIXTURE,
        run_id=RUN_ID,
        client=FakeJevClient([FakeDecision(operation="CLICK", target_key="1", confidence=0.91)]),
        client_name="A",
    )

    assert report.selected_action == "CLICK"
    assert report.selected_target == 1
    assert report.confidence == 0.91
    assert report.original_action == "CLICK"
    assert report.match is True
    assert "selected_action" in render_markdown(report)


def test_evaluate_jev_cli_uses_fixture_client(monkeypatch, capsys) -> None:
    """The CLI's recorded fixture client does not require Jev credentials."""
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)

    assert (
        main(
            [
                "--run-id",
                RUN_ID,
                "--checkpoint",
                str(FIXTURE),
                "--client",
                "B",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["selected_action"] == "DONE"
    assert report["confidence"] == 0.62
    assert report["match"] is False
