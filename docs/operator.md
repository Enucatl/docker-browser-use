# Operator runbook (Phase 1 MVP)

Cold-start guide for the always-on browser-agent stack on this homelab host.
Implementation tracking: [`task_ledger.md`](../task_ledger.md).

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Docker + Compose | Project lives at `/opt/docker/browser-use` |
| Shared env | `/opt/docker/.env` provides `DOCKER_DOMAIN`; export `COMPOSE_ENV_FILES=../.env` in the shell/systemd unit. |
| External network | `traefik_proxy` already exists (Traefik) |
| Authelia | Middleware `authelia@docker` + `secured@file`; wildcard `*.docker.home.arpa` already allows `group:admins` |
| Hardening profiles | Sibling repo [`../compose-security-baseline`](../../compose-security-baseline) (`hardening.yml`) |
| Secrets | `./secrets/postgres_password`, `jev_api_key`, `openrouter_api_key` (see [`../secrets/README.md`](../secrets/README.md)); Puppet ACLs for remapped uid `100999` |

Public URL: `https://browser-use.${DOCKER_DOMAIN}` (e.g. `https://browser-use.docker.home.arpa`).

No Authelia config change is required for a normal `*.docker.home.arpa` admin app.

## First-time setup

```bash
cd /opt/docker/browser-use
export COMPOSE_ENV_FILES=../.env
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password
: > secrets/jev_api_key
: > secrets/openrouter_api_key
chmod 600 secrets/postgres_password secrets/jev_api_key secrets/openrouter_api_key
docker compose config >/dev/null   # Host(`browser-use.docker.home.arpa`) — not blank
```

Optional (live Jev / OpenRouter): put real one-line keys in
`secrets/jev_api_key` and `secrets/openrouter_api_key` — never commit them.
Compose mounts them as `JEV_API_KEY_FILE` / `TEXT_LLM_API_KEY_FILE`.

## Bring the stack up

```bash
docker compose build
docker compose up -d
docker compose ps
```

Services: `controller` (Traefik), `db` (internal), `browser` (CDP/VNC internal),
`novnc` (Authelia-gated `/vnc`).

The controller entrypoint applies Alembic migrations to `head` on start. Manual
fallback (host with `uv` + DB env): `uv run python -m browser_use_agent.db.migrate`
— see [`../db/README.md`](../db/README.md).

Health checks:

```bash
docker compose exec controller .venv/bin/python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
docker compose exec browser curl -sf http://127.0.0.1:9222/json/version | head -c 200
```

## First login (Authelia)

1. Open `https://browser-use.${DOCKER_DOMAIN}/` in a browser on the LAN.
2. Complete Authelia login as a user in `group:admins`.
3. You should land on the **New run** form (Agent Web UI).

Identity for API/UI actions comes from Authelia `Remote-User` (see
[`auth.md`](auth.md)). Do not set `AUTH_REQUIRED=false` in Compose production.

## Demo run (smoke without live Jev)

Without `JEV_API_KEY_FILE`, the controller uses `FakeJevClient`,
which returns `DONE` on the first decide. That is enough to prove:

- run create → worker → audit events → WebSocket stream → UI history

Suggested goal text (UI placeholder is fine):

```text
Smoke: attach Chrome and finish (fake Jev)
```

Steps:

1. Submit the goal on `/`.
2. On the run page, confirm status moves toward `succeeded` (or watch live events).
3. Optional: **Pause** then **Resume**, or **Cancel**, while a longer scripted
   run is active (needs a real Jev script or a slow page — see limitations).
4. Open **Open live view (/vnc)** (Authelia again if prompted) to see headed Chrome.

With real Jev credentials configured, use a concrete browse goal. Remember the
browser Compose network is `internal: true` — public page loads may fail until
egress is designed (see limitations).

## Pause / approve / takeover

| Control | When | Docs |
| --- | --- | --- |
| Pause / Resume / Cancel / Retry | Run page buttons or `POST /api/runs/{id}/…` | API under `/api/runs` |
| Approve / Reject | High-impact actions → `awaiting_approval` | [`approvals.md`](approvals.md) |
| Take control / Release | Parks agent; blocks execute | [`takeover.md`](takeover.md) |
| Live view | `/vnc/` (view-only RFB by default) | [`live-view.md`](live-view.md) |

Take-control is the **agent mutex**. Interactive VNC (disable
`VNC_VIEW_ONLY`) is optional and separate from the mutex.

## Where data lives

| Data | Location |
| --- | --- |
| Audit / runs / hash chain | Postgres volume `pgdata` (`agent_events`, …) |
| Artifact blobs | Volume `artifacts_data` → `/var/lib/browser-use/artifacts` on controller |
| Chrome profile | Volume `chrome_profile` → `/data/chrome-profile` on browser |
| Downloads | Volume `browser_downloads` → `/data/downloads` |

Artifact layout: [`artifacts.md`](artifacts.md). Screenshots / checkpoints:
[`screenshots.md`](screenshots.md), [`checkpoints.md`](checkpoints.md).

SQL peek (from host via Compose):

```bash
docker compose exec -T db \
  psql -U browser_use -d browser_use \
  -c "SELECT id, status, left(goal,60) FROM runs ORDER BY created_at DESC LIMIT 5;"
```

## Browser hardening notes

The `browser` service extends **`limits-xxlarge`** from
`../compose-security-baseline/hardening.yml`, **not** full `hardened-*`
(Chromium + Xvfb + writable profile). Details: [`browser-runtime.md`](browser-runtime.md).

Controller / novnc use `hardened-medium` / `hardened-tiny`. Postgres uses the
baseline `postgres` profile.

**CDP and raw VNC must never be host-published or Traefik-routed.** Only the
HTTP noVNC edge (`/vnc`) and the controller UI/API are on `traefik_proxy`.

## Known limitations (Phase 1)

1. **Real Jev credentials** — Without `JEV_API_KEY_FILE`, decisions are fake
   (`DONE` immediately). Live action selection needs a real key and reachable API.
2. **Bitwarden** — Not installed yet (T026/T027). Login/identity/card fills are stubs.
3. **Internal network egress** — Compose `default` is `internal: true`. The
   `browser` service has no internet path, so Chromium cannot load public sites
   until an intentional egress design lands. The controller can still reach
   outbound APIs via `traefik_proxy` when keyed.
4. **Authelia + `/vnc`** — Manual verify: open `/vnc/` after Authelia login and
   confirm the desktop appears. Do not bypass Authelia for this path.
5. **Text LLM** — `TYPE_TEXT` without pre-filled text needs `TEXT_LLM_*` or fails closed.
6. **Multi-profile / cost / OTel** — Phase 2 (T029–T031).

Remaining work: [`task_ledger.md`](../task_ledger.md) Phase 2 / 3.

## Smoke checklist

Follow [`smoke-checklist.md`](smoke-checklist.md) after every cold deploy.
