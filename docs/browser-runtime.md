# Browser runtime (Chromium / CDP)

Operator notes for the internal `browser` Compose service (T004 / T013 / T022).
Bitwarden lands in T026.

## Role

- Runs **headed** Chromium on **Xvfb** with Chrome DevTools Protocol (CDP) for
  Browser Use, plus **x11vnc** for the live view pipeline.
- Joins the **internal** Compose network only — never `traefik_proxy`.
- Does **not** publish CDP (9222) or VNC (5900) on the host. Peers reach CDP as
  `browser:9222`; noVNC reaches VNC as `browser:5900` on the internal net.
- Public live view is the sibling **`novnc`** service — see [`live-view.md`](live-view.md).

## Volumes

| Volume | Mount | Purpose |
| --- | --- | --- |
| `chrome_profile` | `/data/chrome-profile` | Persistent agent Chrome profile (cookies, extensions later) |
| `browser_downloads` | `/data/downloads` | Downloads; share with controller when the session manager needs it |

Bitwarden will install into `chrome_profile` in T026 — leave the volume empty for now.

## Hardening choice

Baseline profiles live in `../compose-security-baseline/hardening.yml`.

This service **extends `limits-xxlarge`**, not `hardened-xlarge`.

Why not full `hardened-*`:

1. **`read_only: true`** — Chromium writes under the profile, crash pads, singleton
   locks, and shader/cache paths. A read-only rootfs needs a large set of tmpfs
   exceptions that still behave like a writable runtime; we keep a normal rootfs
   and confine state to `/data/*` plus `/tmp/chromium`.
2. **Chromium sandbox compatibility** — the image installs Debian's
   `chromium-sandbox` helper and removes `--no-sandbox`. The browser service uses
   `seccomp-chromium.json`, based on Docker's default profile with only the
   namespace calls needed by Chromium's user-namespace sandbox added. This keeps
   `no-new-privileges` enabled while allowing Chromium's own sandbox to start.
3. **Xvfb + x11vnc (T022)** — need `/tmp` X11 sockets/locks and extra RAM/PIDs for
   a headed session. Profile bumped from `limits-xlarge` → `limits-xxlarge`
   (3g / 768 pids). Raw VNC uses `-nopw` because RFB is internal-only; Authelia
   gates the HTTP noVNC edge (see [`live-view.md`](live-view.md)).

What we still apply:

- Memory / PID caps from `limits-xxlarge` (3g / 768 pids)
- `security_opt: no-new-privileges:true`
- Moby default seccomp policy plus the Chromium namespace exceptions in
  `seccomp-chromium.json`
- Non-root user `1000:1000` matching the profile volume
- `shm_size: 2gb` (Chrome shared memory)
- No Traefik labels, no host `ports` (CDP or VNC)

Revisit `hardened-xlarge` with targeted tmpfs mounts and capability dropping if a
future Chrome build supports the full read-only runtime profile cleanly.

## CDP binding

| Fixed value | Meaning |
| --- | --- | --- |
| `9222` / `9223` | nginx front / Chromium loopback ports |
| `/data/chrome-profile` | Persistent profile path |
| `/data/downloads` | Download directory |
| `:99` / `5900` | Xvfb display / internal x11vnc port |
| `VNC_VIEW_ONLY` | Operator watch only until T023 |

Modern Chromium refuses non-loopback DevTools binds and rejects `Host` headers that
are not localhost/IP. The entrypoint runs Chromium on `127.0.0.1:9223`
and **nginx-light** listens on `0.0.0.0:9222`, proxying with
`Host: 127.0.0.1` and WebSocket upgrade support so `http://browser:9222` works for
sibling services. Do not add host port mappings like `"9222:9222"` or `"5900:5900"`.

Controller connection string: `http://browser:9222`. CDP clients that follow
`webSocketDebuggerUrl` still see `127.0.0.1:9223` in `/json/version` JSON. The
session manager (`browser_use_agent.browser`) rewrites that host/port to the
peer CDP URL before attaching Browser Use (same idea as Playwright).

## Session manager (T013)

The controller owns `BrowserSessionManager`:

- **Ensure Chrome** — wait for CDP health; optionally `docker compose up -d browser`
  when `BROWSER_COMPOSE_CONTROL=true` (requires Docker CLI access from the
  controller, e.g. a docker.sock mount — off by default).
- **Attach** — Browser Use connects with `is_local=False` and `keep_alive=True`
  against the rewritten WebSocket URL. Chromium already uses
  `/data/chrome-profile` on volume `chrome_profile`.
- **Single interactive session** — a second run gets `BrowserSessionBusyError`.
- **Idle TTL** — after release, `BROWSER_IDLE_TTL_SECONDS` (default 300) calls
  Browser Use `stop()` (detach only; does **not** `kill()` Chromium), so the
  persistent profile is not corrupted. Optional `BROWSER_STOP_ON_IDLE` can stop
  the Compose service when compose control started it.
- **Audit** — emits `browser_started` / `browser_stopped` when an `AuditWriter`
  + DB session are available.
- **Headed + live view** — Chromium runs on Xvfb; operators watch via Authelia-gated
  noVNC (`/vnc/`). See [`live-view.md`](live-view.md).

Env (controller): `BROWSER_IDLE_TTL_SECONDS`, `BROWSER_COMPOSE_CONTROL`,
`BROWSER_STOP_ON_IDLE`. CDP, profile, and storage paths are fixed by the Compose
layout. Downloads volume is mounted on the controller at
`/data/downloads` for later artifact ingestion.

### about:blank smoke

With the browser service healthy on the internal network:

```bash
docker compose up -d browser
# From a peer on the Compose network (example using the project venv):
docker run --rm --network browser-use_default \
  -v /opt/docker/browser-use:/app -w /app \
  -v "$HOME/.local/share/uv/python:$HOME/.local/share/uv/python:ro" \
  -e PYTHONPATH=/app/src \
  --entrypoint /app/.venv/bin/python \
  python:3.14-slim-bookworm \
  -c 'import asyncio; from browser_use_agent.browser import BrowserSessionManager, load_browser_settings; \
print(asyncio.run(BrowserSessionManager(load_browser_settings()).smoke_about_blank()))'
```

Or pytest (skips when CDP is down):

```bash
uv run pytest -m integration tests/test_browser_session.py
```

## Smoke test

Build and prove Chromium starts:

```bash
docker compose build browser
docker compose run --rm --no-deps browser smoke
```

Bring the long-running worker up and hit CDP from inside the network:

```bash
docker compose up -d browser
docker compose exec browser curl -sf http://127.0.0.1:9222/json/version
```

## Related

- **Live view (Xvfb + noVNC)** — [`live-view.md`](live-view.md)
- **Bitwarden** extension in the profile volume (T026)
- Multi-profile Personal / Work / Testing (T031)
