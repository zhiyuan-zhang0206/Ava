"""ava-ops — agent-runner inbound ops server.

The ONLY long-running ava process on an agent-runner the gateway dials
DIRECTLY (the runner's other services — agent-host, browser,
mcp-daemon — are local or health-checked). Serves POST /ops; each request
executes in-process against the `ops` op clusters and returns {status, result}.

Usage: .venv/bin/python -m services.agent_runner.agent_ops.daemon — a per-machine
singleton via pidfile, supervised by the application root. Registers
its own unit in `machines` once serving (`_register_boot`).

Idempotency keys: an envelope carrying `idempotency_key` is deduplicated
against the shared `api_idempotency` table (method='ops' rows — migration
20260808T200000_unify-ops-idempotency): the first dispatch runs the op and
stores its outcome, later ones replay it, so the gateway's retry of
non-idempotent ops (spawn / lifecycle) cannot duplicate the
effect (Task #961).

A central DB pool is shared across all ops; each op manages its connection
lifetime via `pool.connection()`. Threading: `spawn` / `lifecycle` run on the
event loop; every other arm is synchronous on this daemon's own pool
(`_dispatch_sync`, the thin binding of `services.agent_runner.agent_ops.dispatch_sync`) —
none may hold the loop (see that module; `_hard_exit` skips interpreter
teardown).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    # Reject unknown argv before imports can load Settings or touch local state.
    argparse.ArgumentParser(description=__doc__).parse_args()


import asyncio
import functools
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents import AvaAgentError, ResurrectRefused
from base.cluster.machine import machine_name
from base.cluster.transport_encryption import verify_transport_encryption
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db.transaction import write_transaction
from base.log import init_gateway_process
from ops import host_config as host_config
from ops import inventory as inventory
from ops import lifecycle
from ops import uploads as uploads

# The synchronous op arms and the op modules they call live in
# `services.agent_runner.agent_ops.dispatch_sync` (split at the file-size ceiling, task
# #4129 I4). The op modules below are re-exported through the daemon because
# the routing tests patch them through this module's name
# (`daemon.cluster`); the arms reference the same module objects.
from ops.cluster import operations
from ops.cluster_status import ShellNotFoundError
from ops.rpc_schemas import (
    LaunchAgentRequest,
    LifecyclePayload,
    OpEnvelope,
    OpStatus,
    is_op_kind,
)
from services.agent_runner.agent_ops import health, outbox_flusher
from services.agent_runner.agent_ops import maintenance as maintenance_activity
from services.agent_runner.agent_ops._boot import (
    _open_db_pool,
    _ops_acceptance,
    _ops_bind_host,
    _ops_handles,
    _register_boot,
)
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

cluster = operations

_log = logging.getLogger("services.agent_runner.agent_ops.daemon")


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("ops")


def _pidfile() -> Path:
    return _endpoint().pidfile


# ── Ops request identity and response replay ────────────────────────────────
# Acceptance and execution are not one transaction. Keep claims across errors
# and retain all ops identities until domain recovery/expiry can prove it safe
# to retire them. A pending row is inspectable uncertainty, never permission to
# execute again. The immutable hash rejects key reuse for another operation.
# A same-key dispatch while the owner is still executing is a caller bug
# (gateway attempts are sequential); wait briefly for the stored outcome,
# then fail loud instead of re-executing.
_DEDUP_WAIT_STEP_S = 0.1
_DEDUP_WAIT_ATTEMPTS = 30  # ~3s cap
# Bounded retry for a connection that dies mid-transaction (Task #1059): the
# idempotency key makes a re-run safe — a committed claim replays/waits, an
# uncommitted one re-claims and executes.
_DISPATCH_RETRY_ATTEMPTS = 3
_DISPATCH_RETRY_BACKOFF_S = 0.5
_sleep = asyncio.sleep

# Shared DB pool for all in-process ops calls — opened in `_main`, closed in
# its finally; tests that bypass `_main` set this explicitly.
_db_pool: ConnectionPool | None = None


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.agent_runner.agent_ops.daemon"):
        _log.info("[agent_ops] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.agent_runner.agent_ops.daemon")


ActiveOps = dict[str, tuple[str, float]]

# The op arms' own thread pool, instead of asyncio's default executor.
#
# `asyncio.run` closes by awaiting `loop.shutdown_default_executor()`, which JOINS
# every default-executor thread. A wedged op arm — the 2026-08-12 shape — is a
# thread that never finishes, so SIGTERM unwinds normally, `_main`'s finally runs
# (health server stopped, pidfile removed, pool closed), and then the interpreter
# sits in that join. Python 3.12 bounds it at `asyncio.constants.THREAD_JOIN_TIMEOUT`
# (300 s) and then gives up with `shutdown(wait=False)`, so this is a five-minute
# stall rather than a permanent one — but the supervisor's graceful window is 15 s,
# so in practice a wedged daemon stops being something that exits and becomes
# something that gets force-killed.
#
# Owning the pool buys nameable threads and a capacity bound — NOT a free exit.
# `shutdown(wait=False)` does not release the daemon: `_python_exit` (registered by
# `threading._register_atexit`) joins every worker still in `_threads_queues` with no
# bound, `wait=False` does not take a running thread out of that mapping, and 3.9+
# forces those workers non-daemon. Measured: own pool + wedged worker +
# `shutdown(wait=False)` + `sys.exit(0)` is still alive minutes later. `_hard_exit`
# is what actually gets the process out.
#
# `max_workers` tracks `ops_concurrency` so the semaphore stays the binding limit;
# `thread_name_prefix` makes a stuck thread findable in a dump, which the refusal
# runbook above tells an operator to look for.
_op_executor: ThreadPoolExecutor | None = None


def _op_thread_pool() -> ThreadPoolExecutor:
    """This daemon's op executor, created on first use."""
    global _op_executor  # noqa: PLW0603 — event-loop thread only, created once
    if _op_executor is None:
        _op_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.services.ops_concurrency),
            thread_name_prefix="ava-ops-arm",
        )
    return _op_executor


async def _run_arm(
    kind: str, payload: dict[str, Any], *, active_ops: ActiveOps
) -> tuple[OpStatus, dict[str, object]]:
    """`_dispatch_sync` on this daemon's own pool (see `_op_executor`).

    An arm must not read contextvars: `run_in_executor` does not propagate context,
    so anything set per-request on the loop side (a request id, a trace span) reads
    as its default in the worker. Pass what an arm needs through its `payload`.
    """
    loop = asyncio.get_running_loop()
    active = (kind, time.monotonic())
    active_ops[kind] = active
    try:
        future = loop.run_in_executor(
            _op_thread_pool(), functools.partial(_dispatch_sync, kind, payload)
        )
        maintenance_activity.track_worker(future)
        return await asyncio.shield(future)
    finally:
        if active_ops.get(kind) == active:
            active_ops.pop(kind)


def _dispatch_sync(kind: str, payload: dict[str, Any]) -> tuple[OpStatus, dict[str, object]]:
    """The blocking op arms, bound to this daemon's shared pool.

    The arms live in `services.agent_runner.agent_ops.dispatch_sync` (split at the file-size
    ceiling, task #4129 I4). The binding stays here so `_run_arm` and the
    tests' patch surface (`ops_daemon._dispatch_sync`) keep working unchanged.
    """
    return dispatch_sync(kind, payload, pool=_db_pool, db=_ops_handles()[0])


async def _dispatch(
    kind: str, payload: dict[str, Any], *, active_ops: ActiveOps
) -> tuple[OpStatus, dict[str, object]]:
    """Execute one op in-process by calling the `ops` op clusters.

    `kind` ranges over `ops.rpc_schemas.OpKind` (the canonical op vocabulary);
    this `match` must stay exhaustive over it, and an unrecognized kind falls
    through the `case _` to a 'failed' result rather than crashing the ops
    server. Each arm validates its payload into the per-kind request model and
    serializes the per-kind result model — the wire contract lives in the models
    (`ops/rpc_schemas/__init__.py`), not in hand-written isinstance guards here.

    Returns (status, result) where status is 'completed' or 'failed' and result
    is a JSON-serializable dict (response body on success, error info on
    failure). The caller serializes this into the /ops HTTP response — every
    result goes through `.model_dump(mode="json")` so datetime / enum fields land
    as JSON-native types before json.dumps.

    Wire-protocol errors raised by ops functions (AvaAgentError subclasses such
    as AgentNotFound / ForkSourceEmpty / MachineNotRegistered) are converted to
    the OpFailure shape ({"error", "detail", "reason"}) so the gateway's handler
    can re-emit the original semantics via `_raise_proxied_wire_error_from_payload`.
    A malformed payload (ValidationError), an unparseable lifecycle path
    (ValueError), a capture for a session that no longer exists
    (ShellNotFoundError) or a resurrection refusal (ResurrectRefused) becomes a
    plain 'failed' result.
    """
    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}
    pool = _db_pool
    if pool is None:
        return OpStatus.FAILED, {
            "error": "_db_pool not initialized; _main must run before _dispatch"
        }

    try:
        match kind:
            case "spawn-launch" | "spawn-launch-v2":
                db, bus = _ops_handles()
                spawned = await lifecycle.launch_agent_op(
                    db, bus, LaunchAgentRequest.model_validate(payload), pool
                )
                # `exclude_none`: the settlement receipt is present only when a
                # withdrawn model was rewritten (task #4306) — the common wire
                # shape stays {"id": ...}.
                return OpStatus.COMPLETED, spawned.model_dump(mode="json", exclude_none=True)
            case "lifecycle":
                lc = LifecyclePayload.model_validate(payload)
                db, bus = _ops_handles()
                resp = await lifecycle.lifecycle_op(
                    db,
                    bus,
                    lc.path,
                    lc.body,
                    pool,
                    trigger_inbound_id=lc.trigger_inbound_id,
                    trigger_inbound_kind=lc.trigger_inbound_kind,
                )
                return OpStatus.COMPLETED, resp.model_dump(mode="json")
            case _:
                return await _run_arm(kind, payload, active_ops=active_ops)
    except AvaAgentError as exc:
        # Carry both detail and the wire `reason` enum value so the
        # gateway's `_raise_proxied_wire_error_from_payload` can
        # reconstruct the same AvaAgentError subclass (`EXCEPTION_BY_REASON[reason]`).
        return OpStatus.FAILED, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": str(exc),
            "reason": exc.reason.value,
        }
    except (ValidationError, ValueError) as exc:
        # ValidationError: a payload that failed its per-kind model_validate.
        # ValueError: lifecycle.lifecycle_op raises it for an unparseable path.
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    except (ShellNotFoundError, ResurrectRefused) as exc:
        # A capture for a shell session that no longer exists (capture_shell's
        # business miss) is a normal 'failed' result the gateway turns into its
        # 404; a resurrection refusal is a durable row verdict the caller
        # reports by reason. Neither is a dispatch crash for _ops_route's
        # catch-all to log.
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}


async def _dispatch_idempotent(
    kind: str,
    payload: dict[str, Any],
    key: str,
    pool: ConnectionPool | None,
    *,
    active_ops: ActiveOps,
) -> tuple[OpStatus, dict[str, object]]:
    """Dispatch one op with a dedup key, retrying a pass that dies on a
    closed DB connection.

    The body is `_dispatch_idempotent_pass`; a `psycopg.OperationalError`
    (a connection that died between checkout and commit) re-runs the whole
    pass up to `_DISPATCH_RETRY_ATTEMPTS` times. Re-running is safe: the
    dedup claim is atomic, so a retry either re-claims (original claim never
    committed) or observes the existing row and replays/waits for its outcome.
    Any other exception propagates unchanged.
    """
    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}
    if pool is None:
        return OpStatus.FAILED, {
            "error": "_db_pool not initialized; _main must run before _dispatch_idempotent"
        }
    for attempt in range(_DISPATCH_RETRY_ATTEMPTS):
        try:
            return await _dispatch_idempotent_pass(kind, payload, key, pool, active_ops=active_ops)
        except psycopg.OperationalError:
            if attempt + 1 >= _DISPATCH_RETRY_ATTEMPTS:
                raise
            _log.warning(
                "dispatch idempotent pass died with OperationalError (attempt %d/%d); retrying",
                attempt + 1,
                _DISPATCH_RETRY_ATTEMPTS,
            )
            await _sleep(_DISPATCH_RETRY_BACKOFF_S)
    raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises


async def _dispatch_idempotent_pass(
    kind: str, payload: dict[str, Any], key: str, pool: ConnectionPool, *, active_ops: ActiveOps
) -> tuple[OpStatus, dict[str, object]]:
    """Replay one immutable request; unresolved execution never frees its identity.

    A claim cannot prove an external effect happened, so a lost result or owner
    crash leaves the record pending. Duplicates wait briefly, then fail with an
    explicit uncertain outcome without dispatching. A domain receipt must own
    recovery; this response record is not an exactly-once execution framework.
    """
    request_hash = hashlib.sha256(
        json.dumps([kind, payload], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body, request_hash) "
            "VALUES (%s, 'ops', %s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, kind, json.dumps(payload), request_hash),
        )
        owned = cur.fetchone() is not None
    if owned:
        # Exceptions, cancellation and result-write failure retain the committed
        # claim. A retry cannot infer that execution had no side effects.
        status, result = await _dispatch(kind, payload, active_ops=active_ops)
        status = OpStatus(status)
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_idempotency SET op_status = %s, response_body = %s, "
                "completed_at = now() WHERE key = %s AND method = 'ops'",
                (status, json.dumps(result, default=str), key),
            )
        return status, result
    # A concurrent duplicate waits for the recorded outcome, never executes twice.
    for _ in range(_DEDUP_WAIT_ATTEMPTS):
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT method, path, request_hash, op_status, response_body FROM api_idempotency "
                "WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
        if row is not None:
            if row[0] != "ops" or row[1] != kind or row[2] != request_hash:
                return OpStatus.FAILED, {
                    "error": "idempotency request identity conflict or legacy identity unavailable; "
                    "the stored operation was not re-executed"
                }
            if row[3] is not None:
                result: dict[str, object] = row[4] or {}
                return OpStatus(row[3]), result
        await _sleep(_DEDUP_WAIT_STEP_S)
    return OpStatus.FAILED, {
        "error": f"idempotency key {key!r} is owned by another dispatch that never "
        "completed; outcome uncertain, inspect the stored operation before recovery"
    }


async def _ops_route(
    body: bytes, *, active_ops: ActiveOps, dispatch_sem: asyncio.Semaphore
) -> tuple[int, bytes, str]:
    """POST /ops route handler — parse {kind, payload}, dispatch, return result.

    Always responds HTTP 200 with {"status", "result"} once the body parses; a
    'failed' status is a semantic outcome the gateway re-raises, not an
    HTTP error. A malformed body (not JSON, missing kind) returns 400.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return 400, json.dumps({"error": f"invalid JSON body: {exc}"}).encode(), "application/json"
    try:
        envelope = OpEnvelope.model_validate(parsed)
    except ValidationError as exc:
        return (
            400,
            json.dumps({"error": f"body must be {{kind: str, payload: dict}}: {exc}"}).encode(),
            "application/json",
        )

    if not is_op_kind(envelope.kind):
        return (
            200,
            json.dumps(
                {"status": OpStatus.FAILED, "result": {"error": f"unknown kind: {envelope.kind!r}"}}
            ).encode(),
            "application/json",
        )

    async with dispatch_sem:
        try:
            with maintenance_activity.admission(envelope.kind):
                if envelope.idempotency_key is not None:
                    # Non-idempotent ops retried by the gateway carry a dedup key:
                    # first dispatch executes + stores, later same-key dispatches
                    # replay (see _dispatch_idempotent).
                    status, result = await _dispatch_idempotent(
                        envelope.kind,
                        envelope.payload,
                        envelope.idempotency_key,
                        _db_pool,
                        active_ops=active_ops,
                    )
                else:
                    status, result = await _dispatch(
                        envelope.kind, envelope.payload, active_ops=active_ops
                    )
        except Exception as exc:
            _log.exception("dispatch refused or crashed for kind=%s", envelope.kind)
            status, result = OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    status = OpStatus(status)
    # default=str is a last-resort fallback: op results should already be
    # JSON-native (Pydantic returns go through model_dump(mode="json")), but a
    # stray non-JSON value (datetime, Path, ...) in a hand-built dict must
    # degrade to its str() instead of raising here and 500-ing the control plane.
    return (
        200,
        json.dumps({"status": status, "result": result}, default=str).encode(),
        "application/json",
    )


async def _main() -> None:
    # Request arms and health snapshots share only this daemon invocation's state.
    active_ops: ActiveOps = {}
    if _is_running():
        _log.info("ava-ops pidfile %s indicates another instance is alive — exiting", _pidfile())
        sys.exit(1)
    _write_pidfile()

    global _db_pool  # noqa: PLW0603 — set once at startup, cleared in finally for test reuse
    dispatch_sem = asyncio.Semaphore(settings.services.ops_concurrency)

    our_machine = machine_name()

    # Schema-current assertion: if the central DB is ahead of this checkout,
    # abort before serving any op that assumes its columns.
    from base.deploy.schema.migrations import assert_schema_current

    try:
        assert_schema_current(settings.data_plane.db_url)
    except Exception as exc:
        _log.error(
            "schema version mismatch on ops-server startup: %s — run `git pull && uv sync && ava start` to catch up",
            exc,
        )
        sys.exit(1)

    pool = _open_db_pool()
    _db_pool = pool
    db, bus = _ops_handles()
    # Redeliver recorded delivery failures whenever the data plane allows
    # (task #3757): a resident loop that outlives every sender process, owned with
    # the server by the TaskGroup below.
    liveness = LivenessGroup()
    outbox_progress = liveness.register(
        "delivery-outbox", outbox_flusher.INITIAL_LIVENESS_TIMEOUT_S
    )

    try:
        # Every /ops dial presents a machine API token of this generation; an
        # open cluster serves /ops unauthenticated on loopback.
        acceptance = _ops_acceptance()
        bind_host = _ops_bind_host(acceptance)
        if bind_host != "127.0.0.1":
            verify_transport_encryption(bind_host, authenticated=acceptance is not None)
        endpoint = _endpoint()
        server = await start_health_server(
            "ops",
            endpoint.health_port,
            host=bind_host,
            extra_routes={
                ("POST", "/ops"): functools.partial(
                    _ops_route, active_ops=active_ops, dispatch_sem=dispatch_sem
                )
            },
            liveness=liveness,
            components=lambda: health.ops_components(active_ops),
            extra=lambda: {
                "maintenance": maintenance_activity.progress(),
                "saturation": health.saturation(
                    active_ops, max(1, settings.services.ops_concurrency)
                ),
            },
            auth_digests=acceptance,
        )
        _log.info(
            "ava-ops up, machine=%s serving POST /ops on %s:%d",
            our_machine,
            bind_host,
            endpoint.health_port,
        )
        _register_boot()
        try:
            # One TaskGroup owns the server and the outbox loop: a loop that raises
            # cancels the server and ends the process, and the supervisor restarts it.
            async with server, asyncio.TaskGroup() as resident:
                resident.create_task(server.serve_forever())
                resident.create_task(outbox_flusher.outbox_loop(pool, db, bus, outbox_progress))
        finally:
            await stop_health_server(server)
            _remove_pidfile()
    finally:
        pool.close()
        _db_pool = None
        _shutdown_op_pool()


def _shutdown_op_pool() -> None:
    """Drop the op pool without joining it.

    `wait=False` stops THIS call from blocking; it does not stop the interpreter's
    own atexit join (see `_op_executor`), which is why `_hard_exit` exists. Both are
    needed: without this, shutdown waits here; without that, it waits at teardown.
    """
    global _op_executor  # noqa: PLW0603 — event-loop thread only
    if _op_executor is not None:
        _op_executor.shutdown(wait=False)
        _op_executor = None


def main(*, argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    # Task #3621: ops is on the full-validation whitelist — build the eager
    # config chain at the entry, before serving.
    from base.config import ensure_eager

    ensure_eager()
    init_gateway_process(name="ops")
    install_graceful_shutdown("ops")
    code = 0
    # Drain async cleanup explicitly; Runner.close also joins the default
    # executor, which may contain a stuck worker and must not delay this exit.
    runner = asyncio.Runner()
    try:
        runner.run(_main())
    except KeyboardInterrupt:
        _log.info("[ops] interrupted, shutting down")
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[ops] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[ops] daemon crashed — uncaught exception escaped _main()")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main(argv=[])
