# Human approval gates (T021/T028)

High-impact actions park the run in `awaiting_approval` until an authenticated
operator approves or rejects them. The worker stays alive and waits; the action
does **not** execute until granted.

## Policy (Phase 1)

`needs_approval(action, context) -> ApprovalRequest | None` evaluates the
ordered, versioned `src/browser_use_agent/policy/approval_policy.toml` pack
(first match wins; restart reloads it; never auto-approve):

| Rule | When |
| --- | --- |
| `always_bitwarden` | Always (`BITWARDEN_LOGIN` / `IDENTITY` / `CARD`) |
| `money_keyword` | `CLICK` element text matches purchase, payment, transfer, or destructive keywords |
| `high_impact_url` | `NAVIGATE` URL matches checkout, payment, billing, unsubscribe, or destructive patterns |
| `low_confidence` | Any action confidence is below `0.5` |
| No match | Allowed without a gate |

Each match produces a stable `reason_code` and configured human `message`.
The message is shown in the existing approval panel and is retained on the
approval event/row.

## API

| Method | Path | Effect |
| --- | --- | --- |
| `POST` | `/api/runs/{id}/approve` | Grant; run → `running`; worker continues execute |
| `POST` | `/api/runs/{id}/reject` | Deny; run → `failed` (no Jev replan in Phase 1) |

Optional JSON body: `{ "reason": "…" }`.

Approver identity comes from Authelia `Remote-User` (or the local-dev stub when
`AUTH_REQUIRED=false`). It is stored on `human_approvals.actor` with
`decided_at` and decision status (`granted` / `denied`).

Audit events (`approval_requested`, `approval_granted`, `approval_denied`,
`approval_timeout`) are redacted and pushed on the existing WebSocket event
stream (T011).

## Timeout (fail closed)

`APPROVAL_TIMEOUT_SECONDS` (default **300**) caps how long a run may wait.
On expiry the loop:

1. Marks the pending `human_approvals` row `denied` (actor `system`)
2. Emits `approval_timeout` with `fail_closed: true`
3. Fails the run (`status=failed`, message `approval_timeout`)

There is no auto-approve path.

## Reject behavior

Reject **fails the run**. The agent does not ask Jev to replan in Phase 1.
Cancel while awaiting approval still ends as `cancelled`.
