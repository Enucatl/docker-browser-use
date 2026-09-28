# Authentication and CSRF (T012)

The controller trusts Authelia forward-auth identity headers and hardens
browser-facing Origin/Host checks for the private UI/API.

## Production path

Traefik labels use
`middlewares=browser-use-strip-identity@docker,authelia@docker,secured@file`.
The first middleware removes all four incoming `Remote-*` identity headers.
After Authelia allows the request, Traefik injects its response headers:

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
2. The controller and noVNC use the dedicated `browser_use_proxy` bridge with
   Traefik. Neither joins the shared `traefik_proxy` network, so unrelated
   containers there cannot connect directly to either backend.
3. Clients on the internal Compose network are treated as part of the trusted
   operator plane (same assumption as other homelab apps).

The application does not check the TCP peer: internal peers and the Docker host
can still supply identity headers. Keep unrelated containers off both Browser
Use networks. NoVNC uses the same identity stripping and Authelia chain.
This follows Authelia's [trusted remote networks guidance](https://www.authelia.com/integration/trusted-header-sso/introduction/)
and Traefik's [empty-value header removal](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/headers/).

## Network setup and migration

The sibling Traefik Compose project owns the named `browser_use_proxy` network;
Browser Use declares it external. Docker assigns addresses automatically; no
subnet or source-IP allowlist is configured. The bridge retains outbound access
for the controller's model APIs; the `default` network remains internal.

Apply the updated Traefik Compose file first, then recreate the Browser Use
ingress services to remove their old shared-network attachments:

```bash
cd /opt/docker/traefik
COMPOSE_ENV_FILES=../.env docker compose up -d traefik
cd /opt/docker/browser-use
COMPOSE_ENV_FILES=../.env docker compose up -d --no-deps controller novnc
docker network inspect browser_use_proxy --format '{{range .Containers}}{{.Name}} {{end}}'
```

The dedicated network should contain only Traefik, controller and noVNC. Verify
controller and noVNC are absent from `docker network inspect traefik_proxy`, then
check UI and `/vnc/` login through Authelia. Both Compose changes are required;
editing the files alone does not change running containers.

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
