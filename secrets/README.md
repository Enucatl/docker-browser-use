# Secrets

Put local secret files in this directory. Real secret values stay out of git
(`.gitignore` ignores everything here except this README and `.gitkeep`).

## Expected files

| File | Used by | How to generate |
| --- | --- | --- |
| `postgres_password` | Compose `db` + `controller` | `openssl rand -hex 32 > secrets/postgres_password` |

Example:

```bash
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password
chmod 700 secrets
chmod 600 secrets/postgres_password
```

Do **not** commit `secrets/postgres_password`. Compose mounts it as the Docker
secret `postgres_password` (`POSTGRES_PASSWORD_FILE` / `DATABASE_PASSWORD_FILE`).

On `docker.home.arpa`, POSIX ACLs for these paths may be managed in
`../puppet-control-repo/data/nodes/docker.yaml`.
