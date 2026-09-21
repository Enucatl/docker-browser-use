# Phase 1 smoke checklist

Run after a cold `docker compose up` (or image rebuild). Mark each step. A human
or agent can execute this; Authelia login and `/vnc` visual check need a browser.

Related: [`operator.md`](operator.md).

## A. Compose and secrets

- [ ] Shared `/opt/docker/.env` provides `DOCKER_DOMAIN` (or it is set via the shell)
- [ ] `secrets/postgres_password`, `secrets/jev_api_key`, `secrets/openrouter_api_key` present, mode `600`, not in git
- [ ] `docker compose config` succeeds
- [ ] `docker compose up -d` brings `db`, `controller`, `browser`, `novnc` to healthy/started

```bash
cd /opt/docker/browser-use
docker compose ps
```

## B. CDP / VNC exposure (must stay private)

Confirm **no host publish** and **no Traefik route** for CDP or raw VNC:

```bash
# Expect: "no public port" / "invalid IP" / empty — never 0.0.0.0:9222 or :5900
docker compose port browser 9222 || true
docker compose port browser 5900 || true

# Ports should map to null (not published)
docker compose ps browser --format json | head -c 400
docker inspect "$(docker compose ps -q browser)" \
  --format '{{json .NetworkSettings.Ports}}'
# Expect: {"5900/tcp":null,"9222/tcp":null} (or equivalent null host bindings)

# Browser must not be on traefik_proxy
docker inspect "$(docker compose ps -q browser)" \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
# Expect: only the project default / internal network name
```

- [ ] `docker compose port browser 9222` fails or shows no host binding
- [ ] `docker compose port browser 5900` fails or shows no host binding
- [ ] Browser networks exclude `traefik_proxy`
- [ ] CDP answers **inside** the container:  
      `docker compose exec browser curl -sf http://127.0.0.1:9222/json/version`

## C. Authelia + UI

- [ ] `https://browser-use.${DOCKER_DOMAIN}/` redirects/challenges via Authelia
- [ ] After login (admins group), **New run** form is visible
- [ ] Unauthenticated access to `/` does not serve the app without Authelia

## D. Demo run + audit

Without Jev credentials, FakeJev finishes quickly with `DONE` (still validates
lifecycle + audit).

1. Start a run with goal: `Smoke: attach Chrome and finish (fake Jev)`
2. Open the run page; confirm live events stream (WebSocket status connected)
3. Expect terminal status `succeeded` (fake Jev) or inspect failures in events

Audit verification (pick **at least one**):

**SQL (preferred):**

```bash
RUN_ID='<paste-run-uuid>'
docker compose exec -T db psql -U browser_use -d browser_use -v ON_ERROR_STOP=1 <<SQL
SELECT status, left(goal, 80) FROM runs WHERE id = '${RUN_ID}';
SELECT seq, event_type, actor FROM agent_events
  WHERE run_id = '${RUN_ID}' ORDER BY seq;
SQL
```

- [ ] Run row exists with expected status
- [ ] `agent_events` has a coherent sequence (e.g. run lifecycle / browser / decide)

**UI:** run page event list shows multiple sequenced events for that run.

Optional hash-chain check (from a shell with DB env / `uv`):

```bash
# Inside controller, with DATABASE_* already set by Compose:
docker compose exec controller .venv/bin/python - <<'PY'
import uuid
from browser_use_agent.db.engine import create_engine_from_settings
from browser_use_agent.db.settings import load_database_settings
from browser_use_agent.audit import verify_run_chain
from sqlalchemy.orm import Session

run_id = uuid.UUID("PASTE-RUN-UUID")
engine = create_engine_from_settings(load_database_settings())
assert engine is not None
with Session(engine) as session:
    assert verify_run_chain(session, run_id), "hash chain broken"
print("ok")
PY
```

- [ ] Audit check completed (SQL and/or UI; optional hash chain)

## E. Controls (best-effort on fake-Jev runs)

FakeJev may finish before you can pause. If the run is still `running`/`paused`:

- [ ] Pause → status `paused`; Resume → `running`
- [ ] Cancel → `cancelled`

Otherwise, note “skipped — run already terminal” and rely on unit tests / a
keyed Jev run later.

Approval / takeover (manual when a gate fires):

- [ ] If `awaiting_approval`: Approve or Reject works ([`approvals.md`](approvals.md))
- [ ] Take control → `awaiting_human`; Release control → `running` ([`takeover.md`](takeover.md))

## F. Live view (manual Authelia verify)

- [ ] Open `https://browser-use.${DOCKER_DOMAIN}/vnc/` after Authelia login
- [ ] noVNC loads and shows the Xvfb/Chrome desktop (may be blank/about:blank)
- [ ] Confirm this path still goes through Authelia (no anonymous public VNC)

## G. Known limitations acknowledged

- [ ] No real Jev key → decisions are fake (`DONE`)
- [ ] Bitwarden not installed (T026+)
- [ ] Browser has no public egress (`default` network `internal: true`)
- [ ] `/vnc` verified manually behind Authelia

## Pass criteria

Phase 1 smoke **passes** when A–D and B (CDP absent) are checked, plus F if a
display is available. Controls (E) are best-effort without a slow/live Jev path.
