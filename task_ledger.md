# Browser-use task ledger

Concrete, dispatchable implementation tasks for the always-on self-hosted browser-agent service.

**Do not treat this file as executable work.** Each row is one unit of work for a later implementation model (e.g. GPT 5.6 Luna). Open `tasks/<TASKID>.md` for the full brief before coding.

## Architecture summary

Always-on Docker stack: Agent Web UI → Agent Controller → Browser Use (Chrome via CDP) + Jev (action selection) + optional small text LLM → Bitwarden (secrets in Chrome) → PostgreSQL audit + compressed artifacts.

Homelab wiring (already present on this host):

| Concern | Path / convention |
| --- | --- |
| Hardening profiles | `../compose-security-baseline/hardening.yml` (not `baseline-compose-security`) |
| Traefik ingress | Docker labels on public services; network `traefik_proxy` |
| Authelia | Middleware `authelia@docker` + `secured@file`; hostname `*.docker.home.arpa` |
| Shared env | `/opt/docker/.env` (`DOCKER_DOMAIN`, subnets); project `.env` + `./secrets/` |
| Reference stacks | `../send-bills` (simple Traefik app), `../paperless-ai` (multi-service) |

Suggested public URL: `https://browser-use.${DOCKER_DOMAIN}` (e.g. `browser-use.docker.home.arpa`). Wildcard Authelia rule already covers admins; no Authelia config change unless custom groups/policies are required.

## How to dispatch

1. Pick the next **ready** task whose dependencies are **done**.
2. Give the implementation model only: `tasks/<TASKID>.md`, this ledger’s architecture summary, and relevant existing code.
3. Require: uv/`src` layout (see Python packaging conventions), compose hardening extends, no secrets in git, CDP/VNC never on `traefik_proxy` except the intentional Authelia-gated UI/noVNC path.
4. Mark status in the table below when starting/finishing.

Status values: `todo` · `ready` · `blocked` · `in_progress` · `done` · `cancelled`

## Phase 0 — Homelab foundation

| ID | Title | Depends | Status | Guide |
| --- | --- | --- | --- | --- |
| T001 | Python project scaffold (uv, src layout, tooling) | — | done | [tasks/T001.md](tasks/T001.md) |
| T002 | Compose stack skeleton: networks, Traefik, Authelia, hardening | T001 | done | [tasks/T002.md](tasks/T002.md) |
| T003 | PostgreSQL service, secrets, and app DB wiring | T002 | done | [tasks/T003.md](tasks/T003.md) |
| T004 | Chromium/browser worker image and hardened runtime profile | T002 | done | [tasks/T004.md](tasks/T004.md) |
| T005 | Docker CI via compose-security-baseline reusable workflow | T002 | done | [tasks/T005.md](tasks/T005.md) |

## Phase 1 — Integration glue (MVP)

| ID | Title | Depends | Status | Guide |
| --- | --- | --- | --- | --- |
| T006 | Event-sourcing schema and migrations | T003 | done | [tasks/T006.md](tasks/T006.md) |
| T007 | Artifact store (filesystem, SHA-256 dedupe, Zstd payloads) | T003 | done | [tasks/T007.md](tasks/T007.md) |
| T008 | Secret redaction before any audit write | T001 | done | [tasks/T008.md](tasks/T008.md) |
| T009 | Audit event writer with hash chaining | T006, T008 | done | [tasks/T009.md](tasks/T009.md) |
| T010 | Agent controller FastAPI: run lifecycle API | T003, T006 | done | [tasks/T010.md](tasks/T010.md) |
| T011 | WebSocket run progress and live event stream | T010 | done | [tasks/T011.md](tasks/T011.md) |
| T012 | Authelia `Remote-User` trust and CSRF/origin hardening | T010, T002 | done | [tasks/T012.md](tasks/T012.md) |
| T013 | Browser Use session manager (on-demand Chrome, persistent profile) | T004, T010 | done | [tasks/T013.md](tasks/T013.md) |
| T014 | Jev action space + Browser Use ↔ Jev adapter | T001 | done | [tasks/T014.md](tasks/T014.md) |
| T015 | Observe → Jev → execute control loop with audit hooks | T009, T013, T014 | done | [tasks/T015.md](tasks/T015.md) |
| T016 | Small text LLM gate for `TYPE_TEXT` only | T015 | done | [tasks/T016.md](tasks/T016.md) |
| T017 | Model-call and browser-action tracing tables/writers | T009, T015 | done | [tasks/T017.md](tasks/T017.md) |
| T018 | Compressed browser-state checkpoints on meaningful changes | T007, T015 | done | [tasks/T018.md](tasks/T018.md) |
| T019 | Screenshot policy (WebP/JPEG, event-driven capture) | T007, T015 | done | [tasks/T019.md](tasks/T019.md) |
| T020 | Pause, resume, cancel, and retry controls | T010, T015 | done | [tasks/T020.md](tasks/T020.md) |
| T021 | Human approval gates for high-impact actions | T011, T015, T020 | ready | [tasks/T021.md](tasks/T021.md) |
| T022 | Live agent Chrome view (Xvfb + noVNC) behind Traefik/Authelia | T004, T013, T012 | ready | [tasks/T022.md](tasks/T022.md) |
| T023 | Take-control / human-in-the-loop VNC handoff | T022, T020 | todo | [tasks/T023.md](tasks/T023.md) |
| T024 | Minimal Agent Web UI (run, watch, pause, approve, history) | T011, T012, T020, T021 | todo | [tasks/T024.md](tasks/T024.md) |
| T025 | End-to-end MVP smoke path and operator README | T015–T024 | todo | [tasks/T025.md](tasks/T025.md) |

## Phase 2 — Secrets, policies, observability

| ID | Title | Depends | Status | Guide |
| --- | --- | --- | --- | --- |
| T026 | Install Bitwarden into the persistent Chrome profile | T013 | ready | [tasks/T026.md](tasks/T026.md) |
| T027 | Explicit Bitwarden actions (`LOGIN` / `IDENTITY` / `CARD`) | T015, T026, T008 | todo | [tasks/T027.md](tasks/T027.md) |
| T028 | Richer approval policy engine (rules + UI reasons) | T021 | todo | [tasks/T028.md](tasks/T028.md) |
| T029 | Cost entries and simple cost dashboard API/UI | T017, T024 | todo | [tasks/T029.md](tasks/T029.md) |
| T030 | OpenTelemetry instrumentation alongside Postgres audit | T015 | ready | [tasks/T030.md](tasks/T030.md) |
| T031 | Multi-profile support (Personal / Work / Testing) | T013, T024 | todo | [tasks/T031.md](tasks/T031.md) |

## Phase 3 — Evaluation and storage maturity

| ID | Title | Depends | Status | Guide |
| --- | --- | --- | --- | --- |
| T032 | Offline Jev evaluation harness from recorded states | T014, T018 | ready | [tasks/T032.md](tasks/T032.md) |
| T033 | Artifact retention policy and optional state diffs | T018, T019 | ready | [tasks/T033.md](tasks/T033.md) |
| T034 | Optional MinIO backend for artifact storage | T007 | ready | [tasks/T034.md](tasks/T034.md) |
| T035 | Periodic signed audit checkpoints | T009 | ready | [tasks/T035.md](tasks/T035.md) |

## Recommended dispatch order (critical path)

```text
T001 → T002 → T003 → T006 → T008 → T009 → T010 → T011
                 ↘ T004 → T013 ↗
T001 → T014 ─────────────────────→ T015 → T016/T017/T018/T019
T002 → T012 → T024 (with T020/T021)
T004 → T022 → T023
T015…T024 → T025 (MVP exit)
then Phase 2 / 3 as needed
```

## Out of scope for all Phase 1 tasks unless stated

- Exposing raw CDP publicly
- Continuous video recording (default off)
- Forcing Firefox as the agent browser
- Putting passwords/CVVs/cookies into Jev prompts or audit payloads
- Rewriting Traefik/Authelia global config for a normal `*.docker.home.arpa` app
- Replacing Browser Use’s Chrome/CDP stack
