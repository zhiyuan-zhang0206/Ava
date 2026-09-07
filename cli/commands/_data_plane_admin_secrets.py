"""One-time, post-migration upgrade from bearer-backed data-plane credentials.

``AVA_CLUSTER_SECRET`` historically authenticated the owner Postgres role,
Redis ``default`` user, and runtime Redis ACL user. The split is an external
multi-system transition, so its generated target is journaled before the first
mutation and replayed until the unit env is committed.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

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
    redis_identity,
)
from shared.cluster.derive import REDIS_PASSWORD_ENV
from shared.config import settings
from shared.config.data_plane import DataPlaneSettings
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.platform import file_lock
from shared.private_storage import write_private_bytes
from shared.rollout_handoff import update_process_env
from shared.url_secret import url_host, url_with_password

_TOKEN_BYTES = 32
_TRANSITION_FILE = "data-plane-credential-split.json"
_TRANSITION_LOCK_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class _Transition:
    """Frozen v1 handoff payload; changing fields requires a new marker version."""

    db_admin_password: str
    redis_admin_password: str
    redis_password: str
    db_url: str
    redis_url: str


def _transition_path() -> Path:
    return Path(ava_home()) / "run" / _TRANSITION_FILE


def _transition_lock_path() -> Path:
    return _transition_path().with_suffix(".lock")


def _read_transition() -> _Transition | None:
    path = _transition_path()
    try:
        raw: object = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(raw, dict):
        raise TypeError(f"malformed data-plane credential transition: {path}")
    payload = cast("dict[object, object]", raw)
    expected = {
        "db_admin_password",
        "redis_admin_password",
        "redis_password",
        "db_url",
        "redis_url",
    }
    if set(payload) != expected:
        raise ValueError(f"malformed data-plane credential transition: {path}")

    def _required_string(key: str) -> str:
        value = payload[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"malformed data-plane credential transition: {path}")
        return value

    return _Transition(
        db_admin_password=_required_string("db_admin_password"),
        redis_admin_password=_required_string("redis_admin_password"),
        redis_password=_required_string("redis_password"),
        db_url=_required_string("db_url"),
        redis_url=_required_string("redis_url"),
    )


def _write_transition(transition: _Transition) -> None:
    payload = json.dumps(asdict(transition), separators=(",", ":"), sort_keys=True)
    write_private_bytes(_transition_path(), payload.encode())


def pending_data_plane_bootstrap_credentials() -> tuple[str, str, str] | None:
    """Journaled DB-admin, Redis-admin, and Redis-runtime values for native bring-up."""
    transition = _read_transition()
    if transition is None:
        return None
    return (
        transition.db_admin_password,
        transition.redis_admin_password,
        transition.redis_password,
    )


def _apply_transition_to_process(transition: _Transition) -> None:
    update_process_env(
        {
            "AVA_DB_ADMIN_PASSWORD": transition.db_admin_password,
            "AVA_REDIS_ADMIN_PASSWORD": transition.redis_admin_password,
            REDIS_PASSWORD_ENV: transition.redis_password,
            "AVA_DB_URL": transition.db_url,
            "AVA_REDIS_URL": transition.redis_url,
        }
    )
    refreshed = DataPlaneSettings()  # pyright: ignore[reportCallIssue] — transition supplies aliases
    settings.data_plane.db_admin_password = refreshed.db_admin_password
    settings.data_plane.redis_admin_password = refreshed.redis_admin_password
    settings.data_plane.db_url = refreshed.db_url
    settings.data_plane.redis_url = refreshed.redis_url


def _mint_or_existing(values: dict[str, str | None], key: str) -> str:
    return (values.get(key) or "").strip() or secrets.token_urlsafe(_TOKEN_BYTES)


def _working_redis_admin_password(
    redis_host: str, redis_port: int, candidates: tuple[str, ...]
) -> str:
    """Return the first known password Redis currently accepts for ``default``."""
    import redis

    def _works(password: str) -> bool:
        try:
            with redis.Redis(
                host=redis_host,
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


def _ensure_data_plane_admin_secrets_unlocked(*, allow_legacy_upgrade: bool) -> bool:
    """Mint or replay split credentials while the transition lock is held."""
    secret = settings.data_plane.cluster_secret
    if not secret:
        return False

    env_path = Path(ava_home()) / ".env"
    values = dotenv_values(env_path)
    transition = _read_transition()
    missing = (
        not (values.get("AVA_DB_ADMIN_PASSWORD") or "").strip()
        or not (values.get("AVA_REDIS_ADMIN_PASSWORD") or "").strip()
        or not (values.get(REDIS_PASSWORD_ENV) or "").strip()
    )
    if not missing and transition is None:
        return False
    if transition is None and not allow_legacy_upgrade:
        print(
            "  · deferred legacy data-plane credential split until a handoff-capable "
            "rollout parent is installed"
        )
        return False

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("no cluster registry record — cannot split data-plane credentials")
    identity = identity_from_url(settings.data_plane.db_url)
    pg_port = record_postgres_port(record)
    redis_port = record_redis_port(record)
    # The admin dials go to the host THIS cluster's redis_url names — the URL
    # is the single dial source (self-host URLs are loopback-rewritten by
    # DataPlaneSettings; an externally-hosted data plane is dialed at its own
    # address, Task #1752). Loopback fallback for a URL without a host.
    redis_host = url_host(settings.data_plane.redis_url)

    if transition is None:
        db_admin_password = _mint_or_existing(values, "AVA_DB_ADMIN_PASSWORD")
        redis_admin_password = _mint_or_existing(values, "AVA_REDIS_ADMIN_PASSWORD")
        redis_password = _mint_or_existing(values, REDIS_PASSWORD_ENV)
        raw_db_url = (values.get("AVA_DB_URL") or settings.data_plane.db_url).strip()
        raw_redis_url = (values.get("AVA_REDIS_URL") or settings.data_plane.redis_url).strip()
        transition = _Transition(
            db_admin_password=db_admin_password,
            redis_admin_password=redis_admin_password,
            redis_password=redis_password,
            db_url=url_with_password(raw_db_url, db_admin_password),
            redis_url=url_with_password(raw_redis_url, redis_password),
        )
        # Commit the complete target before the first external mutation. A hard
        # kill below leaves one secret set for this parent or the next start.
        _write_transition(transition)

    db_admin_password = transition.db_admin_password
    redis_admin_password = transition.redis_admin_password
    redis_password = transition.redis_password

    ensure_cluster_role(
        identity,
        base_admin_url=pg_admin_url(pg_port),
        db_admin_password=db_admin_password,
    )

    import redis

    current_redis_admin = _working_redis_admin_password(
        redis_host,
        redis_port,
        (
            (values.get("AVA_REDIS_ADMIN_PASSWORD") or "").strip(),
            settings.data_plane.redis_admin_password,
            secret,
            redis_admin_password,
        ),
    )
    with redis.Redis(
        host=redis_host,
        port=redis_port,
        username="default",
        password=current_redis_admin,
        socket_connect_timeout=3,
        socket_timeout=3,
    ) as client:
        client.execute_command(  # pyright: ignore[reportUnknownMemberType]
            "CONFIG", "SET", "requirepass", redis_admin_password
        )
        client.execute_command("CONFIG", "REWRITE")  # pyright: ignore[reportUnknownMemberType]

    ensure_cluster_redis_acl(
        redis_identity(),
        redis_admin_url=f"redis://default:{redis_admin_password}@{redis_host}:{redis_port}",
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

    upsert_env(
        env_path,
        {
            "AVA_DB_ADMIN_PASSWORD": db_admin_password,
            "AVA_REDIS_ADMIN_PASSWORD": redis_admin_password,
            REDIS_PASSWORD_ENV: redis_password,
            "AVA_DB_URL": transition.db_url,
            "AVA_REDIS_URL": transition.redis_url,
        },
        audit_site="data_plane_admin_secrets",
    )
    _apply_transition_to_process(transition)
    _transition_path().unlink()
    print("  ✓ split legacy data-plane credentials into owner, Redis-admin, and Redis-runtime")
    return True


def ensure_data_plane_admin_secrets(*, allow_legacy_upgrade: bool = True) -> bool:
    """Mint, activate, and persist split credentials as one replayable transition.

    No-op on a remote-managed data plane (Task #1752): the split is a
    local-instance concept (provisioning the owner role / Redis ACL against the
    per-cluster instance); a remote/SaaS plane authenticates through whatever
    the URLs carry and rotates credentials at the provider.
    """
    if settings.data_plane.is_remote:
        return False
    with file_lock(_transition_lock_path(), timeout_s=_TRANSITION_LOCK_TIMEOUT_S):
        return _ensure_data_plane_admin_secrets_unlocked(allow_legacy_upgrade=allow_legacy_upgrade)


def resume_pending_data_plane_admin_secrets() -> bool:
    """Finish a journaled split without initiating a new transition."""
    if _read_transition() is None:
        return False
    return ensure_data_plane_admin_secrets(allow_legacy_upgrade=True)
