"""Tests for the controller health endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from browser_use_agent.api.app import create_app
from browser_use_agent.config import AppSettings


def test_healthz_ok() -> None:
    """GET /healthz returns 200 and a JSON ok payload without a database."""
    settings = AppSettings(host="127.0.0.1", port=8000, database=None)
    client = TestClient(create_app(settings))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
