# docker-browser-use

Always-on self-hosted browser-agent service (Agent Web UI → controller → Browser Use / Chrome).

## Development

```bash
uv sync
uv run pytest
uv run ruff format .
uv run ruff check .
```

Postgres audit schema migrations (Alembic): see [`db/README.md`](db/README.md). Apply with
`uv run python -m browser_use_agent.db.migrate` when `DATABASE_*` (or `DATABASE_URL`) is set.

Implementation work is tracked in [`task_ledger.md`](task_ledger.md); detailed briefs live under [`tasks/`](tasks/).

## Compose (homelab)

Copy [`.env.example`](.env.example) to `.env` so Compose loads `COMPOSE_ENV_FILES=../.env` (`DOCKER_DOMAIN` from `/opt/docker/.env`):

```bash
cp .env.example .env
docker compose config
```

Public URL: `https://browser-use.${DOCKER_DOMAIN}` behind Traefik with middlewares `authelia@docker,secured@file`. Authelia’s wildcard `*.docker.home.arpa` rule already allows `group:admins`; no Authelia config change is required for this stack. The controller trusts Authelia `Remote-User` headers when `AUTH_REQUIRED=true` (Compose default) and checks CSRF trusted origins — see [`docs/auth.md`](docs/auth.md).

Secrets belong under [`secrets/`](secrets/) (gitignored except `.gitkeep` / README). Generate the Postgres password before first `compose up`:

```bash
openssl rand -hex 32 > secrets/postgres_password
chmod 600 secrets/postgres_password
```

The `db` service (`postgres:18`) stays on the internal Compose network only — not on `traefik_proxy` and not published on the host.

The `browser` service runs headed Chromium on Xvfb with CDP on the internal network (`browser:9222`). It is not on `traefik_proxy` and does not publish CDP (9222) or VNC (5900) on the host. Persistent agent profile: volume `chrome_profile`. The controller attaches Browser Use on demand via `BrowserSessionManager` (CDP WebSocket host rewrite, idle detach, audit `browser_started`/`browser_stopped`). Runtime notes: [`docs/browser-runtime.md`](docs/browser-runtime.md).

Live Chrome view: Authelia-gated noVNC at `https://browser-use.${DOCKER_DOMAIN}/vnc/` (sibling `novnc` service; view-only by default). Take-control mutex: [`docs/takeover.md`](docs/takeover.md). See [`docs/live-view.md`](docs/live-view.md).

Artifact blobs (SHA-256 content-addressed, Zstd helpers for structured state) live on volume `artifacts_data` at `/var/lib/browser-use/artifacts` on the controller — not served by Traefik. See [`docs/artifacts.md`](docs/artifacts.md).

Live run progress: WebSocket `WS /api/runs/{run_id}/events` (replay + AuditWriter bridge). Schema and reconnect notes: [`docs/ws-events.md`](docs/ws-events.md).

```bash
docker compose build browser novnc
docker compose run --rm --no-deps browser smoke
docker compose up -d browser novnc
```
