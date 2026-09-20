# Database placeholders

Postgres data lives in the Compose volume `pgdata` (`/var/lib/postgresql` in the
`db` container). Connection env and the `postgres_password` Docker secret are
wired in T003.

SQL / Alembic migrations for the audit schema belong in **T006**. Keep this
directory as a placeholder until that task lands; do not invent the full schema
here.
