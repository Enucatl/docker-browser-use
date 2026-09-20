# Database and migrations

Postgres data lives in the Compose volume `pgdata` (`/var/lib/postgresql` in the
`db` container). Connection env and the `postgres_password` Docker secret are
wired in T003.

## Schema migrations (Alembic)

Audit / event-sourcing schema lives in SQLAlchemy models under
`src/browser_use_agent/db/models.py`, with Alembic revisions in `/alembic/versions/`.

**Canonical timeline:** `agent_events` is append-only. Rows are never updated or
deleted in normal operation (PostgreSQL triggers reject `UPDATE`/`DELETE`). Hash
chaining columns (`prev_hash`, `event_hash`) are filled by
`browser_use_agent.audit.AuditWriter` (T009). Per-run SHA-256 chains use
key-sorted UTC-canonical JSON; see the module docstring in
`src/browser_use_agent/audit/hashchain.py`. Verify with
`verify_run_chain(session, run_id)`. Normalized helper tables
(`agent_decisions`, `model_calls`, …) support analysis without replacing the
event stream. Artifact **blobs** live on volume `artifacts_data` (see
[`docs/artifacts.md`](../docs/artifacts.md)); the `artifacts` table stores
metadata only.

Apply migrations (compose `db` healthy, `DATABASE_*` set):

```bash
uv run python -m browser_use_agent.db.migrate
# or: uv run python -m browser_use_agent.db
```

Programmatic: `from browser_use_agent.db import upgrade_head; upgrade_head()`.

Override URL for one-shot / tests: `DATABASE_URL=postgresql+psycopg://...`.
