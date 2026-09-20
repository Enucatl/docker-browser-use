# Authentication and CSRF (T012)

The controller trusts Authelia forward-auth identity headers and hardens
browser-facing Origin/Host checks for the private UI/API.

## Production path

Traefik labels use `middlewares=authelia@docker,secured@file`. After Authelia
allows the request, Traefik injects:

| Header | Meaning |
| --- | --- |
| `Remote-User` | Username (required when `AUTH_REQUIRED=true`) |
| `Remote-Groups` | Comma-separated groups |
| `Remote-Name` | Display name |
| `Remote-Email` | Email |

Compose sets `AUTH_REQUIRED=true`, `ALLOWED_HOSTS=browser-use.${DOCKER_DOMAIN},…`,
and `CSRF_TRUSTED_ORIGINS=https://browser-use.${DOCKER_DOMAIN}`. Unsafe HTTP
methods and WebSocket upgrades with a browser `Origin` must match that origin.

`/healthz` stays unauthenticated so Docker healthchecks on `127.0.0.1` work.

## Spoofing model

`Remote-User` (and related headers) are **not** cryptographic. Anyone who can
speak HTTP to the controller process can set them. Trust is valid only because:

1. The controller does not publish host ports (`ports: []`).
2. The only public ingress is Traefik on `traefik_proxy` with Authelia forward-auth.
3. Clients on the internal Compose network are treated as part of the trusted
   operator plane (same assumption as other homelab apps).

Do not expose the controller without Authelia. Do not treat these headers as
proof of identity on the open internet.

## Local development bypass

Leave `AUTH_REQUIRED` unset or `false` for bare uvicorn and pytest (the default
outside Compose). Then identity resolution accepts, in order:

1. `Remote-User` (if you inject it yourself)
2. `X-Browser-Use-Dev-User`
3. Stub principal `anonymous`

**Never** set `AUTH_REQUIRED=false` in the production Compose service env.
Use that only on a developer laptop or in tests.
