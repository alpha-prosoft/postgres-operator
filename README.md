# postgres-operator

A tiny Kubernetes operator (kopf, ~250 LoC) that turns this:

```yaml
apiVersion: postgres.alpha-prosoft.com/v1
kind: DatabaseInstance
metadata:
  name: my-app-db
  namespace: my-app
spec:
  databaseName: my_app
  targetSecretName: my-app-db-credentials
```

…into a Postgres database, a role, and a Kubernetes Secret with connection
credentials.

**Create-only.** The operator never drops databases, never deletes Secrets,
never rotates passwords. Every action is guarded by an existence check —
re-running is always a no-op once the desired state is reached.

## Flow

```mermaid
flowchart LR
    User[User] -- kubectl apply --> CR[DatabaseInstance CR]
    CR -- watched --> Op[postgres-operator]
    Op -- read username/password --> M[(SealedSecret<br/>postgres-master)]
    Op -- CREATE ROLE / DB --> PG[(Postgres)]
    Op -- create if missing --> S[Target Secret<br/>host port user pass url]
    App[Your app] -- mounts --> S
    App -- connects --> PG
```

Reconcile decision per CR:

| Target Secret | Role | Database | Action |
|---|---|---|---|
| missing | missing | missing | generate password → create role + DB + Secret |
| missing | missing | exists | create role + Secret (DB left alone) |
| missing | **exists** | any | **Degraded** — can't recover password, refuse to override |
| exists | missing | any | reuse password from Secret → create role (+ DB if missing) |
| exists | exists | exists | no-op |

## Status

Status is written for ArgoCD-style health probes:

```yaml
status:
  phase: Ready                # Ready | Degraded
  message: "database='my_app' user='my_app' secret='my-app/my-app-db-credentials'"
  health:
    status: Healthy           # Healthy | Degraded
    message: ...
  conditions:
    - type: Ready
      status: "True"
      reason: Ready
      message: ...
```

A Lua health hook for ArgoCD (`resource.customizations.health.postgres.alpha-prosoft.com_DatabaseInstance`) is included with the chart so Argo shows the right colour in the UI.

## Install

The chart renders a `SealedSecret` for the master Postgres user. Pre-seal the
values with `kubeseal`:

```sh
RELEASE_NS=postgres-operator
SECRET=postgres-master

ENC_USER=$(echo -n "postgres" | kubeseal --raw -n "$RELEASE_NS" --name "$SECRET")
ENC_PASS=$(echo -n "<password>" | kubeseal --raw -n "$RELEASE_NS" --name "$SECRET")

helm install postgres-operator oci://docker.io/alphaprosoft/postgres-operator-helm \
  --namespace "$RELEASE_NS" --create-namespace \
  --set masterSecret.host=postgres.example.svc \
  --set masterSecret.encryptedUsername="$ENC_USER" \
  --set masterSecret.encryptedPassword="$ENC_PASS"
```

To bring your own Secret instead, set `masterSecret.create=false` and ensure a
Secret named `masterSecret.name` with keys `username`/`password` exists in the
release namespace before the operator starts.

### Configuration

| Helm value | Default | Notes |
|---|---|---|
| `masterSecret.host` | _required_ | FQDN of the master Postgres |
| `masterSecret.port` | `5432` | |
| `masterSecret.database` | `postgres` | DB to use for the admin connection |
| `masterSecret.sslmode` | `prefer` | psycopg sslmode |
| `masterSecret.create` | `true` | Render the SealedSecret |
| `masterSecret.encryptedUsername` | _required if `create`_ | kubeseal --raw output |
| `masterSecret.encryptedPassword` | _required if `create`_ | kubeseal --raw output |
| `image.repository` | `alphaprosoft/postgres-operator` | |
| `replicaCount` | `1` | |
| `resources` | small defaults | |

## Generated Secret

```
host       postgres.example.svc
port       5432
database   my_app
username   my_app
password   <32-char random>
url        postgresql://my_app:...@postgres.example.svc:5432/my_app
```

The Secret is **not** linked to the `DatabaseInstance` via `ownerReferences` —
that would cascade-delete it when the CR is removed, which we explicitly don't
want. The Secret stays put even if the CR is gone. Find it via
`app.kubernetes.io/managed-by=postgres-operator`.

## Layout

```
pg_operator/                     operator source (kopf handlers)
helm/postgres-operator/          Helm chart: CRD, RBAC, Deployment, SealedSecret
Dockerfile                       python:3.12-slim runtime
.github/workflows/build.yml      image + chart push (DockerHub OCI)
```

## CI

Repo (or org) needs:

- `secrets.DOCKER_PUSH_USERNAME` — also used as the namespace when
  `DOCKER_PUSH_URL` is host-only.
- `secrets.DOCKER_PUSH_PASSWORD`
- `vars.DOCKER_PUSH_URL` — `docker.io` or `docker.io/<org>`.

Image: `docker.io/{namespace}/postgres-operator:1.{count}-{branch}`
Chart: `oci://docker.io/{namespace}/postgres-operator-helm:1.{count}-{branch}`
