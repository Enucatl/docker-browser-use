# Browser-state checkpoints

Full model-visible browser observations are archived as **compressed artifacts**
on meaningful transitions. `agent_events` rows stay small and only store
artifact references (`artifact_id`, `storage_key`, `sha256`, reason, fingerprint
hints).

Schema version is embedded in every payload (`schema_version`) so offline Jev
evaluation (T032) can reject unknown envelopes loudly.

## When a checkpoint is written

| Reason | Trigger |
| --- | --- |
| `first` | First observation of a run |
| `url_change` | Page URL changed since last checkpoint |
| `title_change` | Document title changed |
| `dom_hash_change` | Structural candidate set changed enough (Jaccard distance ≥ threshold) |
| `browser_error` | Browser/page error count increased |
| `approval` | Approval boundary (forced from the agent loop) |
| `error` | Action failure boundary (forced from the agent loop) |
| `periodic` | Observation count since last write reached the interval |

Unchanged observations are **skipped** (no new artifact / event), except when
forced or the periodic interval elapses.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHECKPOINT_ENABLED` | `true` | Master switch |
| `CHECKPOINT_INTERVAL_STEPS` | `10` | Periodic interval (`0` disables) |
| `CHECKPOINT_DOM_CHANGE_RATIO` | `0.25` | Minimum Jaccard distance for a “major” DOM change |

`ARTIFACTS_ROOT` (T007) is required for the worker to auto-wire a
`CheckpointWriter`.

## Payload shape

Zstd-compressed JSON (`application/zstd+json`), artifact kind `browser_state`:

```json
{
  "schema_version": 1,
  "kind": "browser_state",
  "reason": "url_change",
  "run_id": "...",
  "step_id": "...",
  "captured_at": "2026-09-20T00:00:00+00:00",
  "fingerprint": { "url": "...", "title": "...", "dom_hash": "...", "...": "..." },
  "observation": { "url": "...", "title": "...", "candidates": [ ... ] }
}
```

Secrets are redacted **before** serialize (T008). Content-addressed dedupe is
handled by the artifact store (T007).

## Audit event

Event type: `browser_state_checkpoint`. Metadata includes refs only — never the
full candidate list.
