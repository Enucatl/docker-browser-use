# Human approval gates (T021)

High-impact actions park the run in `awaiting_approval` until an authenticated
operator approves or rejects them. The worker stays alive and waits; the action
does **not** execute until granted.

## Policy (Phase 1)

`needs_approval(action, context) -> ApprovalRequest | None` uses a conservative
heuristic (never auto-approve):

| Trigger | When |
| --- | --- |
| Bitwarden kinds | Always (`BITWARDEN_LOGIN` / `IDENTITY` / `CARD`) |
| `CLICK` | Target label/href matches purchase, payment, delete-account, send-message, … keywords |
| `NAVIGATE` | URL path/host contains checkout, payment, billing, unsubscribe, … fragments |
| Other kinds | Allowed without a gate |

Richer rule packs and UI-facing explanations land in T028.

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
