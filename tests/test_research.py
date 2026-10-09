"""Checks for evidence validation, chunk reuse, synthesis, and controls."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from test_agent_loop import RecordingAuditWriter

from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.controls import RunControlSignals
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.policy.actions import BrowserObservation
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.policy.research import parse_extraction, parse_synthesis, research_prompt
from browser_use_agent.policy.text_llm import (
    FakeTextLLMClient,
    OpenAICompatibleTextLLMClient,
    TextLLMError,
    TextLLMResult,
    TextLLMSettings,
)
from browser_use_agent.runs.status import RunStatus


class ResearchClient(FakeTextLLMClient):
    """Simulate supported findings and a final answer using supplied evidence IDs."""

    async def complete_structured(self, prompt):
        """Return known fixture facts from supplied page text or saved evidence."""
        self.calls.append(prompt)
        data = json.loads(prompt.user)
        if data["purpose"] == "extraction":
            findings = [
                {"field": field, "value": value, "excerpt": excerpt}
                for field, value, excerpt in [
                    ("headquarters", "Paris, France", "Headquarters: Paris, France"),
                    ("refund_policy", "30 days", "Refunds within 30 days"),
                    ("refund_policy", "14 days", "Refunds within 14 days"),
                ]
                if excerpt in data["page_text"] and field in data["fields"]
            ]
            content = {"findings": findings}
        else:
            fields = {}
            for name in data["fields"]:
                items = [item for item in data["evidence"] if item["field"] == name]
                status = (
                    "conflicting"
                    if len({item["value"] for item in items}) > 1
                    else ("found" if items else "missing")
                )
                fields[name] = {
                    "answer": items[0]["value"] if status == "found" else None,
                    "status": status,
                    "evidence_ids": [item["id"] for item in items],
                }
            content = {"fields": fields}
        return TextLLMResult(text=json.dumps(content), model=self.model, latency_ms=0)


def _loop(operations, observations, **kwargs):
    """Build a loop with in-memory audit and a scripted browser/model."""
    client = kwargs.pop("text_llm", ResearchClient())
    audit = RecordingAuditWriter()
    loop = AgentLoop(
        run_id=uuid.uuid4(),
        goal="Research Acme",
        browser=FakeBrowserPort(observations),
        jev=FakeJevClient(
            [
                FakeDecision(operation=op, navigate_url="https://example.test/policy")
                for op in operations
            ]
        ),
        text_llm=client,
        audit=audit,
        needs_approval=lambda *_: False,
        output_fields=kwargs.pop("output_fields", {"headquarters": "City and country"}),
        **kwargs,
    )
    return loop, client, audit


async def test_research_survives_navigation_and_returns_missing_and_conflicts() -> None:
    """Saved sources remain original while missing and disputed fields stay explicit."""
    first = BrowserObservation(
        url="https://example.test/company",
        title="Company",
        page_text="Headquarters: Paris, France. Refunds within 30 days",
    )
    second = BrowserObservation(
        url="https://example.test/policy", title="Policy", page_text="Refunds within 14 days"
    )
    loop, _, _ = _loop(
        ["EXTRACT", "NAVIGATE", "EXTRACT", "DONE"],
        [first, first, second, second],
        output_fields={"headquarters": "City", "refund_policy": "Window", "revenue": "Revenue"},
    )
    assert (await loop.run()).status == RunStatus.SUCCEEDED
    assert loop.result["headquarters"]["sources"][0]["url"] == first.url
    assert loop.result["refund_policy"]["status"] == "conflicting"
    assert len(loop.result["refund_policy"]["sources"]) == 2
    assert loop.result["revenue"] == {"answer": None, "status": "missing", "sources": []}


async def test_repeated_extraction_reuses_saved_evidence_and_reads_long_pages() -> None:
    """Only unread chunks call the model; repeated complete pages reuse evidence."""
    page = BrowserObservation(
        url="https://example.test/long", page_text="X" * 10_001 + " Refunds within 30 days"
    )
    saved = []
    loop, client, audit = _loop(
        ["EXTRACT", "EXTRACT", "EXTRACT", "DONE"],
        [page],
        output_fields={"refund_policy": "Window"},
        persist_research=saved.append,
    )
    assert (await loop.run()).status == RunStatus.SUCCEEDED
    assert len(client.calls) == 3  # Two chunks and final synthesis.
    assert len(loop.evidence) == 1
    assert loop.evidence[0]["offset"] == 10_000
    assert len(loop.research_pages) == 2
    assert [e.payload["reused"] for e in audit.events if e.event_type == "research_evidence"] == [
        False,
        False,
        True,
    ]
    assert saved[-1]["result"] == loop.result
    assert "EXTRACT" in loop.jev.calls[0].questions["operation"].criteria
    assert "https://duckduckgo.com/" in loop.jev.calls[0].questions["navigate_url"].criteria


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        '{"findings":[{"field":"unknown","value":"Paris","excerpt":"Paris"}]}',
        '{"findings":[{"field":"headquarters","value":"Rome","excerpt":"Rome"}]}',
        '{"findings":[{"field":"headquarters","value":"Paris","excerpt":"Paris","url":"fake"}]}',
    ],
)
def test_extraction_rejects_invented_excerpts_fields_and_provenance(content: str) -> None:
    """Model JSON cannot invent quotes, fields, or source URLs."""
    with pytest.raises(TextLLMError):
        parse_extraction(
            content,
            fields={"headquarters": "City"},
            text="Paris",
            url="https://example.test",
            title="Source",
            content_hash="hash",
            offset=0,
        )


@pytest.mark.parametrize("change", ["citation", "missing_field", "hide_conflict", "false_missing"])
def test_synthesis_rejects_unbacked_or_incomplete_results(change: str) -> None:
    """Unknown citations and suppressed evidence prevent a successful result."""
    fields = {"headquarters": "City"}
    evidence = parse_extraction(
        '{"findings":[{"field":"headquarters","value":"Paris","excerpt":"Paris"}]}',
        fields=fields,
        text="Paris",
        url="https://example.test",
        title="Source",
        content_hash="hash",
        offset=0,
    )
    answer = {"answer": "Paris", "status": "found", "evidence_ids": [evidence[0]["id"]]}
    if change == "citation":
        answer["evidence_ids"] = ["invented"]
    if change == "hide_conflict":
        evidence.append({**evidence[0], "id": "other", "value": "Rome"})
    if change == "false_missing":
        answer = {"answer": None, "status": "missing", "evidence_ids": []}
    content = {"fields": {} if change == "missing_field" else {"headquarters": answer}}
    with pytest.raises(TextLLMError):
        parse_synthesis(json.dumps(content), fields=fields, evidence=evidence)


@pytest.mark.parametrize(
    "client",
    [FakeTextLLMClient(texts=["broken JSON"]), FakeTextLLMClient(fail_with="model unavailable")],
)
async def test_failed_extraction_cannot_finish_successfully(client) -> None:
    """Invalid JSON and model errors preserve a failed state and no result."""
    loop, _, audit = _loop(
        ["EXTRACT", "DONE"],
        [
            BrowserObservation(
                url="https://example.test",
                page_text="Headquarters: Paris, France",
            )
        ],
        text_llm=client,
    )
    assert (await loop.run()).status == RunStatus.FAILED
    assert loop.result is None
    assert not loop.evidence
    assert "model_call_failed" in audit.types()


async def test_empty_evidence_cannot_produce_a_successful_result() -> None:
    """DONE without saved evidence fails without asking the synthesis model."""
    loop, client, _ = _loop(["DONE"], [BrowserObservation(url="https://example.test")])
    assert (await loop.run()).status == RunStatus.FAILED
    assert not client.calls


@pytest.mark.parametrize("phase", ["extraction", "synthesis"])
async def test_cancel_during_research_model_discards_unfinished_output(phase: str) -> None:
    """Cancellation after model return prevents saving findings or final success."""
    cancelled = False

    class CancellingClient(ResearchClient):
        """Cancel after the selected research model phase returns."""

        async def complete_structured(self, prompt):
            nonlocal cancelled
            result = await super().complete_structured(prompt)
            cancelled = json.loads(prompt.user)["purpose"] == phase
            return result

    loop, _, _ = _loop(
        ["EXTRACT", "DONE"],
        [
            BrowserObservation(
                url="https://example.test",
                page_text="Headquarters: Paris, France",
            )
        ],
        text_llm=CancellingClient(),
        is_cancelled=lambda: cancelled,
    )
    assert (await loop.run()).status == RunStatus.CANCELLED
    assert loop.result is None
    assert bool(loop.evidence) == (phase == "synthesis")


async def test_takeover_during_extraction_discards_stale_finding() -> None:
    """A human edit forces a new observation and extraction after release."""
    held = False
    signals = RunControlSignals()

    class TakeoverClient(ResearchClient):
        """Hold browser control once while an extraction is in flight."""

        async def complete_structured(self, prompt):
            nonlocal held
            result = await super().complete_structured(prompt)
            if len(self.calls) == 1:
                held = True
            return result

    async def release():
        """Release the simulated human takeover after the first model call."""
        nonlocal held
        while not held:
            await asyncio.sleep(0)
        held = False
        signals.wake.set()

    loop, _, _ = _loop(
        ["EXTRACT", "EXTRACT", "DONE"],
        [
            BrowserObservation(
                url="https://example.test",
                page_text="Headquarters: Paris, France",
            )
        ],
        text_llm=TakeoverClient(),
        is_awaiting_human=lambda: held,
        control_signals=signals,
    )
    task = asyncio.create_task(release())
    assert (await loop.run()).status == RunStatus.SUCCEEDED
    await task
    assert len(loop.evidence) == 1
    assert len(loop.text_llm.calls) == 3  # Stale extraction, fresh extraction, synthesis.


async def test_structured_completion_keeps_full_json_and_its_own_budget() -> None:
    """Research JSON is not truncated to the typing line or token budget."""
    prompt = research_prompt("Return JSON", {"purpose": "extraction"})
    content = json.dumps({"findings": [], "padding": "a" * 800}, indent=2)
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    client = OpenAICompatibleTextLLMClient(TextLLMSettings(api_key="test", max_tokens=12))
    with patch("niquests.apost", new=AsyncMock(return_value=response)) as post:
        result = await client.complete_structured(prompt)
        assert result.text == content
        assert post.call_args.kwargs["json"]["max_tokens"] == 4096
        assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}
        await client.complete_type_text(prompt)
        assert post.call_args.kwargs["json"]["max_tokens"] == 12
