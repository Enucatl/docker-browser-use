# Browser runtime (Chromium / CDP)

Operator notes for the internal `browser` Compose service (T004). Session manager,
noVNC, and Bitwarden land in later tasks (T013 / T022 / T026).

## Role

- Runs headless Chromium with Chrome DevTools Protocol (CDP) for Browser Use.
- Joins the **internal** Compose network only — never `traefik_proxy`.
- Does **not** publish CDP (9222) on the host. Peers reach it as `browser:9222`.

## Volumes

| Volume | Mount | Purpose |
| --- | --- | --- |
| `chrome_profile` | `/data/chrome-profile` | Persistent agent Chrome profile (cookies, extensions later) |
| `browser_downloads` | `/data/downloads` | Downloads; share with controller when the session manager needs it |

Bitwarden will install into `chrome_profile` in T026 — leave the volume empty for now.

## Hardening choice

Baseline profiles live in `../compose-security-baseline/hardening.yml`.

This service **extends `limits-xlarge`**, not `hardened-xlarge`.

Why not full `hardened-*`:

1. **`read_only: true`** — Chromium writes under the profile, crash pads, singleton
   locks, and shader/cache paths. A read-only rootfs needs a large set of tmpfs
   exceptions that still behave like a writable runtime; we keep a normal rootfs
   and confine state to `/data/*` plus `/tmp/chromium`.
2. **Sandbox vs `cap_drop: ALL`** — Docker typically lacks the user-namespace setup
   Chrome’s Zygote sandbox expects. We run with `--no-sandbox` (see
   `CHROMIUM_FLAGS`). Dropping all capabilities on top of that adds little
   isolation for a process that already disables its sandbox, and has broken
   Chromium in similar stacks.

What we still apply:

- Memory / PID caps from `limits-xlarge` (2g / 512 pids)
- `security_opt: no-new-privileges:true`
- Non-root user `1000:1000` matching the profile volume
- `shm_size: 2gb` (Chrome shared memory)
- No Traefik labels, no host `ports`

Revisit `hardened-xlarge` with targeted tmpfs mounts if a future Chrome build runs
cleanly under `cap_drop: ALL` without `--no-sandbox`.

## CDP binding

| Variable | Default | Meaning |
| --- | --- | --- |
| `CDP_PORT` | `9222` | nginx CDP front port inside the container namespace (not on the host) |
| `CDP_LOOPBACK_PORT` | `9223` | Port Chromium binds on `127.0.0.1` (modern Chrome is loopback-only) |
| `CHROME_USER_DATA_DIR` | `/data/chrome-profile` | Persistent profile path |
| `CHROME_DOWNLOAD_DIR` | `/data/downloads` | Download directory |

Modern Chromium refuses non-loopback DevTools binds and rejects `Host` headers that
are not localhost/IP. The entrypoint runs Chromium on `127.0.0.1:${CDP_LOOPBACK_PORT}`
and **nginx-light** listens on `0.0.0.0:${CDP_PORT}`, proxying with
`Host: 127.0.0.1` and WebSocket upgrade support so `http://browser:9222` works for
sibling services. Do not add host port mappings like `"9222:9222"`.

Controller connection string (when T013 lands): `http://browser:9222`. CDP clients
that follow `webSocketDebuggerUrl` may still see `127.0.0.1` in JSON; libraries such
as Playwright rewrite the host from the browser URL — session manager should do the
same if needed.

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

## Future (not in this image yet)

- **Xvfb + noVNC** for a live view (T022)
- **Browser Use session manager** starting/stopping Chrome on demand (T013)
- **Bitwarden** extension in the profile volume (T026)
- Multi-profile Personal / Work / Testing (T031)
