#!/usr/bin/env python3
"""Rotate independent data-plane credentials on a gateway host.

Dry-run is the default. ``--scope admin`` rotates the owner Postgres password
and Redis ``default``/requirepass password. ``--scope runner`` rotates the
least-privilege Postgres runner password and Redis ACL password. The default
rotates both. It never changes ``AVA_CLUSTER_SECRET``: that is the separate,
emergency-only control-plane bearer rotation.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import redis
from dotenv import dotenv_values

from cli.commands._cluster_instance import pg_admin_url
from cli.commands._pgbouncer import ensure_pgbouncer, pgbouncer_reachable
from shared.cluster import (
    RUNNER_DB_PASSWORD_ENV,
    RUNNER_ROLE,
    ensure_cluster_redis_acl,
    ensure_cluster_role,
    ensure_runner_role,
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
from shared.url_secret import url_host, url_with_password, url_with_userinfo

_TOKEN_BYTES = 32
_SCOPES = frozenset({"admin", "runner", "both"})
_GATEWAY_CONTEXT_ERROR = (
    "data-plane secret rotation must run on the gateway host in a gateway context, not from "
    "an agent shell. Run:\n"
    "cd <gateway checkout (e.g. ~/.ava/source)> && unset AVA_PROCESS_PROFILE && set -a; "
    ". ~/.ava/.env; set +a && .venv/bin/python scripts/rotate_data_plane_secrets.py ..."
)


@dataclass
class RotationState:
    """All mutable values needed to resume safely. Kept in a 0600 file because
    it includes both the old and replacement data-plane passwords."""

    scope: str
    identity: str
    old_db_admin_password: str
    new_db_admin_password: str
    old_redis_admin_password: str
    new_redis_admin_password: str
    old_runner_db_password: str
    new_runner_db_password: str
    old_redis_password: str
    new_redis_password: str
    pg_port: int
    redis_port: int
    pgbouncer_enabled: bool
    pgbouncer_port: int
    # The hosts the probes/dials go to, derived from this cluster's own URLs at
    # build_state (Task #1752 external data plane). Defaulted loopback so a
    # journal written before these fields existed resumes unchanged.
    pg_host: str = "127.0.0.1"
    redis_host: str = "127.0.0.1"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    phase: str = "minted"

    def path(self) -> Path:
        stamp = self.started_at.replace(":", "").replace("+00:00", "Z")
        return ava_home() / "backups" / "secret-rotation" / f"data-plane-{stamp}.json"

    def save(self) -> Path:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)
        return path

    @classmethod
    def load(cls, path: Path) -> RotationState:
        return cls(**json.loads(path.read_text()))

    @property
    def rotates_admin(self) -> bool:
        return self.scope in {"admin", "both"}

    @property
    def rotates_runner(self) -> bool:
        return self.scope in {"runner", "both"}

    @property
    def db_admin_password(self) -> str:
        return self.new_db_admin_password if self.rotates_admin else self.old_db_admin_password

    @property
    def redis_admin_password(self) -> str:
        return (
            self.new_redis_admin_password if self.rotates_admin else self.old_redis_admin_password
        )

    @property
    def runner_db_password(self) -> str:
        return self.new_runner_db_password if self.rotates_runner else self.old_runner_db_password

    @property
    def redis_password(self) -> str:
        return self.new_redis_password if self.rotates_runner else self.old_redis_password


def mint_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _env_password(values: dict[str, str | None], key: str, fallback: str) -> str:
    return (values.get(key) or "").strip() or fallback


def _gateway_identity() -> str:
    if settings.profile == "agent":
        raise RuntimeError(_GATEWAY_CONTEXT_ERROR)
    identity = identity_from_url(settings.data_plane.db_url)
    if identity == RUNNER_ROLE:
        raise RuntimeError(_GATEWAY_CONTEXT_ERROR)
    return identity


def build_state(scope: str = "both") -> RotationState:
    if scope not in _SCOPES:
        raise ValueError(f"unsupported scope {scope!r}")
    identity = _gateway_identity()
    if not settings.data_plane.cluster_secret:
        raise RuntimeError("this is a no-auth cluster; it has no data-plane passwords to rotate")
    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("no cluster registry record — cannot resolve data-plane ports")
    values = dotenv_values(ava_home() / ".env")
    bearer = settings.data_plane.cluster_secret
    old_db_admin = _env_password(values, "AVA_DB_ADMIN_PASSWORD", bearer)
    old_redis_admin = _env_password(values, "AVA_REDIS_ADMIN_PASSWORD", bearer)
    old_runner_db = _env_password(values, RUNNER_DB_PASSWORD_ENV, "")
    old_redis_runtime = _env_password(values, REDIS_PASSWORD_ENV, bearer)
    if scope in {"runner", "both"} and not old_runner_db:
        raise RuntimeError(
            f"{RUNNER_DB_PASSWORD_ENV} is missing; run `ava cluster ensure-db-role` before "
            "rotating runner credentials"
        )
    return RotationState(
        scope=scope,
        identity=identity,
        old_db_admin_password=old_db_admin,
        new_db_admin_password=mint_secret() if scope in {"admin", "both"} else old_db_admin,
        old_redis_admin_password=old_redis_admin,
        new_redis_admin_password=(mint_secret() if scope in {"admin", "both"} else old_redis_admin),
        old_runner_db_password=old_runner_db,
        new_runner_db_password=(mint_secret() if scope in {"runner", "both"} else old_runner_db),
        old_redis_password=old_redis_runtime,
        new_redis_password=(mint_secret() if scope in {"runner", "both"} else old_redis_runtime),
        pg_port=record_postgres_port(record),
        redis_port=record_redis_port(record),
        pg_host=url_host(settings.data_plane.db_url),
        redis_host=url_host(settings.data_plane.redis_url),
        pgbouncer_enabled=settings.data_plane.pgbouncer_enabled,
        pgbouncer_port=record_pgbouncer_port(record),
    )


def _pg_probe(host: str, user: str, db_name: str, port: int, password: str) -> bool:
    try:
        with psycopg.connect(
            url_with_userinfo(f"postgresql://@{host}:{port}/{db_name}", user, password),
            connect_timeout=3,
        ) as connection:
            connection.execute("SELECT 1")
        return True
    except Exception:
        return False


def _redis_probe(host: str, port: int, password: str, *, username: str) -> bool:
    try:
        with redis.Redis(
            host=host,
            port=port,
            username=username,
            password=password,
            socket_connect_timeout=3,
            socket_timeout=3,
        ) as client:
            return bool(client.ping())
    except Exception:
        return False


def _pgbouncer_probe(state: RotationState, role: str, password: str) -> bool:
    return pgbouncer_reachable(state.pgbouncer_port, state.identity, role, password)


def preflight(state: RotationState) -> bool:
    """Refuse to rotate over pre-existing credential drift."""
    checks = [
        (
            "Postgres owner",
            _pg_probe(
                state.pg_host,
                state.identity,
                state.identity,
                state.pg_port,
                state.old_db_admin_password,
            ),
        ),
        (
            "Redis default",
            _redis_probe(
                state.redis_host,
                state.redis_port,
                state.old_redis_admin_password,
                username="default",
            ),
        ),
    ]
    if state.rotates_runner:
        checks.extend(
            [
                (
                    "Postgres runner",
                    _pg_probe(
                        state.pg_host,
                        RUNNER_ROLE,
                        state.identity,
                        state.pg_port,
                        state.old_runner_db_password,
                    ),
                ),
                (
                    "Redis ACL user",
                    _redis_probe(
                        state.redis_host,
                        state.redis_port,
                        state.old_redis_password,
                        username=state.identity,
                    ),
                ),
            ]
        )
    if state.pgbouncer_enabled:
        checks.append(
            (
                "PgBouncer owner",
                _pgbouncer_probe(state, state.identity, state.old_db_admin_password),
            )
        )
        if state.rotates_runner:
            checks.append(
                (
                    "PgBouncer runner",
                    _pgbouncer_probe(state, RUNNER_ROLE, state.old_runner_db_password),
                )
            )
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    return all(ok for _label, ok in checks)


def _working_redis_admin_password(state: RotationState) -> str:
    for password in (state.new_redis_admin_password, state.old_redis_admin_password):
        if _redis_probe(state.redis_host, state.redis_port, password, username="default"):
            return password
    raise RuntimeError("Redis default user rejects both recorded admin passwords")


def _refresh_pgbouncer(state: RotationState) -> None:
    if not state.pgbouncer_enabled:
        return
    rc = ensure_pgbouncer(
        pg_port=state.pg_port,
        listen_port=state.pgbouncer_port,
        db_name=state.identity,
        role=state.identity,
        cluster_secret=settings.data_plane.cluster_secret,
        db_admin_password=state.db_admin_password,
        runner_password=state.runner_db_password,
    )
    if rc != 0:
        raise RuntimeError("PgBouncer userlist refresh failed")


def apply_admin(state: RotationState) -> None:
    """Rotate the owner role and Redis default user, keeping runtime users live."""
    if not state.rotates_admin:
        return
    ensure_cluster_role(
        state.identity,
        base_admin_url=pg_admin_url(state.pg_port),
        db_admin_password=state.new_db_admin_password,
    )
    current = _working_redis_admin_password(state)
    with redis.Redis(
        host=state.redis_host,
        port=state.redis_port,
        username="default",
        password=current,
        socket_connect_timeout=3,
        socket_timeout=3,
    ) as client:
        client.execute_command("CONFIG", "SET", "requirepass", state.new_redis_admin_password)
    _refresh_pgbouncer(state)


def apply_runner(state: RotationState) -> None:
    """Rotate both runner credentials and synchronize PgBouncer's userlist."""
    if not state.rotates_runner:
        return
    ensure_runner_role(
        state.identity,
        base_admin_url=pg_admin_url(state.pg_port),
        runner_password=state.new_runner_db_password,
    )
    admin_password = _working_redis_admin_password(state)
    ensure_cluster_redis_acl(
        state.identity,
        redis_admin_url=(f"redis://default:{admin_password}@{state.redis_host}:{state.redis_port}"),
        runtime_password=state.new_redis_password,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
    )
    _refresh_pgbouncer(state)


def verify(state: RotationState) -> None:
    checks = [
        (
            "Postgres owner",
            _pg_probe(
                state.pg_host,
                state.identity,
                state.identity,
                state.pg_port,
                state.db_admin_password,
            ),
        ),
        (
            "Redis default",
            _redis_probe(
                state.redis_host,
                state.redis_port,
                state.redis_admin_password,
                username="default",
            ),
        ),
    ]
    if state.rotates_runner:
        checks.extend(
            [
                (
                    "Postgres runner",
                    _pg_probe(
                        state.pg_host,
                        RUNNER_ROLE,
                        state.identity,
                        state.pg_port,
                        state.runner_db_password,
                    ),
                ),
                (
                    "Redis ACL user",
                    _redis_probe(
                        state.redis_host,
                        state.redis_port,
                        state.redis_password,
                        username=state.identity,
                    ),
                ),
            ]
        )
    if state.pgbouncer_enabled:
        checks.append(
            ("PgBouncer owner", _pgbouncer_probe(state, state.identity, state.db_admin_password))
        )
        if state.rotates_runner:
            checks.append(
                ("PgBouncer runner", _pgbouncer_probe(state, RUNNER_ROLE, state.runner_db_password))
            )
    failed = [label for label, ok in checks if not ok]
    if failed:
        raise RuntimeError(f"rotation verification failed: {', '.join(failed)}")


def write_env(state: RotationState) -> None:
    values = dotenv_values(ava_home() / ".env")
    db_url = url_with_password(
        (values.get("AVA_DB_URL") or settings.data_plane.db_url).strip(), state.db_admin_password
    )
    redis_url = url_with_password(
        (values.get("AVA_REDIS_URL") or settings.data_plane.redis_url).strip(), state.redis_password
    )
    upsert_env(
        ava_home() / ".env",
        {
            "AVA_DB_ADMIN_PASSWORD": state.db_admin_password,
            "AVA_REDIS_ADMIN_PASSWORD": state.redis_admin_password,
            RUNNER_DB_PASSWORD_ENV: state.runner_db_password,
            REDIS_PASSWORD_ENV: state.redis_password,
            "AVA_DB_URL": db_url,
            "AVA_REDIS_URL": redis_url,
        },
        audit_site="rotate_data_plane_secrets",
    )


def _run_phase(state: RotationState, phase: str, fn: Callable[[RotationState], None]) -> None:
    print(f"-> {phase}")
    fn(state)
    state.phase = phase
    state.save()
    print(f"   ✓ {phase}")


def print_plan(state: RotationState, *, dry_run: bool) -> None:
    print(f"scope:             {state.scope}")
    print(f"identity:          {state.identity!r}")
    print(f"postgres port:     {state.pg_port}")
    print(f"redis port:        {state.redis_port}")
    print(f"mode:              {'DRY RUN (read-only)' if dry_run else 'EXECUTE'}")
    if state.rotates_runner:
        print(
            "runner follow-up: refresh every enrolled runner after this rotation; "
            "the refreshed PgBouncer userlist alone cannot update cached runner URLs."
        )


def main(argv: list[str] | None = None) -> int:
    if settings.data_plane.is_remote:
        print(
            "✗ this cluster's data plane is remote-managed — rotation is a "
            "local-instance operation (ALTER ROLE / ACL / pooler userlist). A "
            "remote/SaaS plane rotates credentials at the provider; update "
            "AVA_DB_URL / AVA_REDIS_URL here.",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description="Rotate independent data-plane credentials.")
    parser.add_argument("--scope", choices=sorted(_SCOPES), default="both")
    parser.add_argument("--execute", action="store_true", help="perform the rotation")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--resume", metavar="STATE_FILE", help="resume a saved rotation")
    args = parser.parse_args(argv)
    try:
        _gateway_identity()
    except RuntimeError:
        print(_GATEWAY_CONTEXT_ERROR, file=sys.stderr)
        return 1
    state = RotationState.load(Path(args.resume)) if args.resume else build_state(args.scope)
    print_plan(state, dry_run=not args.execute)
    if not args.execute:
        preflight(state)
        print("\n[dry-run] no changes made.")
        return 0
    if not args.resume and not preflight(state):
        print("\n✗ refusing to rotate over pre-existing credential drift.", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"\nType 'rotate {state.scope}' to continue: ")
        if answer.strip() != f"rotate {state.scope}":
            print("aborted.")
            return 1

    try:
        _run_phase(state, "admin", apply_admin)
        _run_phase(state, "runner", apply_runner)
        _run_phase(state, "verified", verify)
        _run_phase(state, "env_written", write_env)
    except Exception as exc:
        state_path = state.save()
        print(f"\n✗ rotation failed at {state.phase!r}: {exc}", file=sys.stderr)
        print(f"  resume with --execute --resume {state_path}", file=sys.stderr)
        return 1

    print("\n✓ data-plane rotation complete.")
    if state.rotates_runner:
        print(
            "NEXT: restart every enrolled runner so it refetches the runner URLs and credentials."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
