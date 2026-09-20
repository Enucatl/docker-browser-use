# docker-browser-use

Always-on self-hosted browser-agent service (Agent Web UI → controller → Browser Use / Chrome).

## Development

```bash
uv sync
uv run pytest
uv run ruff format .
uv run ruff check .
```

Implementation work is tracked in [`task_ledger.md`](task_ledger.md); detailed briefs live under [`tasks/`](tasks/).
