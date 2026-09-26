#!/usr/bin/env python3
"""Rotate the independent Redis data-plane credentials on a gateway host.

Dry-run is the default. ``--scope admin`` rotates the Redis ``default`` /
requirepass password; ``--scope runner`` rotates the Redis runtime ACL
password; the default rotates both. It never changes ``AVA_CLUSTER_SECRET``:
that is the separate, emergency-only control-plane bearer rotation.

PostgreSQL has no rotatable password here: the schema owner is NOLOGIN, the
administrator is the OS user over the owner-only socket, and application
logins are write generations that rotate with each release transition
(``shared.cluster.authority``). Redis credentials do not rotate per rollout;
this script is the explicit operator action
(decisions/2026-09-27-write-generation-rollout-choices.md).
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

import redis
from dotenv import dotenv_values

from shared.cluster import (
    ensure_cluster_redis_acl,
    get_record,
    record_redis_port,
    redis_identity,
)
from shared.cluster.derive import REDIS_PASSWORD_ENV
from shared.config import settings
from shared.envfile import upsert_env
from shared.paths import ava_home
from shared.url_secret import url_host, url_with_password

_TOKEN_BYTES = 32
_SCOPES = frozenset({"admin", "runner", "both"})
_GATEWAY_CONTEXT_ERROR = (
    "data-plane secret rotation must run on the gateway host in a gateway context, not from "
    "an agent shell. Run:\n"
    "cd <gateway checkout (e.g. ~/.ava/source)> && unset AVA_PROCESS_PROFILE && "
    ".venv/bin/python scripts/rotate_data_plane_secrets.py ..."
)


@dataclass
class RotationState:
    """All mutable values needed to resume safely. Kept in a 0600 file because
    it includes both the old and replacement Redis passwords."""

    scope: str
    old_redis_admin_password: str
    new_redis_admin_password: str
    old_redis_password: str
    new_redis_password: str
    redis_port: int
    redis_host: str
    redis_user: str
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
        try:
            return cls(**json.loads(path.read_text()))
        except TypeError as exc:
            raise RuntimeError(
                f"{path} is not a Redis-only rotation journal (Postgres credentials no "
                "longer rotate here); start a new rotation"
            ) from exc

    @property
    def rotates_admin(self) -> bool:
        return self.scope in {"admin", "both"}

    @property
    def rotates_runner(self) -> bool:
        return self.scope in {"runner", "both"}

    @property
    def redis_admin_password(self) -> str:
        return (
            self.new_redis_admin_password if self.rotates_admin else self.old_redis_admin_password
        )

    @property
    def redis_password(self) -> str:
        return self.new_redis_password if self.rotates_runner else self.old_redis_password


def mint_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _require_gateway_context() -> None:
    if settings.profile == "agent":
        raise RuntimeError(_GATEWAY_CONTEXT_ERROR)


def build_state(scope: str = "both") -> RotationState:
    if scope not in _SCOPES:
        raise ValueError(f"unsupported scope {scope!r}")
    _require_gateway_context()
    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("no cluster registry record — cannot resolve data-plane ports")
    values = dotenv_values(ava_home() / ".env")
    old_admin = (values.get("AVA_REDIS_ADMIN_PASSWORD") or "").strip()
    old_runtime = (values.get(REDIS_PASSWORD_ENV) or "").strip()
    if not (old_admin and old_runtime):
        raise RuntimeError(
            "this home records no Redis credentials; convert it first with "
            "scripts/cutover_db_authority.py"
        )
    return RotationState(
        scope=scope,
        old_redis_admin_password=old_admin,
        new_redis_admin_password=mint_secret() if scope in {"admin", "both"} else old_admin,
        old_redis_password=old_runtime,
        new_redis_password=mint_secret() if scope in {"runner", "both"} else old_runtime,
        redis_port=record_redis_port(record),
        redis_host=url_host(settings.data_plane.redis_url),
        redis_user=redis_identity(),
    )


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


def _checks(state: RotationState, admin: str, runtime: str) -> list[tuple[str, bool]]:
    checks = [
        (
            "Redis default",
            _redis_probe(state.redis_host, state.redis_port, admin, username="default"),
        )
    ]
    if state.rotates_runner:
        checks.append(
            (
                "Redis ACL user",
                _redis_probe(
                    state.redis_host, state.redis_port, runtime, username=state.redis_user
                ),
            )
        )
    return checks


def preflight(state: RotationState) -> bool:
    """Refuse to rotate over pre-existing credential drift."""
    checks = _checks(state, state.old_redis_admin_password, state.old_redis_password)
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    return all(ok for _label, ok in checks)


def _working_redis_admin_password(state: RotationState) -> str:
    for password in (state.new_redis_admin_password, state.old_redis_admin_password):
        if _redis_probe(state.redis_host, state.redis_port, password, username="default"):
            return password
    raise RuntimeError("Redis default user rejects both recorded admin passwords")


def apply_admin(state: RotationState) -> None:
    """Rotate the Redis default user, keeping the runtime ACL user live."""
    if not state.rotates_admin:
        return
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


def apply_runner(state: RotationState) -> None:
    """Rotate the Redis runtime ACL password."""
    if not state.rotates_runner:
        return
    admin_password = _working_redis_admin_password(state)
    ensure_cluster_redis_acl(
        state.redis_user,
        redis_admin_url=(f"redis://default:{admin_password}@{state.redis_host}:{state.redis_port}"),
        runtime_password=state.new_redis_password,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
    )


def verify(state: RotationState) -> None:
    failed = [
        label
        for label, ok in _checks(state, state.redis_admin_password, state.redis_password)
        if not ok
    ]
    if failed:
        raise RuntimeError(f"rotation verification failed: {', '.join(failed)}")


def write_env(state: RotationState) -> None:
    values = dotenv_values(ava_home() / ".env")
    redis_url = url_with_password(
        (values.get("AVA_REDIS_URL") or settings.data_plane.redis_url).strip(), state.redis_password
    )
    upsert_env(
        ava_home() / ".env",
        {
            "AVA_REDIS_ADMIN_PASSWORD": state.redis_admin_password,
            REDIS_PASSWORD_ENV: state.redis_password,
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
    print(f"redis user:        {state.redis_user!r}")
    print(f"redis port:        {state.redis_port}")
    print(f"mode:              {'DRY RUN (read-only)' if dry_run else 'EXECUTE'}")
    if state.rotates_runner:
        print(
            "runner follow-up: refresh every enrolled runner after this rotation; "
            "cached runner Redis URLs keep the old password until they refetch."
        )


def main(argv: list[str] | None = None) -> int:
    if settings.data_plane.is_remote:
        print(
            "✗ this cluster's data plane is remote-managed — rotation is a "
            "local-instance operation (requirepass / ACL). A remote/SaaS plane "
            "rotates credentials at the provider; update AVA_REDIS_URL here.",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description="Rotate the Redis data-plane credentials.")
    parser.add_argument("--scope", choices=sorted(_SCOPES), default="both")
    parser.add_argument("--execute", action="store_true", help="perform the rotation")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--resume", metavar="STATE_FILE", help="resume a saved rotation")
    args = parser.parse_args(argv)
    try:
        _require_gateway_context()
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
        print("NEXT: restart every enrolled runner so it refetches the runner Redis URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
