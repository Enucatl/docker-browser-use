# Screenshot capture policy

Viewport screenshots are archived as **WebP or JPEG artifacts** on important
transitions. `agent_events` rows stay small and only store artifact references
(`artifact_id`, `storage_key`, `sha256`, reason, format). Image binaries never
go into Postgres.

Continuous video remains **off** by default (out of scope for T019).

## When a screenshot is captured

| Reason | Trigger |
| --- | --- |
| `first` | First observation of a run |
| `url_change` | Page URL changed since last screenshot |
| `title_change` | Document title changed |
| `approval` | Approval boundary (forced from the agent loop) |
| `error` | Action failure boundary (forced from the agent loop) |
| `destructive` | After a successful high-impact action (`NAVIGATE`, Bitwarden kinds) |
| `periodic` | Observation count since last capture reached the heartbeat interval |

Routine DOM-only churn and ordinary `CLICK` / `SCROLL` / `TYPE_TEXT` steps do
**not** trigger screenshots. That keeps the default policy from capturing on
every action.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCREENSHOT_ENABLED` | `true` | Master switch |
| `SCREENSHOT_FORMAT` | `webp` | `webp` or `jpeg` |
| `SCREENSHOT_QUALITY` | `80` | Encoder quality 1–100 |
| `SCREENSHOT_INTERVAL_STEPS` | `20` | Heartbeat interval (`0` disables) |

Raise `SCREENSHOT_INTERVAL_STEPS` (or set `0`) to loosen capture; lower it to
tighten heartbeats. `ARTIFACTS_ROOT` (T007) is required for the worker to
auto-wire a `ScreenshotWriter`.

## Encoding

Raw capture (CDP PNG or fake test bytes) is re-encoded with Pillow. EXIF and
other ancillary metadata are stripped. Artifact `media_type` is `image/webp` or
`image/jpeg`; kind is `screenshot`.

## Audit event

Event type: `screenshot_captured`. Payload includes refs and policy metadata
only — never the image bytes.
