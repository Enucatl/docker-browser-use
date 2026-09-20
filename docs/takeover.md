# Take-control / human-in-the-loop VNC handoff (T023)

Operators can **take control** of the agent Chrome session for MFA, CAPTCHA,
Bitwarden unlock, passkeys, or awkward widgets, then **release control** so the
agent resumes.

This is the **agent-side mutex**. Interactive VNC (disabling x11vnc view-only)
remains optional/env-driven; see [`live-view.md`](live-view.md).

## State machine (vs pause / approval)

| From | Action | To |
| --- | --- | --- |
| `running` | take-control | `awaiting_human` |
| `paused` | take-control | `awaiting_human` (stronger than pause) |
| `awaiting_human` | release-control | `running` |
| `awaiting_human` | cancel | `cancelled` |
| `awaiting_approval` | take-control | **409** — approve/reject/cancel first |

While `awaiting_human`:

1. The worker parks between loop phases (same wake/poll as pause).
2. Browser **execute** is refused by `TakeoverGuardedBrowserPort` (click/type blocked).
3. Jev decide is not reached while parked (optional auto-disable).
4. On release mid-step, a stale decide/execute is discarded and the loop does a **fresh observe**.

Use **release-control**, not resume, to leave `awaiting_human`.

## API

| Method | Path | Effect |
| --- | --- | --- |
| `POST` | `/api/runs/{id}/take-control` | → `awaiting_human`; audit `takeover_started` |
| `POST` | `/api/runs/{id}/release-control` | → `running`; audit `takeover_ended` |

Operator identity comes from Authelia `Remote-User` (or the local-dev stub when
`AUTH_REQUIRED=false`). The username is stored in the audit payload `actor`
field; the event row’s `actor` column is `human`.
