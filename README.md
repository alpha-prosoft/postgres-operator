# postgres-operator

Lightweight Kubernetes operator (kopf) that creates Postgres databases and
credential Secrets from a `DatabaseInstance` custom resource.

The operator **never deletes and never overrides**: every action is a guarded
"create-if-missing". If the database already exists, it does nothing. If the
target secret already exists, it does nothing. There are no finalizers — when a
`DatabaseInstance` is deleted, nothing is dropped from Postgres.

## How it works

1. The operator reads master Postgres credentials from a Secret named by the
   `POSTGRES_MASTER_SECRET` env var. Required keys: `host`, `username`,
   `password`. Optional: `port` (default `5432`), `database` (default
   `postgres`), `sslmode` (default `prefer`).
2. For each `DatabaseInstance`:
   - If the target Secret exists → use its `username`/`password`.
   - Otherwise generate a 32-char random password.
   - If the role doesn't exist → `CREATE ROLE … LOGIN PASSWORD …`.
   - If the database doesn't exist → `CREATE DATABASE … OWNER …`.
   - If the target Secret doesn't exist → create it with `host`, `port`,
     `database`, `username`, `password`, `url`.
3. Status is written to `.status.phase` (`Ready` / `Degraded`),
   `.status.message`, `.status.health`, and `.status.conditions[Ready]` so
   ArgoCD shows the resource health correctly.

If a role already exists but the target secret is missing, the operator marks
the instance `Degraded` (it cannot reconstruct the password and refuses to
override).

## Example

```yaml
apiVersion: postgres.alpha-prosoft.com/v1
kind: DatabaseInstance
metadata:
  name: my-app-db
  namespace: my-app
spec:
  databaseName: my_app
  targetSecretName: my-app-db-credentials
  # Optional:
  # username: my_app_user
  # targetSecretNamespace: my-app
```

## Install

```sh
helm install postgres-operator oci://docker.io/alphaprosoft/postgres-operator-helm \
  --namespace postgres-operator --create-namespace \
  --set masterSecret.name=postgres-master
```

The master secret must exist before any `DatabaseInstance` is reconciled:

```sh
kubectl -n postgres-operator create secret generic postgres-master \
  --from-literal=host=postgres.example.svc \
  --from-literal=port=5432 \
  --from-literal=username=postgres \
  --from-literal=password=... \
  --from-literal=database=postgres
```

## Layout

- `pg_operator/` — operator source (kopf handlers).
- `helm/postgres-operator/` — Helm chart (CRD, RBAC, Deployment, SA).
  Run `helm template helm/postgres-operator` to render the raw manifests if you need the CRD outside Helm.
- `Dockerfile` — runtime image (`python:3.12-slim` + kopf).
- `.github/workflows/build.yml` — image + chart push to DockerHub OCI.

## CI secrets / variables

- `secrets.DOCKER_PUSH_USERNAME` — DockerHub username; doubles as the
  namespace when `DOCKER_PUSH_URL` is host-only.
- `secrets.DOCKER_PUSH_PASSWORD` — DockerHub access token.
- `vars.DOCKER_PUSH_URL` — registry. Either host-only (`docker.io`) or
  host+namespace (`docker.io/myorg`). Host-only means images push to
  `docker.io/{username}/postgres-operator`.

These can live at organization level — GitHub Actions resolves org-level
`secrets`/`vars` automatically as long as the repo is granted access in
**Org → Settings → Secrets and variables → Actions → Repository access**.
