# Secrets

Put local secret files in this directory. Real secret values stay out of git
(`.gitignore` ignores everything here except this README and `.gitkeep`).

No secret files are required for the T002 compose skeleton. Later tasks (e.g.
PostgreSQL in T003) will document expected filenames.

Recommended permissions once secrets exist:

```bash
chmod 700 secrets
chmod 600 secrets/<secret-file>
```

On `docker.home.arpa`, POSIX ACLs for these paths may be managed in
`../puppet-control-repo/data/nodes/docker.yaml`.
