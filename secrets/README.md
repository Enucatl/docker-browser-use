# Secrets

Put local secret files in this directory. Real secret values stay out of git
(`.gitignore` ignores everything here except this README and `.gitkeep`).

## Expected files

| File | Used by | How to create |
| --- | --- | --- |
| `postgres_password` | Compose `db` + `controller` | `openssl rand -hex 32 > secrets/postgres_password` |
| `jev_api_key` | Compose `controller` (`JEV_API_KEY_FILE`) | paste Jev bearer token (one line) |
| `openrouter_api_key` | Compose `controller` (`TEXT_LLM_API_KEY_FILE`) | paste OpenRouter API key (one line) |

Example:

```bash
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password
# Leave empty until you have live keys (empty → FakeJev / no text LLM):
: > secrets/jev_api_key
: > secrets/openrouter_api_key
# Or write real values:
# printf '%s\n' 'jv_live_…' > secrets/jev_api_key
# printf '%s\n' 'sk-or-…' > secrets/openrouter_api_key
chmod 700 secrets
chmod 600 secrets/*
```

Do **not** commit secret values. Compose mounts them as Docker secrets under
`/run/secrets/…` (`POSTGRES_PASSWORD_FILE` / `DATABASE_PASSWORD_FILE`,
`JEV_API_KEY_FILE`, `TEXT_LLM_API_KEY_FILE`).

On `docker.home.arpa`, POSIX ACLs for these paths are managed in
`../puppet-control-repo/data/nodes/docker.yaml` (remapped uid `100999`).
