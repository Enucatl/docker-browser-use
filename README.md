# docker-browser-use

Always-on self-hosted browser-agent service (Agent Web UI → controller → Browser Use / Chrome).

**Phase 1 MVP operators:** start with [`docs/operator.md`](docs/operator.md) and
[`docs/smoke-checklist.md`](docs/smoke-checklist.md). Remaining work is tracked in
[`task_ledger.md`](task_ledger.md).

## Development

```bash
uv sync
uv run pytest
uv run ruff format .
uv run ruff check .
```

Postgres audit schema migrations (Alembic): see [`db/README.md`](db/README.md).
The Compose `controller` entrypoint applies `upgrade head` on start. On a host
with `DATABASE_*` set you can also run
`uv run python -m browser_use_agent.db.migrate`.

## Compose (homelab)

Prerequisites: Traefik (`traefik_proxy`), Authelia middlewares, `/opt/docker/.env`
(`DOCKER_DOMAIN`), and hardening profiles from
[`../compose-security-baseline`](../compose-security-baseline).

```bash
# Compose defaults live in docker-compose.yml; the shared env only provides DOCKER_DOMAIN.
export COMPOSE_ENV_FILES=../.env
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password
: > secrets/jev_api_key
: > secrets/openrouter_api_key
chmod 600 secrets/postgres_password secrets/jev_api_key secrets/openrouter_api_key
docker compose config
docker compose up -d
```

Put the official TypeSafe API key from `console.typesafe.ai` in
`secrets/jev_api_key` as a single line. The controller calls
`https://api.typesafe.ai/v1/systemone` with a Bearer header.

Public URL: `https://browser-use.${DOCKER_DOMAIN}` behind Traefik with
`authelia@docker,secured@file`. Authelia’s wildcard `*.docker.home.arpa` rule
already allows `group:admins`. Auth details: [`docs/auth.md`](docs/auth.md).

### What runs where

| Service | Role | Public? |
| --- | --- | --- |
| `controller` | FastAPI + Agent Web UI | Yes (Authelia) |
| `novnc` | Live Chrome view at `/vnc/` | Yes (Authelia) |
| `browser` | Headed Chromium, CDP `:9222`, x11vnc `:5900` | **No** — internal only |
| `db` | Postgres audit store | **No** — internal only |

CDP and raw VNC are **never** host-published and never on `traefik_proxy`. Verify
with the smoke checklist § B.

Persistent Chrome profile: volume `chrome_profile`. Artifacts:
volume `artifacts_data` at `/var/lib/browser-use/artifacts` on the controller —
[`docs/artifacts.md`](docs/artifacts.md). Browser runtime / hardening:
[`docs/browser-runtime.md`](docs/browser-runtime.md).

### Day-2 operations

- First Authelia login, demo run, pause/approve/takeover: [`docs/operator.md`](docs/operator.md)
- Post-deploy smoke: [`docs/smoke-checklist.md`](docs/smoke-checklist.md)
- Live view: [`docs/live-view.md`](docs/live-view.md) · Takeover: [`docs/takeover.md`](docs/takeover.md)
- Approvals: [`docs/approvals.md`](docs/approvals.md) · WS events: [`docs/ws-events.md`](docs/ws-events.md)

### Known limitations (Phase 1)

- **Jev:** without `JEV_API_KEY_FILE`, FakeJev returns `DONE` (UI/audit smoke only)
- **Bitwarden:** not yet (T026/T027)
- **Browser egress:** Compose `default` is `internal: true` — Chromium cannot load public sites yet
- **`/vnc`:** confirm manually behind Authelia after deploy

```bash
docker compose build
docker compose up -d
docker compose run --rm --no-deps browser smoke
```
