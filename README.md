# postgres-operator

Lightweight Kubernetes operator (kopf) that creates Postgres databases and
credential Secrets from a `DatabaseInstance` custom resource.

The operator **never deletes and never overrides**: every action is a guarded
"create-if-missing". If the database already exists, it does nothing. If the
target secret already exists, it does nothing. There are no finalizers — when a
`DatabaseInstance` is deleted, nothing is dropped from Postgres.

## How it works

1. The operator reads the master Postgres `username`/`password` from a Secret
   named by `POSTGRES_MASTER_SECRET` (in the operator's own namespace). Host
   and connection options come from env vars: `POSTGRES_HOST` (required),
   `POSTGRES_PORT` (default `5432`), `POSTGRES_DATABASE` (default `postgres`),
   `POSTGRES_SSLMODE` (default `prefer`). At startup the operator runs a
   `SELECT version()` against the master and logs the result; a failed check
   is logged as a warning but does not crash the pod.
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

The chart renders a `SealedSecret` (bitnami-labs/sealed-secrets) holding the
master `username`/`password`. Encrypt the values with `kubeseal` first:

```sh
RELEASE_NS=postgres-operator
SECRET_NAME=postgres-master

ENC_USER=$(echo -n "postgres" | kubeseal --raw \
  --namespace "$RELEASE_NS" --name "$SECRET_NAME")
ENC_PASS=$(echo -n "<master-password>" | kubeseal --raw \
  --namespace "$RELEASE_NS" --name "$SECRET_NAME")

helm install postgres-operator oci://docker.io/alphaprosoft/postgres-operator-helm \
  --namespace "$RELEASE_NS" --create-namespace \
  --set masterSecret.host=postgres.example.svc \
  --set masterSecret.port=5432 \
  --set masterSecret.database=postgres \
  --set masterSecret.encryptedUsername="$ENC_USER" \
  --set masterSecret.encryptedPassword="$ENC_PASS"
```

If you provision the Secret out of band (e.g. external-secrets, manual
kubectl), set `masterSecret.create=false` and ensure a Secret named
`masterSecret.name` with keys `username`/`password` exists in the release
namespace before the operator pod starts.

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
