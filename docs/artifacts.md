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

MinIO/S3 backend is T034 (`ARTIFACT_STORE=fs|s3`).
