"""Postgres operator.

Watches DatabaseInstance resources and creates a Postgres database, role and
target Secret. Never deletes or overrides existing state — every action is a
guarded "create-if-missing".
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import kopf
import psycopg
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

GROUP = "postgres.alpha-prosoft.com"
VERSION = "v1"
PLURAL = "databaseinstances"
KIND = "DatabaseInstance"

MASTER_SECRET_ENV = "POSTGRES_MASTER_SECRET"
MASTER_SECRET_NAMESPACE_ENV = "POSTGRES_MASTER_SECRET_NAMESPACE"
OPERATOR_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _operator_namespace() -> str:
    ns = os.environ.get(MASTER_SECRET_NAMESPACE_ENV)
    if ns:
        return ns
    if os.path.exists(OPERATOR_NAMESPACE_FILE):
        with open(OPERATOR_NAMESPACE_FILE) as f:
            return f.read().strip()
    return "default"


def _decode_secret(secret) -> dict:
    return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}


def _load_master_credentials() -> dict:
    secret_name = os.environ.get(MASTER_SECRET_ENV)
    if not secret_name:
        raise RuntimeError(f"{MASTER_SECRET_ENV} env var not set")
    namespace = _operator_namespace()
    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(secret_name, namespace)
    data = _decode_secret(secret)
    missing = [k for k in ("username", "password", "host") if k not in data]
    if missing:
        raise RuntimeError(
            f"Master secret {namespace}/{secret_name} missing keys: {missing}"
        )
    return {
        "host": data["host"],
        "port": int(data.get("port", "5432")),
        "user": data["username"],
        "password": data["password"],
        "database": data.get("database", "postgres"),
        "sslmode": data.get("sslmode", "prefer"),
    }


def _connect_master():
    creds = _load_master_credentials()
    conn = psycopg.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        dbname=creds["database"],
        sslmode=creds["sslmode"],
        autocommit=True,
        connect_timeout=10,
    )
    return conn, creds


def _generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _user_exists(conn, username: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
        return cur.fetchone() is not None


def _database_exists(conn, dbname: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        return cur.fetchone() is not None


def _read_secret(namespace: str, name: str) -> Optional[dict]:
    v1 = client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    return _decode_secret(secret)


def _create_secret(namespace: str, name: str, data: dict) -> None:
    v1 = client.CoreV1Api()
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={"app.kubernetes.io/managed-by": "postgres-operator"},
        ),
        type="Opaque",
        string_data=data,
    )
    v1.create_namespaced_secret(namespace, body)


def _quote_ident(name: str) -> str:
    if not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"invalid identifier: {name!r}")
    return '"' + name + '"'


def _set_status(patch, phase: str, message: str, ready: bool) -> None:
    patch.status["phase"] = phase
    patch.status["message"] = message
    patch.status["observedAt"] = _now()
    patch.status["health"] = {
        "status": "Healthy" if ready else "Degraded",
        "message": message,
    }
    patch.status["conditions"] = [
        {
            "type": "Ready",
            "status": "True" if ready else "False",
            "reason": phase,
            "message": message,
            "lastTransitionTime": _now(),
        }
    ]


def _degraded(patch, logger, prev_phase: Optional[str], message: str) -> None:
    if prev_phase != "Degraded":
        logger.warning(message)
    _set_status(patch, "Degraded", message, ready=False)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.persistence.finalizer = None
    settings.posting.level = logging.WARNING
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, status, name, namespace, patch, logger, **_):
    prev_phase = (status or {}).get("phase")

    db_name = spec.get("databaseName")
    target_secret_name = spec.get("targetSecretName")
    if not db_name:
        _degraded(patch, logger, prev_phase, "spec.databaseName is required")
        return
    if not target_secret_name:
        _degraded(patch, logger, prev_phase, "spec.targetSecretName is required")
        return

    target_secret_namespace = spec.get("targetSecretNamespace", namespace)
    username = spec.get("username") or db_name

    try:
        _quote_ident(db_name)
        _quote_ident(username)
    except ValueError as e:
        _degraded(patch, logger, prev_phase, str(e))
        return

    try:
        conn, master = _connect_master()
    except Exception as e:
        _degraded(patch, logger, prev_phase, f"Cannot connect to master Postgres: {e}")
        return

    try:
        existing_secret = _read_secret(target_secret_namespace, target_secret_name)

        if existing_secret:
            password = existing_secret.get("password")
            if not password:
                _degraded(
                    patch,
                    logger,
                    prev_phase,
                    f"Secret {target_secret_namespace}/{target_secret_name} exists but has no 'password' key",
                )
                return
            secret_username = existing_secret.get("username")
            if secret_username:
                username = secret_username
                try:
                    _quote_ident(username)
                except ValueError as e:
                    _degraded(patch, logger, prev_phase, str(e))
                    return
        else:
            password = None

        user_exists = _user_exists(conn, username)
        db_exists = _database_exists(conn, db_name)

        if not existing_secret and user_exists:
            _degraded(
                patch,
                logger,
                prev_phase,
                f"Role '{username}' already exists but target secret is missing — refusing to override the password",
            )
            return

        if password is None:
            password = _generate_password()

        actions = []

        if not user_exists:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE ROLE {_quote_ident(username)} WITH LOGIN PASSWORD %s",
                    (password,),
                )
            actions.append(f"role '{username}'")

        if not db_exists:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE {_quote_ident(db_name)} OWNER {_quote_ident(username)}"
                )
            actions.append(f"database '{db_name}'")

        if not existing_secret:
            secret_data = {
                "host": master["host"],
                "port": str(master["port"]),
                "database": db_name,
                "username": username,
                "password": password,
                "url": f"postgresql://{username}:{password}@{master['host']}:{master['port']}/{db_name}",
            }
            _create_secret(target_secret_namespace, target_secret_name, secret_data)
            actions.append(f"secret '{target_secret_namespace}/{target_secret_name}'")

        if actions:
            logger.info(f"Created: {', '.join(actions)}")
        elif prev_phase != "Ready":
            logger.info(
                f"In sync (database='{db_name}', user='{username}', secret='{target_secret_namespace}/{target_secret_name}')"
            )

        _set_status(
            patch,
            "Ready",
            f"database='{db_name}' user='{username}' secret='{target_secret_namespace}/{target_secret_name}'",
            ready=True,
        )
    except Exception as e:
        _degraded(patch, logger, prev_phase, f"Reconcile failed: {e}")
    finally:
        conn.close()
