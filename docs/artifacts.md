# Artifact storage

Large audit payloads (screenshots, compressed browser state, downloads) live on
a Docker volume. Postgres keeps **metadata only** in the `artifacts` table
(schema from T006); blobs are content-addressed on disk.

Browser-state checkpoint policy (when to write, env knobs, payload schema) is
documented in [checkpoints.md](checkpoints.md) (T018). Screenshot policy
(WebP/JPEG, event-driven capture) is in [screenshots.md](screenshots.md) (T019).

## Layout

Root (compose default): `/var/lib/browser-use/artifacts`

Object path: `ab/cd/<sha256>` where `ab`/`cd` are the first four hex digits of
the SHA-256 of the **stored bytes**. Identical payloads share one object;
multiple metadata rows may reference the same `storage_key`.

The raw directory is **not** exposed via Traefik. Callers must redact secrets
before `put` (T008).

## Compose

Volume `artifacts_data` is mounted read-write on the `controller` service:

| Volume | Mount | Purpose |
| --- | --- | --- |
| `artifacts_data` | `/var/lib/browser-use/artifacts` | Content-addressed blobs |

Browser downloads still use `browser_downloads` (`/data/downloads`); the session
manager ingests files into the artifact store when needed.

Override the root with `ARTIFACTS_ROOT` if mounting elsewhere.

## Optional MinIO backend

Filesystem storage is the default. To use the internal MinIO service, create
the `minio_access_key` and `minio_secret_key` Docker secret files, then start
the profile with `ARTIFACT_STORE=s3`:

```bash
ARTIFACT_STORE=s3 docker compose \
  -f docker-compose.yml -f docker-compose.minio.yml \
  --profile minio up -d
```

The controller uses `http://minio:9000` and creates the configured bucket on
startup. Override `ARTIFACT_S3_ENDPOINT_URL`, `ARTIFACT_S3_BUCKET`,
`ARTIFACT_S3_PREFIX`, or `ARTIFACT_S3_SSE` as needed. MinIO has no published
ports and is not attached to `traefik_proxy`.

Migration is best-effort: copy each filesystem object at
`<ARTIFACTS_ROOT>/<storage_key>` to the same logical key in the configured S3
bucket, then switch `ARTIFACT_STORE` to `s3`; PostgreSQL metadata keys remain
unchanged.

## API

```python
from browser_use_agent.artifacts import (
    FilesystemArtifactStore,
    zstd_encode_json,
    zstd_decode_json,
)

store = FilesystemArtifactStore.from_settings()
compressed = zstd_encode_json({"url": "https://example.test"})
result = store.put(
    compressed,
    media_type="application/zstd+json",
    kind="state",
    session=session,  # optional: writes artifacts metadata row
)
blob = store.get(result.storage_key)
```

## Retention and state diffs (T033)

Retention is disabled unless a period is configured. Plan or apply it with
`uv run python -m browser_use_agent.artifacts.retention`; `--apply` is required
for mutation. `SCREENSHOT_RETENTION_DAYS` and `CHECKPOINT_RETENTION_DAYS` set
the age threshold. `SCREENSHOT_RETENTION_KEEP_EVERY` and
`CHECKPOINT_RETENTION_KEEP_EVERY` control old-artifact downsampling. Boundary
artifacts (`first`, `approval`, `error`, `browser_error`, and `destructive`)
are retained.

The job never deletes or updates `agent_events`. It soft-deletes metadata by
adding `retention_deleted_at` to the artifact row's JSON metadata, preserving
FK-safe audit metadata. A content-addressed blob is unlinked only after every
metadata row sharing its `storage_key` is marked; immutable event payloads may
therefore retain a reference whose blob is intentionally unavailable.

State diffs are off by default. Set `STATE_DIFFS_ENABLED=true` and optionally
`STATE_DIFF_FULL_EVERY` to emit JSON Patch-like object diffs with periodic full
baselines using `browser_use_agent.artifacts.state_diff`.

MinIO/S3 backend is T034 (`ARTIFACT_STORE=fs|s3`).
