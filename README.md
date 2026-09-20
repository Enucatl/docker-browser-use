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

Public URL: `https://browser-use.${DOCKER_DOMAIN}` behind Traefik with middlewares `authelia@docker,secured@file`. Authelia’s wildcard `*.docker.home.arpa` rule already allows `group:admins`; no Authelia config change is required for this stack.

Secrets belong under [`secrets/`](secrets/) (gitignored except `.gitkeep` / README). Generate the Postgres password before first `compose up`:

```bash
openssl rand -hex 32 > secrets/postgres_password
chmod 600 secrets/postgres_password
```

The `db` service (`postgres:18`) stays on the internal Compose network only — not on `traefik_proxy` and not published on the host.

The `browser` service runs Chromium with CDP on the same internal network (`browser:9222`). It is not on `traefik_proxy` and does not publish port 9222 on the host. Persistent agent profile: volume `chrome_profile`. Runtime hardening notes (why `limits-xlarge` instead of `hardened-*`): [`docs/browser-runtime.md`](docs/browser-runtime.md).

```bash
docker compose build browser
docker compose run --rm --no-deps browser smoke
docker compose up -d browser
```
