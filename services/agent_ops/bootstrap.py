"""Read-only prepared-observation helpers retained from the retired updater.

The ops daemon has no bootstrap serving entry or restricted effect dispatcher,
and no production code imports this module; its remaining consumer is the
runtime-prepare CI proof (`scripts/prove_ops_bootstrap.py`). It provides no
runnable daemon or wire mutation ingress.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import psycopg
from pydantic import Field, SecretStr, field_validator

from shared.managed_writer_barrier import RolloutIdentity, lock_rollout
from shared.managed_writer_observation import (
    ExpectedUnitWriters,
    ObservationChallenge,
    UnitObserver,
)
from shared.native_process import pid_starttime_ticks
from shared.native_process.ownership import stable_create_time
from shared.process_evidence import Digest, EvidenceModel, ExpectedProcess
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release


class PreparedObservation(EvidenceModel):
    expected: ExpectedUnitWriters
    operation: RolloutIdentity
    challenge: ObservationChallenge
    schema_digest: Digest


class BootstrapRuntimeIdentity(EvidenceModel):
    """Actual restricted responder, never a normal-service readiness receipt."""

    process: ExpectedProcess
    module: str
    home: str
    artifact_digest: Digest
    manifest_digest: Digest


def runtime_identity(context: PreparedObservation) -> BootstrapRuntimeIdentity:
    process = psutil.Process()
    return BootstrapRuntimeIdentity(
        process=ExpectedProcess(
            pid=process.pid,
            create_time=stable_create_time(process),
            starttime=pid_starttime_ticks(process.pid),
        ),
        module=str(Path(__file__).resolve(strict=True)),
        home=context.expected.home,
        artifact_digest=context.expected.artifact_digest,
        manifest_digest=context.expected.manifest_digest,
    )


class ObserverProjection(EvidenceModel):
    """One already-resolved child cohort; never reads .env or fetches gateway."""

    db_url: SecretStr = Field(min_length=1)
    cluster_secret: SecretStr
    ops_port: int = Field(gt=0, le=65535)
    transport_encryption: str = ""

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
            transport_encryption=os.environ.get("AVA_TRANSPORT_ENCRYPTION", ""),
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
    # prepare_threshold=None: never prepare statements on the pooled front door.
    with (
        psycopg.connect(
            projection.db_url.get_secret_value(),
            prepare_threshold=None,
            connect_timeout=min(5, remaining),
        ) as conn,
        conn.transaction(),
    ):
        remaining_ms = int(
            (context.challenge.valid_until - datetime.now(UTC)).total_seconds() * 1000
        )
        if remaining_ms <= 0:
            raise ReleaseRejectedError("bootstrap challenge expired while connecting")
        conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(remaining_ms),))
        lock_rollout(conn, context.operation)
        row = conn.execute(
            "SELECT home FROM machine_units WHERE machine_name=%s AND home=%s",
            (context.expected.machine, context.expected.home),
        ).fetchone()
        if row != (context.expected.home,):
            raise ReleaseRejectedError("prepared observer unit is not registered")
        # A concurrent registry DDL can delay even the unit lookup. Do not use
        # the clock captured before that wait as evidence of current authority.
        at = lock_rollout(conn, context.operation)
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
            payload = json.loads(result[1])
            payload["runtime"] = runtime_identity(context).model_dump(mode="json")
            return result[0], json.dumps(payload).encode(), result[2]
        return result
    except (psycopg.Error, RuntimeError, ValueError):
        return (409, b'{"error":"bootstrap operation is unavailable or stale"}', "application/json")
