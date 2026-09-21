# Live agent Chrome view (Xvfb + noVNC)

Operator **viewership** of the agent’s real Chromium session (T022). Interactive
take-control / mutex is **T023** — this path is view-only by default.

## URL

```text
https://browser-use.${DOCKER_DOMAIN}/vnc/
```

Example: `https://browser-use.docker.home.arpa/vnc/`.

Traefik routing (labels only; no global Traefik/Authelia repo edits):

| Piece | Value |
| --- | --- |
| Host | `browser-use.${DOCKER_DOMAIN}` |
| Path | `PathPrefix(/vnc)` (priority 200, paperless-ai-style) |
| Strip | middleware `browser-use-vnc-strip` → `/vnc` |
| Auth | `authelia@docker,secured@file` |
| Backend | `novnc:6080` (websockify + noVNC assets) |

The index page opens `vnc.html` with `path=vnc/websockify` so the WebSocket still
matches the `/vnc` prefix before stripprefix.

## Architecture

```text
Operator Firefox
  → Traefik HTTPS + Authelia + secured@file
  → novnc (websockify :6080) on traefik_proxy + internal net
  → browser:5900 (x11vnc, view-only) on the internal default net
  → Xvfb :99 ← headed Chromium

CDP stays un-published on browser:9222 (nginx→loopback); the controller reaches
it on the internal default net, never Traefik.
```

| Port | Where | Public? |
| --- | --- | --- |
| 6080 HTTP (noVNC) | `novnc` via Traefik `/vnc` | Yes, Authelia-gated |
| 5900 RFB (VNC) | `browser` Compose network | No |
| 9222 CDP | `browser` Compose network | No |

## Hardening

### `browser` (Xvfb + Chromium + x11vnc)

Extends **`limits-xxlarge`** (bumped from `limits-xlarge` for headed Chrome +
Xvfb + x11vnc memory/PID headroom). Still **not** `hardened-*`:

1. Same Chromium reasons as before (`read_only` / sandbox — see
   [`browser-runtime.md`](browser-runtime.md)).
2. **Xvfb + x11vnc** need a writable `/tmp` for the X11 socket and locks (compose
   tmpfs). `cap_drop: ALL` + read-only rootfs has not been validated for this
   stack; revisit later if needed.
3. **x11vnc `-nopw`** — raw RFB is not host-published; noVNC reaches it over the
   internal `default` network. Edge auth is Authelia on HTTP noVNC, not an RFB
   password. Do not add `"5900:5900"` host mappings.
4. **`VNC_VIEW_ONLY=true` (default)** — operators can watch but not drive Chrome.
   T023 will add interactive takeover.

### `novnc` (websockify)

Extends **`hardened-tiny`** (`read_only`, `cap_drop: ALL`, `no-new-privileges`)
plus a small `/tmp` tmpfs. Non-root `1000:1000`. No host ports. Only the noVNC
HTTP port is labeled for Traefik.

## Manual test notes

Full Authelia login may not be exercisable from every automation host. Checklist:

1. `docker compose build browser novnc && docker compose up -d browser novnc`
2. Internal CDP still healthy:  
   `docker compose exec browser curl -sf http://127.0.0.1:9222/json/version`
3. Internal VNC listening (no host publish):  
   `docker compose exec browser sh -c 'cat /proc/net/tcp /proc/net/tcp6' | grep -i ':170C'`  
   (5900 decimal = `0x170C`) or `ss -lntp` if available inside the image.
4. Confirm compose has empty `ports: []` on `browser` and `novnc` (no `9222`/`5900`/`6080` host maps).
5. From a desktop on the LAN/VPN: open  
   `https://browser-use.${DOCKER_DOMAIN}/vnc/` → Authelia challenge → noVNC canvas
   shows the X session / Chromium window.
6. Confirm middleware chain in Traefik (router `browser-use-vnc`) includes
   `browser-use-vnc-strip`, `authelia@docker`, `secured@file`.
7. View-only: mouse/keyboard in noVNC should not control Chrome until T023.

If Authelia cannot be completed here, steps 1–4 plus Traefik label review still
validate the non-public CDP/VNC posture and the intended middleware chain.

## Out of scope (later)

- Disabling view-only / shared interactive VNC (env `VNC_VIEW_ONLY` still defaults true)
- WebRTC replacement for noVNC

## Take-control (T023)

Agent mutex and run status `awaiting_human` are documented in
[`takeover.md`](takeover.md). Live view remains view-only by default; operators
take/release control via the controller API so the agent cannot click/type while
a human owns the session.
