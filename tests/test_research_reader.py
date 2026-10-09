"""Research reader checks for safe content, chunking, and browser controls."""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browser_use_agent.agent.browser_port import BrowserUsePort, FakeBrowserPort, _safe_page_html
from browser_use_agent.agent.takeover import TakeoverActiveError, TakeoverGuardedBrowserPort
from browser_use_agent.policy.actions import BrowserObservation


def _node(tag: str, text: str = "", attrs: dict | None = None, children: list | None = None):
    """Build a minimal tree using the same reader attributes as Browser Use."""
    from browser_use.dom.views import NodeType

    children = children or []
    node = SimpleNamespace(
        tag_name=tag,
        node_type=NodeType.TEXT_NODE if tag == "#text" else NodeType.ELEMENT_NODE,
        node_value=text,
        attributes=attrs or {},
        children=children,
        children_and_shadow_roots=children,
        shadow_roots=[],
        content_document=None,
        is_visible=True,
    )
    node.get_all_children_text = lambda: "\n".join(child.node_value for child in children)
    return node


def test_reader_omits_form_values_before_html_serialization() -> None:
    """Raw credentials, including mirrored values, never enter the serialized HTML."""
    from browser_use.dom.markdown_extractor import convert_html_to_markdown

    password = "ordinary-unmarked-password"
    textarea_secret = "unmarked-credential-in-textarea"
    root = _node(
        "body",
        children=[
            _node("p", children=[_node("#text", "Headquarters: Paris, France")]),
            _node("input", attrs={"type": "password", "value": password}),
            _node("textarea", children=[_node("#text", textarea_secret)]),
            _node("p", children=[_node("#text", f"Echo: {password}")]),
            _node("p", children=[_node("#text", "password: other-secret")]),
            _node("a", attrs={"api_key": "unmarked-api-key"}, children=[_node("#text", "Policy")]),
        ],
    )
    html, secrets = _safe_page_html(root)
    assert password in secrets
    assert password not in html
    assert textarea_secret not in html
    assert "other-secret" not in html
    assert "unmarked-api-key" not in html
    assert root.children[1].attributes["value"] == password  # Live DOM remains intact.
    markdown, _, _ = convert_html_to_markdown(html)
    assert "Headquarters: Paris, France" in markdown
    assert "Policy" in markdown


def test_ordinary_form_values_do_not_corrupt_research_text() -> None:
    """Checkbox values such as 'on' cannot erase substrings from ordinary prose."""
    root = _node(
        "body",
        children=[
            _node("input", attrs={"type": "checkbox", "value": "on"}),
            _node("input", attrs={"name": "search", "value": "France"}),
            _node("input", attrs={"name": "api_token", "value": "unmarked-sensitive-value"}),
            _node("input", attrs={"type": None, "autocomplete": None, "value": "on"}),
            _node(
                "p",
                attrs={"title": None, "class": ""},
                children=[_node("#text", "Refund conditions: original packaging in France.")],
            ),
            _node("p", children=[_node("#text", "Echo: unmarked-sensitive-value")]),
        ],
    )
    html, secrets = _safe_page_html(root)
    assert "on" not in secrets
    assert "France" not in secrets
    assert "Refund conditions: original packaging in France." in html
    assert "unmarked-sensitive-value" not in html


async def test_reader_chunks_without_consuming_navigation_and_hashes_complete_page() -> None:
    """Reading another chunk keeps the current fake page and its stable full hash."""
    text = "A" * 10_010 + " Refund window: 30 days"
    browser = FakeBrowserPort(
        [
            BrowserObservation(url="https://example.test/first", page_text=text),
            BrowserObservation(url="https://example.test/second", page_text="Second source"),
        ]
    )
    await browser.observe()
    first = await browser.read_page()
    rest = await browser.read_page(10_000)
    assert len(first.page_text) == 10_000
    assert first.page_text_remaining == len(text) - 10_000
    assert rest.page_text == text[10_000:]
    assert rest.page_text_remaining == 0
    assert first.page_text_hash == rest.page_text_hash
    assert rest.url == first.url
    second = await browser.observe()
    assert second.url.endswith("second")
    assert (await browser.read_page()).page_text_hash != first.page_text_hash
    with pytest.raises(ValueError, match="nonnegative"):
        await browser.read_page(-1)


async def test_reader_redacts_metadata_and_refuses_takeover() -> None:
    """Source titles/URLs are redacted and takeover prevents research reads."""
    browser = FakeBrowserPort(
        [
            BrowserObservation(
                url="https://user:secret@example.test/?api_key=123456789abc&token=abc",
                title="password: secret-value",
                page_text="Refunds within 30 days. password: private-value",
            ),
        ]
    )
    page = await browser.read_page()
    assert "123456789abc" not in page.url
    assert "secret@" not in page.url
    assert "token=abc" not in page.url
    assert "secret-value" not in page.title
    assert "private-value" not in page.page_text
    guarded = TakeoverGuardedBrowserPort(browser, lambda: True)
    with pytest.raises(TakeoverActiveError):
        await guarded.read_page()


async def test_live_reader_failure_is_explicit_empty_content() -> None:
    """Unavailable DOM cannot fall back to raw page text during research."""
    session = SimpleNamespace(
        get_browser_state_summary=AsyncMock(return_value={"url": "https://example.test"}),
        _dom_watchdog=None,
    )
    page = await BrowserUsePort(session).read_page()
    assert not page.page_text
    assert any("Page reader unavailable" in error for error in page.browser_errors)


async def test_reader_refuses_changed_page_provenance() -> None:
    """A redirect while reading cannot attach old URLs to the new page's text."""
    session = SimpleNamespace(
        get_browser_state_summary=AsyncMock(return_value={"url": "https://example.test/first"}),
        get_current_page_url=AsyncMock(
            side_effect=[
                "https://example.test/first",
                "https://example.test/second",
            ]
        ),
        _dom_watchdog=SimpleNamespace(
            enhanced_dom_tree=_node(
                "body",
                children=[_node("p", children=[_node("#text", "Source fact")])],
            )
        ),
    )
    page = await BrowserUsePort(session).read_page()
    assert not page.page_text
    assert page.browser_errors


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("BROWSER_RESEARCH_CDP_URL"),
    reason="Set BROWSER_RESEARCH_CDP_URL to opt into the controlled source browser check",
)
async def test_research_reader_two_controlled_pages() -> None:
    """Collect cited ordinary page text from two controlled HTTP source pages.

    Set BROWSER_RESEARCH_SOURCE_HOST when remote Chrome cannot reach this test
    host as 127.0.0.1 (for example, its Docker bridge gateway).
    """
    from browser_use import BrowserSession
    from test_agent_loop import RecordingAuditWriter

    from browser_use_agent.agent.loop import AgentLoop
    from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
    from browser_use_agent.policy.text_llm import TextLLMClient, TextLLMPrompt, TextLLMResult
    from browser_use_agent.runs.status import RunStatus

    pages = {
        "/headquarters": (
            "Acme headquarters",
            "<p>Acme headquarters: Paris, France</p>"
            '<input type="checkbox" value="on">'
            '<a href="/refunds">Refund policy</a>',
        ),
        "/refunds": (
            "Acme refund policy",
            "<p>Refunds are available within 30 days of purchase.</p>",
        ),
    }

    class Sources(BaseHTTPRequestHandler):
        """Serve only two deterministic source documents."""

        def do_GET(self) -> None:
            """Return ordinary HTML source text and a document title."""
            page = pages.get(self.path)
            if page is None:
                self.send_error(404)
                return
            title, body = page
            html = f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format: str, *args: object) -> None:
            """Keep source requests out of test output."""

    class SourceTextLLM(TextLLMClient):
        """Return findings solely from supplied live text, then cite saved IDs."""

        async def complete_type_text(self, prompt: TextLLMPrompt) -> TextLLMResult:
            """Reject typing since this deterministic research needs no forms."""
            raise AssertionError("Unexpected typing request")

        async def complete_structured(self, prompt: TextLLMPrompt) -> TextLLMResult:
            """Verify extraction input and build final citations from saved evidence."""
            data = json.loads(prompt.user)
            if data["purpose"] == "extraction":
                text = data["page_text"]
                facts = [
                    ("headquarters", "Paris, France", "Acme headquarters: Paris, France"),
                    (
                        "refund_policy",
                        "30 days",
                        "Refunds are available within 30 days of purchase.",
                    ),
                ]
                findings = [
                    {"field": field, "value": value, "excerpt": excerpt}
                    for field, value, excerpt in facts
                    if excerpt in text
                ]
                assert len(findings) == 1, text
                payload = {"findings": findings}
            else:
                payload = {
                    "fields": {
                        field: {
                            "answer": item["value"],
                            "status": "found",
                            "evidence_ids": [item["id"]],
                        }
                        for field in data["fields"]
                        for item in data["evidence"]
                        if item["field"] == field
                    }
                }
            return TextLLMResult(text=json.dumps(payload), model="controlled-source", latency_ms=0)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Sources)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = os.environ.get("BROWSER_RESEARCH_SOURCE_HOST", "127.0.0.1")
    base = f"http://{host}:{server.server_port}"
    session = BrowserSession(cdp_url=os.environ["BROWSER_RESEARCH_CDP_URL"], keep_alive=True)
    try:
        await session.start()
        await session.navigate_to(base + "/headquarters")
        saved = {}
        loop = AgentLoop(
            run_id=uuid.uuid4(),
            goal=f"Research Acme using {base}/headquarters and {base}/refunds",
            browser=BrowserUsePort(session),
            jev=FakeJevClient(
                [
                    FakeDecision(operation="EXTRACT", confidence=0.99),
                    FakeDecision(
                        operation="NAVIGATE", navigate_url=base + "/refunds", confidence=0.99
                    ),
                    FakeDecision(operation="EXTRACT", confidence=0.99),
                    FakeDecision(operation="DONE", confidence=0.99),
                ]
            ),
            text_llm=SourceTextLLM(),
            audit=RecordingAuditWriter(),
            needs_approval=lambda action: False,
            output_fields={"headquarters": "City and country", "refund_policy": "Refund window"},
            persist_research=saved.update,
            max_steps=6,
        )
        outcome = await loop.run()
        assert outcome.status == RunStatus.SUCCEEDED, outcome.message
        assert loop.result["headquarters"]["answer"] == "Paris, France"
        assert loop.result["refund_policy"]["answer"] == "30 days"
        assert loop.result["headquarters"]["sources"][0]["url"] == base + "/headquarters"
        assert loop.result["refund_policy"]["sources"][0]["url"] == base + "/refunds"
        assert len(saved["evidence"]) == 2
    finally:
        await session.stop()
        server.shutdown()
        server.server_close()
        thread.join()
