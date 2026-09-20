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

The `db` service (`postgres:18`) stays on the internal Compose network only — not on `traefik_proxy` and not published on the host. CDP/VNC follow the same rule when added.
