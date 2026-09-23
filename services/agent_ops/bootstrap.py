"""Restricted same-service ops entry before normal Settings/schema/PID effects.

Only the updater's explicit prepared context and pre-projected child environment
are accepted. This observer never registers a unit or migrates; it serves its
prepared observation route plus a strict allowlist of effect deliveries
(`cluster_bootstrap_hop` / `cluster_normal_continue`), each executed by a
one-shot child process (`services/agent_ops/dispatch_child.py`) so this
interpreter stays free of ordinary Settings and of the ops stack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psutil
import psycopg
from pydantic import Field, SecretStr, ValidationError, field_validator

from shared.api_contracts.op_envelope import OpEnvelope
from shared.daemon_http import start_daemon_http
from shared.hop_ledger import build_ledger_payload
from shared.managed_writer_barrier import Digest, EvidenceModel, RolloutIdentity, lock_rollout
from shared.managed_writer_observation import (
    ChallengeRequest,
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
    UnitObserver,
)
from shared.proc_tree import stable_create_time
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release
from shared.session_record import pid_starttime_ticks
from shared.transport_encryption import verify_transport_encryption_declaration


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


async def ledger_response(context: PreparedObservation, body: bytes) -> tuple[int, bytes, str]:
    """Challenge-gated read of the hop ledger; a damaged slot stays a 200 read."""
    try:
        request = ChallengeRequest.model_validate_json(body)
    except ValidationError:
        return 400, b'{"error":"invalid challenge request"}', "application/json"
    if (
        request.challenge != context.challenge.challenge
        or datetime.now(UTC) >= context.challenge.valid_until
    ):
        return 409, b'{"error":"unknown or expired challenge"}', "application/json"
    payload = await asyncio.to_thread(
        build_ledger_payload, Path(context.expected.home), context.challenge.challenge
    )
    # The read may block on the OS; expiry applies after collection too.
    if datetime.now(UTC) >= context.challenge.valid_until:
        return 409, b'{"error":"challenge expired during ledger read"}', "application/json"
    return 200, json.dumps(payload).encode(), "application/json"


# The restricted observer's admitted /ops kinds: the coordinator's two effect
# deliveries to a unit inside its restricted window -- channel C's bootstrap hop
# and channel E's continuation. Everything else stays fail-closed: this observer
# serves one prepared observation, it is not a second daemon.
_ADMITTED_OPS = frozenset({"cluster_bootstrap_hop", "cluster_normal_continue"})

# One admitted op's child work is one session spawn (seconds). This bound is
# the observer's orphan-reclaim limit, not a dial's patience: a coordinator dial
# carries its own default timeout (30s) and retries with the same idempotency
# key, so a child still running past that is answered by its own claim races --
# the sender reads a refusal and the checked recovery re-verifies. 120s just
# caps how long a wedged child may linger before this observer reaps it.
_DISPATCH_CHILD_TIMEOUT_S = 120.0


def run_dispatch_child(envelope: OpEnvelope, home: Path) -> tuple[str, dict[str, object]]:
    """Execute one admitted op through the full dispatch stack, in a child process.

    This observer must not import ordinary Settings or the ops stack (its
    startup refused an interpreter that imported `shared.config`, and the ops
    stack pulls it transitively), so the dispatch runs in a short-lived child of
    this same image: `services/agent_ops/dispatch_child.py`. A child that
    cannot answer degrades to the same failed-envelope shape the daemon returns
    for a crashed dispatch.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "services.agent_ops.dispatch_child"],
            input=envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            cwd=str(home),
            env=dict(os.environ),
            capture_output=True,
            timeout=_DISPATCH_CHILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "failed", {
            "error": f"restricted dispatch child exceeded {_DISPATCH_CHILD_TIMEOUT_S:.0f}s"
        }
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", "replace")[-2000:]
        return "failed", {
            "error": f"restricted dispatch child exited {completed.returncode}: {tail}"
        }
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
        status = str(answer["status"])
        result = answer["result"]
    except (ValueError, KeyError, TypeError):
        tail = completed.stdout.decode("utf-8", "replace")[-2000:]
        return "failed", {"error": f"restricted dispatch child returned no envelope: {tail!r}"}
    if status not in {"completed", "failed"} or not isinstance(result, dict):
        return "failed", {"error": "restricted dispatch child returned a malformed envelope"}
    return status, cast("dict[str, object]", result)


def ops_route(home: Path) -> Callable[[bytes], Awaitable[tuple[int, bytes, str]]]:
    """The restricted observer's `/ops` handler: validate, allowlist, relay.

    Wire shapes mirror the daemon's `_ops_route` exactly (400 on a bad JSON body
    or envelope; otherwise 200 with `{"status", "result"}`). A kind outside the
    allowlist is answered as a failed op and never reaches a child.

    No concurrency semaphore here, deliberately: deliveries are one effect per
    kind per restricted window, the `to_thread` hop is bounded by the default
    executor's worker pool, and duplicate deliveries are settled inside the
    child by its own `api_idempotency` claim or refused by the op's live-session
    guard.
    """

    async def handle(body: bytes) -> tuple[int, bytes, str]:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            return (
                400,
                json.dumps({"error": f"invalid JSON body: {exc}"}).encode(),
                "application/json",
            )
        try:
            envelope = OpEnvelope.model_validate(parsed)
        except ValidationError as exc:
            return (
                400,
                json.dumps({"error": f"body must be {{kind: str, payload: dict}}: {exc}"}).encode(),
                "application/json",
            )
        if envelope.kind not in _ADMITTED_OPS:
            return (
                200,
                json.dumps(
                    {
                        "status": "failed",
                        "result": {
                            "error": (
                                f"kind {envelope.kind!r} is not admitted by the restricted observer"
                            )
                        },
                    }
                ).encode(),
                "application/json",
            )
        status, result = await asyncio.to_thread(run_dispatch_child, envelope, home)
        return (
            200,
            json.dumps({"status": status, "result": result}, default=str).encode(),
            "application/json",
        )

    return handle


async def serve(context: PreparedObservation, projection: ObserverProjection) -> None:
    await asyncio.to_thread(validate_entry, context, projection)
    observer = UnitObserver(context.expected, context.challenge)

    async def observe(body: bytes) -> tuple[int, bytes, str]:
        return await observe_response(context, projection, observer, body)

    async def ledger(body: bytes) -> tuple[int, bytes, str]:
        return await ledger_response(context, body)

    secret = projection.cluster_secret.get_secret_value()
    bind_host = "0.0.0.0" if secret else "127.0.0.1"  # noqa: S104 — guarded below
    verify_transport_encryption_declaration(
        secret,
        bind_host,
        projection.transport_encryption,
    )
    server = await start_daemon_http(
        host=bind_host,
        port=projection.ops_port,
        auth_token=secret or None,
        health_response=lambda: (
            503,
            json.dumps({"mode": "bootstrap_observation", "full_ready": False}).encode(),
        ),
        extra_routes={
            ("POST", "/ops/bootstrap-observation"): observe,
            ("POST", "/ops/bootstrap-hop-ledger"): ledger,
            ("POST", "/ops"): ops_route(Path(context.expected.home)),
        },
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
