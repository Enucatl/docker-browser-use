# WebSocket run event stream (T011)

Live run progress for the Agent Web UI is delivered over WebSockets. Payloads
are **redacted audit events only** (same path as `AuditWriter` / T008–T009).
Large browser state must be referenced by artifact id, not inlined.

## Endpoint

```text
WS /api/runs/{run_id}/events?after_seq=0&replay_limit=200
```

| Query | Default | Meaning |
| --- | --- | --- |
| `after_seq` | `0` | Exclusive lower bound on event `seq` for replay |
| `replay_limit` | `200` (max `500`) | Cap on historical rows before live mode |

## Client message schema (server → client)

All messages are JSON objects.

### Control

| `type` | Fields | When |
| --- | --- | --- |
| `replay_start` | `run_id`, optional `detail` | After accept, before historical events |
| `replay_end` | `run_id`, `seq` (last replayed) | After replay; live follows |
| `error` | `run_id`, `detail` | e.g. unknown run |

### Event

| Field | Type | Notes |
| --- | --- | --- |
| `type` | `"event"` | |
| `run_id` | string UUID | |
| `seq` | int | Per-run monotonic sequence |
| `event_id` | string UUID | `agent_events.id` |
| `event_type` | string | e.g. `task_received`, `run_created` |
| `actor` | string | `agent` \| `human` \| `system` |
| `occurred_at` | string | ISO-8601 |
| `payload` | object | Redacted metadata; may be truncated |
| `source` | `"replay"` \| `"live"` | |
| `truncated` | bool | Present/true when payload was size-capped |

Oversized metadata keeps artifact-related keys (`artifact_id`, `sha256`, …) and
sets `truncated: true` / `_truncated` inside `payload`. Clients should fetch
blobs from the artifact store (see `docs/artifacts.md`).

## Reconnect / replay strategy

1. On first connect, use `after_seq=0` (or omit) to receive recent history up to
   `replay_limit`, then live events.
2. Remember the highest `seq` observed.
3. On disconnect, reconnect with `after_seq=<last_seq>`.
4. The server may briefly overlap replay and live; ignore events with
   `seq <= last_seq`.
5. If the gap is larger than `replay_limit`, raise `replay_limit` or load the
   remainder via a future REST history API (T024).

Live delivery uses an **in-process** pub/sub bridged from `AuditWriter.append`
(same worker). Multi-replica fan-out is out of scope for T011.

## Auth (T012)

| Mode | Env | Behavior |
| --- | --- | --- |
| Local / tests | `AUTH_REQUIRED=false` (default) | Accept `Remote-User`, else `X-Browser-Use-Dev-User`, else identity `anonymous` |
| Prod (Compose) | `AUTH_REQUIRED=true` | Require Authelia `Remote-User`; reject otherwise (WS close `4401`). Browser `Origin` must match `CSRF_TRUSTED_ORIGINS` when present. |

Do **not** set `AUTH_REQUIRED=false` in production Compose. Details and the
header spoofing model: [`docs/auth.md`](auth.md).

## Secrets

WebSocket payloads never bypass redaction: only events already processed by
`AuditWriter` (which calls `redact_for_audit`) are published or replayed.
