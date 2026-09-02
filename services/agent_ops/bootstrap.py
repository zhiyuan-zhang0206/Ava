"""Restricted same-service ops entry before normal Settings/schema/PID effects.

Only the updater's explicit prepared context and pre-projected child environment
are accepted. This observer never registers a unit, migrates, or serves /ops.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from pydantic import Field, SecretStr, field_validator

from shared.daemon_http import start_daemon_http
from shared.managed_writer_barrier import Digest, EvidenceModel, RolloutIdentity, lock_rollout
from shared.managed_writer_observation import (
    ExpectedUnitWriters,
    ObservationChallenge,
    UnitObserver,
)
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release


class PreparedObservation(EvidenceModel):
    expected: ExpectedUnitWriters
    operation: RolloutIdentity
    challenge: ObservationChallenge
    schema_digest: Digest


class ObserverProjection(EvidenceModel):
    """One already-resolved child cohort; never reads .env or fetches gateway."""

    db_url: SecretStr = Field(min_length=1)
    cluster_secret: SecretStr
    ops_port: int = Field(gt=0, le=65535)

    @field_validator("db_url")
    @classmethod
    def explicit_db_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("bootstrap requires an explicit database projection")
        return value

    @classmethod
    def from_environment(cls) -> ObserverProjection:
        # Existing Settings aliases; the normal updater resolves this cohort
        # before gateway shutdown. Missing projection is an error, not a fetch.
        return cls(
            db_url=SecretStr(os.environ["AVA_DB_URL"]),
            cluster_secret=SecretStr(os.environ["AVA_CLUSTER_SECRET"]),
            ops_port=int(os.environ["AVA_OPS_HEALTH_PORT"]),
        )


def read_prepared_context(path: Path) -> PreparedObservation:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 64 * 1024
        or info.st_uid != os.getuid()
    ):
        raise ReleaseRejectedError("bootstrap context must be an owned private regular file")
    return PreparedObservation.model_validate_json(path.read_bytes())


def validate_operation(context: PreparedObservation, projection: ObserverProjection) -> None:
    """Only old-schema columns; no config bootstrap, schema assertion or writes."""
    remaining = int((context.challenge.valid_until - datetime.now(UTC)).total_seconds())
    if remaining < 2:
        raise ReleaseRejectedError("bootstrap challenge has no connection budget remaining")
    with (
        psycopg.connect(
            projection.db_url.get_secret_value(), connect_timeout=min(5, remaining)
        ) as conn,
        conn.transaction(),
    ):
        remaining_ms = int(
            (context.challenge.valid_until - datetime.now(UTC)).total_seconds() * 1000
        )
        if remaining_ms <= 0:
            raise ReleaseRejectedError("bootstrap challenge expired while connecting")
        conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(remaining_ms),))
        at = lock_rollout(conn, context.operation)
        row = conn.execute(
            "SELECT home FROM machine_units WHERE machine_name=%s AND home=%s",
            (context.expected.machine, context.expected.home),
        ).fetchone()
        if row != (context.expected.home,):
            raise ReleaseRejectedError("prepared observer unit is not registered")
        if context.challenge.valid_until <= at:
            raise ReleaseRejectedError("prepared observer challenge is expired")


def validate_entry(context: PreparedObservation, projection: ObserverProjection) -> VerifiedRelease:
    if "shared.config" in sys.modules:
        raise ReleaseRejectedError("bootstrap observer imported ordinary Settings")
    home = Path(context.expected.home)
    if not home.is_absolute() or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("prepared observer home must be canonical and existing")
    if (home / "machine_name").read_text(encoding="utf-8").strip() != context.expected.machine:
        raise ReleaseRejectedError("prepared observer machine differs from this installed unit")
    release = verify_release(
        home / "releases",
        context.expected.artifact_digest,
        manifest_digest=context.expected.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=context.schema_digest,
    )
    # A real image must execute this entry, not a dev/source module that merely
    # points at somebody else's valid prepared manifest.
    if not Path(__file__).resolve().is_relative_to(release.root / "venv"):
        raise ReleaseRejectedError("observer code is not loaded from the prepared image")
    validate_operation(context, projection)
    return release


async def observe_response(
    context: PreparedObservation,
    projection: ObserverProjection,
    observer: UnitObserver,
    body: bytes,
) -> tuple[int, bytes, str]:
    try:
        await asyncio.to_thread(validate_operation, context, projection)
        result = await observer.respond(body)
        if result[0] == 200:
            # OS observation cannot hold a database lock. Revalidate afterward;
            # adoption still needs its own fresh, locked operation check.
            await asyncio.to_thread(validate_operation, context, projection)
        return result
    except (psycopg.Error, RuntimeError, ValueError):
        return (409, b'{"error":"bootstrap operation is unavailable or stale"}', "application/json")


async def serve(context: PreparedObservation, projection: ObserverProjection) -> None:
    await asyncio.to_thread(validate_entry, context, projection)
    observer = UnitObserver(context.expected, context.challenge)

    async def observe(body: bytes) -> tuple[int, bytes, str]:
        return await observe_response(context, projection, observer, body)

    secret = projection.cluster_secret.get_secret_value()
    server = await start_daemon_http(
        host="0.0.0.0" if secret else "127.0.0.1",  # noqa: S104 — existing authenticated ops bind policy
        port=projection.ops_port,
        auth_token=secret or None,
        health_response=lambda: (
            503,
            json.dumps({"mode": "bootstrap_observation", "full_ready": False}).encode(),
        ),
        extra_routes={("POST", "/ops/bootstrap-observation"): observe},
    )
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-observation", type=Path, required=True)
    args = parser.parse_args()
    try:
        if sys.platform == "win32":
            raise ReleaseRejectedError("bootstrap observation has no Windows preparation proof")
        context = read_prepared_context(args.bootstrap_observation)
        projection = ObserverProjection.from_environment()
        asyncio.run(serve(context, projection))
    except (OSError, ValueError, RuntimeError, KeyError, psycopg.Error) as exc:
        # Never expose credential-bearing connection diagnostics or environment.
        sys.stderr.write(f"bootstrap observation refused ({type(exc).__name__})\n")
        return 2
    return 0
