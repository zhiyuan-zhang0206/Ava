"""One-time, post-migration upgrade from bearer-backed data-plane credentials.

`AVA_CLUSTER_SECRET` used to authenticate the owner Postgres role, Redis
``default`` user, and runtime Redis ACL user. A gateway start can safely split
an existing authenticated cluster only after migrations: the data plane is up,
the schema is current, and no service session has been launched with the old
URLs yet. Fresh installs mint these values at birth and therefore skip this
module entirely.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

from cli.commands._cluster_instance import pg_admin_url
from shared.cluster import (
    ensure_cluster_redis_acl,
    ensure_cluster_role,
    get_record,
    identity_from_url,
    record_pgbouncer_port,
    record_postgres_port,
    record_redis_port,
)
from shared.cluster.derive import REDIS_PASSWORD_ENV
from shared.config import settings
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.url_secret import url_with_password

_TOKEN_BYTES = 32


def _mint_or_existing(values: dict[str, str | None], key: str) -> str:
    return (values.get(key) or "").strip() or secrets.token_urlsafe(_TOKEN_BYTES)


def _working_redis_admin_password(redis_port: int, candidates: tuple[str, ...]) -> str:
    """Return the password Redis currently accepts for its ``default`` user.

    The probe recognizes only passwords known to this process: gateway ``.env``
    values, in-memory settings, the bearer, and the freshly minted value. A
    crash after ``CONFIG SET requirepass`` but before the ``.env`` write requires
    restarting Redis, whose configuration still carries the previous
    ``requirepass``, then re-running ``ava start``.
    """
    import redis

    def _works(password: str) -> bool:
        try:
            with redis.Redis(
                host="127.0.0.1",
                port=redis_port,
                username="default",
                password=password,
                socket_connect_timeout=3,
                socket_timeout=3,
            ) as client:
                return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]
        except Exception:
            return False

    for password in dict.fromkeys(candidate for candidate in candidates if candidate):
        if _works(password):
            return password
    raise RuntimeError(
        f"redis :{redis_port} rejected every known admin password; refusing to split "
        "the data-plane credentials"
    )


def ensure_data_plane_admin_secrets() -> bool:
    """Mint and activate missing split data-plane credentials.

    Returns whether an upgrade was applied. It is deliberately a gateway-only
    call site: only the gateway home holds owner and Redis-admin credentials.
    Empty-bearer clusters remain unauthenticated by contract and are a no-op.
    """
    secret = settings.data_plane.cluster_secret
    if not secret:
        return False

    env_path = Path(ava_home()) / ".env"
    values = dotenv_values(env_path)
    missing = (
        not (values.get("AVA_DB_ADMIN_PASSWORD") or "").strip()
        or not (values.get("AVA_REDIS_ADMIN_PASSWORD") or "").strip()
        or not (values.get(REDIS_PASSWORD_ENV) or "").strip()
    )
    if not missing:
        return False

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("no cluster registry record — cannot split data-plane credentials")
    identity = identity_from_url(settings.data_plane.db_url)
    db_admin_password = _mint_or_existing(values, "AVA_DB_ADMIN_PASSWORD")
    redis_admin_password = _mint_or_existing(values, "AVA_REDIS_ADMIN_PASSWORD")
    redis_password = _mint_or_existing(values, REDIS_PASSWORD_ENV)
    pg_port = record_postgres_port(record)
    redis_port = record_redis_port(record)

    # Postgres provisioning uses the local trust socket. No network client is
    # left holding the old password before service sessions exist.
    ensure_cluster_role(
        identity,
        base_admin_url=pg_admin_url(pg_port),
        db_admin_password=db_admin_password,
    )

    import redis

    current_redis_admin = _working_redis_admin_password(
        redis_port,
        (
            (values.get("AVA_REDIS_ADMIN_PASSWORD") or "").strip(),
            settings.data_plane.redis_admin_password,
            secret,
            redis_admin_password,
        ),
    )
    with redis.Redis(
        host="127.0.0.1",
        port=redis_port,
        username="default",
        password=current_redis_admin,
        socket_connect_timeout=3,
        socket_timeout=3,
    ) as client:
        client.execute_command(  # pyright: ignore[reportUnknownMemberType]
            "CONFIG", "SET", "requirepass", redis_admin_password
        )

    ensure_cluster_redis_acl(
        identity,
        redis_admin_url=f"redis://default:{redis_admin_password}@127.0.0.1:{redis_port}",
        runtime_password=redis_password,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
    )

    if settings.data_plane.pgbouncer_enabled:
        from cli.commands._pgbouncer import ensure_pgbouncer, runner_password_from_env

        rc = ensure_pgbouncer(
            pg_port=pg_port,
            listen_port=record_pgbouncer_port(record),
            db_name=identity,
            role=identity,
            cluster_secret=secret,
            db_admin_password=db_admin_password,
            runner_password=runner_password_from_env(),
        )
        if rc != 0:
            raise RuntimeError("PgBouncer userlist refresh failed while splitting credentials")

    raw_db_url = (values.get("AVA_DB_URL") or settings.data_plane.db_url).strip()
    raw_redis_url = (values.get("AVA_REDIS_URL") or settings.data_plane.redis_url).strip()
    new_db_url = url_with_password(raw_db_url, db_admin_password)
    new_redis_url = url_with_password(raw_redis_url, redis_password)
    upsert_env(
        env_path,
        {
            "AVA_DB_ADMIN_PASSWORD": db_admin_password,
            "AVA_REDIS_ADMIN_PASSWORD": redis_admin_password,
            REDIS_PASSWORD_ENV: redis_password,
            "AVA_DB_URL": new_db_url,
            "AVA_REDIS_URL": new_redis_url,
        },
    )

    # The current start must use the split values too; child sessions inherit
    # this process environment and ordinary in-process callers use settings.
    os.environ.update(
        {
            "AVA_DB_ADMIN_PASSWORD": db_admin_password,
            "AVA_REDIS_ADMIN_PASSWORD": redis_admin_password,
            REDIS_PASSWORD_ENV: redis_password,
            "AVA_DB_URL": new_db_url,
            "AVA_REDIS_URL": new_redis_url,
        }
    )
    settings.data_plane.db_admin_password = db_admin_password
    settings.data_plane.redis_admin_password = redis_admin_password
    settings.data_plane.db_url = url_with_password(settings.data_plane.db_url, db_admin_password)
    settings.data_plane.redis_url = url_with_password(settings.data_plane.redis_url, redis_password)
    print("  ✓ split legacy data-plane credentials into owner, Redis-admin, and Redis-runtime")
    return True
