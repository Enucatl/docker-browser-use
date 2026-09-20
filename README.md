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

Secrets belong under [`secrets/`](secrets/) (gitignored except `.gitkeep` / README). CDP/VNC and future Postgres stay off `traefik_proxy` and are not published on the host.
