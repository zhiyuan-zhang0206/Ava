"""ava-ops — agent-runner inbound ops server.

The agent-runner's inbound ops service — the ONLY long-running ava process on
an agent-runner that the gateway dials DIRECTLY over the private network
(the runner's other services — restarter, watchdog, browser, mcp-daemon — are
local or health-checked, not gateway-facing). Serves an inbound HTTP
endpoint the gateway dials to run a cluster op on this host. Each request executes **in-process** by calling free
functions in `ops/ops_*.py` and returns the result in the HTTP
response.

Usage:
    .venv/bin/python -m services.agent_ops.daemon

Per-machine singleton via pidfile. Kept alive every 60s by
`services/watchdog/daemon.py`.

Transport:
    Plain request/response. The gateway resolves this host's address from
    the `machines` table and POSTs to `/ops`. No queue, no SSE, no reconnect
    loop — the cluster's private network makes every node mutually dialable, so a
    control op is one synchronous round-trip.

    This daemon registers its own unit in that table once it is serving
    (`_register_boot`), so the address and the "up" record are written by the
    process that answers there, not only by the `ava start` that launched it.

Endpoint:
    POST /ops  {"kind": <work-kind>, "payload": {...}}
        -> 200 {"status": "completed"|"failed", "result": {...}}
    GET  /healthz  (watchdog liveness; served on the same port)

Normal work kinds and payloads are declared by `ops.rpc_schemas.OpEnvelope`;
the dispatch functions below route them to the corresponding `ops_*` operation.
The explicit prepared bootstrap entry bypasses this normal dispatcher entirely.

Idempotency keys: a request whose envelope carries `idempotency_key` is
deduplicated — the first dispatch with a key runs the op and stores its
outcome in the shared `api_idempotency` table (method='ops' rows — the same
table the gateway's HTTP idempotency middleware uses; migration
20260808T200000_unify-ops-idempotency merged the former
`cluster_ops_idempotency` into it); every later dispatch with the same key
replays the stored outcome instead of re-executing. This is what makes
the gateway's retry of non-idempotent ops (spawn / cluster_update /
lifecycle) safe: a lost response cannot duplicate the effect (Task #961).

A central DB pool is opened at startup and shared across all ops; each op
manages its own connection lifetime via `pool.connection()`.

Threading: `spawn` / `lifecycle` are awaited on the event loop; every other arm is
synchronous and runs on this daemon's own pool (`_dispatch_sync`). None may hold the
loop — see that function for the two-hour prod wedge that established it, and
`_hard_exit` for why the process still has to skip interpreter teardown.
"""

from __future__ import annotations

import sys

if __name__ == "__main__" and any(
    arg == "--bootstrap-observation" or arg.startswith("--bootstrap-observation=")
    for arg in sys.argv[1:]
):
    # This must precede ordinary imports: Settings may fetch a stopped gateway,
    # and normal daemon initialization writes PID/schema/registration state.
    from services.agent_ops.bootstrap import main as bootstrap_main

    raise SystemExit(bootstrap_main())


import asyncio
import contextlib
import functools
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from ops import ops_cluster, ops_config, ops_inventory, ops_lifecycle, ops_uploads
from ops.cluster_status import ShellNotFoundError
from ops.rpc_schemas import (
    AgentSkillViewPayload,
    ClusterSpawnSession,
    ClusterTransitionPayload,
    ClusterUpdatePayload,
    ConfigWritePayload,
    InventoryWritePayload,
    LaunchAgentRequest,
    LifecyclePayload,
    OpEnvelope,
    ShellCapturePayload,
    ShellKillPayload,
    ShellProbePayload,
    UploadReceivePayload,
)
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.agent_ops import health
from services.agent_ops import maintenance as maintenance_activity
from services.agent_ops._boot import (
    _open_db_pool,
    _ops_auth_token,
    _ops_bind_host,
    _register_boot,
)
from shared.agents import AvaAgentError
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.db_transaction import write_transaction
from shared.log import init_gateway_process
from shared.machine import machine_name
from shared.paths import legacy_pid_path
from shared.transport_encryption import verify_transport_encryption

_log = logging.getLogger("services.agent_ops.daemon")

_PIDFILE = settings.services.ops_pidfile

# ── Idempotency-key dedup (Task #961) ────────────────────────────────────────
# A request with `idempotency_key` is deduplicated against the shared
# `api_idempotency` table, method='ops' rows (see `_dispatch_idempotent`): the
# first dispatch with a key owns it, runs the op, and stores the outcome; later
# dispatches with the same key replay the stored outcome. The gateway's retry
# loop (ops/cluster_rpc.py) re-sends the SAME key on every attempt of one
# logical op, so a lost response after a successful run cannot duplicate the
# effect (spawn -> twin agent).
# Rows are kept 7 days — matching the HTTP idempotency channel's retention
# (one shared table, one policy; the gateway middleware prunes on its claims
# too) — orders of magnitude past the longest retry window — and pruned
# opportunistically on each new-key insert.
_DEDUP_TTL_S = 7 * 86_400.0
# A same-key dispatch arriving while the owner is still executing is a caller
# bug (the gateway's attempts are sequential); wait briefly for the owner's
# stored outcome, then fail loud instead of re-executing.
_DEDUP_WAIT_STEP_S = 0.1
_DEDUP_WAIT_ATTEMPTS = 30  # ~3s cap
# A cluster update may spend 30s in validate-before-kill fetch, then pause and
# spawn through winproc. 180s leaves margin while staying far below
# NO_PROGRESS_TIMEOUT_S (900s); the 2026-08-12 wedged-spawn shape is still
# loud after this bound rather than being mistaken for legitimate progress.
_DEDUP_EXPECTED_DURATION_S: dict[str, float] = {"cluster_update": 180.0}
# Bounded retry for a connection that dies mid-transaction (Task #1059):
# `check_connections` (#1027) only guards the checkout; a conn that breaks
# between checkout and commit still crashed the pass. The idempotency key
# makes a re-run safe — if the claim committed, the retry replays/waits
# instead of re-executing; if it did not, the retry re-claims and executes.
_DISPATCH_RETRY_ATTEMPTS = 3
_DISPATCH_RETRY_BACKOFF_S = 0.5
_sleep = asyncio.sleep

# Bounded concurrent dispatches — a burst of inbound /ops POSTs (e.g. a large
# spawn fan-out) runs at most this many op calls in parallel; further requests
# queue on the semaphore so one fan-out cannot overwhelm the agent-runner.
_dispatch_sem: asyncio.Semaphore | None = None

# Shared DB pool for all in-process ops calls. Opened in `_main`, closed in the
# matching finally. None outside the daemon's lifetime — tests that bypass
# `_main` must set this explicitly.
_db_pool: ConnectionPool | None = None


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.agent_ops.daemon"):
        _log.info("[agent_ops] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.agent_ops.daemon") or pidfile_holds_daemon(
        legacy_pid_path("agent_ops"), "services.agent_ops.daemon"
    )


# The one cluster op that spawns an orchestration session, serialized against
# itself. The event loop used to give that serialization for free (neither POST
# yielded); in worker threads with `ops_concurrency`=8 it has to be stated.
#
# **Refused, not queued** — and **only this op**. Refused, because a caller waiting
# behind a stuck update learns nothing while it is stuck, and
# `ClusterUpdateInProgress` is a verdict its callers already handle; this closes the
# check-then-spawn window `spawn_update`'s own session check leaves open. Only this
# op, because the compensating `cluster_resume` and the pause/stop path must stay
# able to land while an update is in flight — a wider lock would rebuild, inside the
# fix, the property this exists to remove.
_cluster_update_lock = asyncio.Lock()

# When the current holder took `_cluster_update_lock`, so a refusal can say how long
# it has been refusing behind. Event-loop thread only, so it needs no lock.
_cluster_update_held_since: float | None = None

# Health handler and `_run_arm` share the loop; restart drops state and idempotency makes retry safe.
_active_ops: dict[str, tuple[str, float]] = {}

# The read-modify-write arms, serialized against each other. Same lost serialization
# as `cluster_update`, different failure: `config_write` and `inventory_write` both
# READ their on-disk state, modify it and write it back, so an interleave lands the
# later writer's snapshot over the earlier one's fields with no error anywhere — in
# the only on-disk copy of a cluster's secrets.
#
# Blocking, not refused: millisecond writes with no session to collide over, so
# waiting one out is cheap where refusing would make a caller re-send a config edit
# for nothing. `threading.Lock`, not `asyncio.Lock` — the contention is between
# worker THREADS, which an asyncio lock cannot see, and taking one on the loop would
# put the serialization back where this change removed it.
#
# Same-process only; the cross-process race is closed by `runtime_config`'s own lock.
_state_write_lock = threading.Lock()

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


async def _run_arm(kind: str, payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
    """`_dispatch_sync` on this daemon's own pool (see `_op_executor`).

    An arm must not read contextvars: `run_in_executor` does not propagate context,
    so anything set per-request on the loop side (a request id, a trace span) reads
    as its default in the worker. Pass what an arm needs through its `payload`.
    """
    loop = asyncio.get_running_loop()
    active = (kind, time.monotonic())
    _active_ops[kind] = active
    try:
        future = loop.run_in_executor(
            _op_thread_pool(), functools.partial(_dispatch_sync, kind, payload)
        )
        maintenance_activity.track_worker(future)
        return await asyncio.shield(future)
    finally:
        if _active_ops.get(kind) == active:
            _active_ops.pop(kind)


def _set_update_held_since(value: float | None) -> None:
    """Record when the in-flight `cluster_update` took the lock (None = free)."""
    global _cluster_update_held_since  # noqa: PLW0603 — event-loop thread only
    _cluster_update_held_since = value


def _dispatch_sync(kind: str, payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
    """The blocking half of `_dispatch`, run in a worker thread.

    **Every arm here blocks, and none of them may block the event loop.** On
    2026-08-12 a `cluster_update` on the Windows runner stopped returning mid-spawn;
    called inline from the async dispatch, it took the whole daemon with it — 2 h
    03 m with not one line logged, every controller stopped, and the stranded-pause
    self-heal unable to run. Which syscall hung was never identified and does not
    matter: a synchronous op holding the loop is a defect on its own.

    **Nothing stayed behind**: no arm here is purely in-memory, and a stalled
    filesystem is the same defect as a stalled fetch. The two genuinely async ops
    (`spawn`, `lifecycle`) stay in `_dispatch`, awaited as before.

    Exceptions propagate to `_dispatch`'s handlers unchanged — `to_thread`
    re-raises in the awaiting coroutine — so the wire-error mapping is untouched.

    **The half-dead shape this leaves, and how to recognise it.** A worker thread
    that wedges the way 08-12 did no longer takes the daemon down, but it does not
    come back either: it holds `_cluster_update_lock` for as long as it is stuck, so
    every later `cluster_update` on this host is REFUSED while `cluster_resume`, the
    controllers, the health endpoint and every other op keep working normally. The
    host therefore looks healthy and simply will not update. The tell is a run of
    `refusing a concurrent cluster_update` warnings in this daemon's log with a
    hold time that only grows. The response is to look for the stuck thread and
    bounce the daemon — restarting it releases the lock, and nothing else will.
    """
    match kind:
        case "cluster_stop":
            transition = ClusterTransitionPayload.model_validate(payload)
            return "completed", ops_cluster.cluster_stop_op(
                transition.deploy_holder,
                transition.deploy_acquired_at,
            )
        case "cluster_update":
            # restart_only=True is the agent-runner leg of a cluster *restart*
            # (bounce services on current code, no checkout / uv sync);
            # target_sha is the rollout's pinned commit this host force-checks-out
            # (absent -> catch up to origin/main on a self-heal); mode is the
            # agent-drain policy ('none' on the rollout's Phase B — the
            # gateway-side quiesce already drained the fleet); force_reap is
            # the quiesce-timeout backstop that kills still-live agents.
            cu = ClusterUpdatePayload.model_validate(payload)
            session = ClusterSpawnSession.model_validate(
                ops_cluster.cluster_update_op(
                    restart_only=cu.restart_only,
                    target_sha=cu.target_sha,
                    mode=cu.mode,
                    force_reap=cu.force_reap,
                )
            )
            return "completed", session.model_dump(mode="json")
        case "cluster_fetch":
            return "completed", ops_cluster.cluster_fetch_op()
        case "cluster_resume":
            if not payload:
                return "completed", ops_cluster.cluster_resume_legacy_op()
            transition = ClusterTransitionPayload.model_validate(payload)
            return "completed", ops_cluster.cluster_resume_op(
                transition.deploy_holder,
                transition.deploy_acquired_at,
            )
        case "status_probe":
            return "completed", ops_cluster.cluster_status_op(_db_pool).model_dump(mode="json")
        case "config_read":
            return "completed", ops_config.config_read_op().model_dump(mode="json")
        case "config_write":
            cw = ConfigWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", ops_config.config_write_op(
                    cw.overrides, local=cw.local
                ).model_dump(mode="json")
        case "inventory_read":
            return "completed", ops_inventory.inventory_read_op().model_dump(mode="json")
        case "inventory_write":
            iw = InventoryWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", ops_inventory.inventory_write_op(
                    iw.plugins, iw.mcp_servers
                ).model_dump(mode="json")
        case "shell_probe":
            sp = ShellProbePayload.model_validate(payload)
            return "completed", ops_cluster.shell_probe_op(sp.agent_id).model_dump(mode="json")
        case "shell_kill":
            sk = ShellKillPayload.model_validate(payload)
            return "completed", ops_cluster.shell_kill_op(sk.agent_id, sk.session_id).model_dump(
                mode="json"
            )
        case "agent_skill_view":
            asv = AgentSkillViewPayload.model_validate(payload)
            return "completed", ops_cluster.agent_skill_view_op(asv.agent_id, _db_pool).model_dump(
                mode="json"
            )
        case "shell_capture":
            sc = ShellCapturePayload.model_validate(payload)
            return "completed", ops_cluster.shell_capture_op(
                sc.agent_id, sc.session_id, sc.lines
            ).model_dump(mode="json")
        case "upload_receive":
            ur = UploadReceivePayload.model_validate(payload)
            return "completed", ops_uploads.upload_receive_op(ur).model_dump(mode="json")
        case _:
            return "failed", {"error": f"unknown kind: {kind!r}"}


async def _dispatch(kind: str, payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
    """Execute one op in-process by calling `ops/ops_*.py`.

    `kind` ranges over `ops.rpc_schemas.OpKind` (the canonical op vocabulary);
    this `match` must stay exhaustive over it, and an unrecognized kind falls
    through the `case _` to a 'failed' result rather than crashing the ops
    server. Each arm validates its payload into the per-kind request model and
    serializes the per-kind result model — the wire contract lives in the models
    (`ops/rpc_schemas.py`), not in hand-written isinstance guards here.

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
    (ValueError), or a capture for a session that no longer exists
    (ShellNotFoundError) becomes a plain 'failed' result.
    """
    pool = _db_pool
    if pool is None:
        return "failed", {"error": "_db_pool not initialized; _main must run before _dispatch"}

    try:
        match kind:
            case "spawn-launch":
                spawned = await ops_lifecycle.launch_agent_op(
                    LaunchAgentRequest.model_validate(payload), pool
                )
                return "completed", spawned.model_dump(mode="json")
            case "lifecycle":
                lc = LifecyclePayload.model_validate(payload)
                resp = await ops_lifecycle.lifecycle_op(
                    lc.path,
                    lc.body,
                    pool,
                    trigger_inbound_id=lc.trigger_inbound_id,
                    trigger_inbound_kind=lc.trigger_inbound_kind,
                )
                return "completed", resp.model_dump(mode="json")
            case "cluster_update":
                # Serialized against itself, and refused rather than queued (see
                # `_cluster_update_lock`). Everything else about the op is in
                # `_dispatch_sync`, which this runs off the loop like the rest.
                if _cluster_update_lock.locked():
                    held_s = (
                        0.0
                        if _cluster_update_held_since is None
                        else time.monotonic() - _cluster_update_held_since
                    )
                    # Logged, not merely answered: a RUN of these is the tell for the
                    # half-dead shape (`_dispatch_sync`), and only this daemon's log
                    # shows the run — each refused caller sees one failure and moves on.
                    _log.warning(
                        "refusing a concurrent cluster_update; the one holding this host "
                        "has been running for %.0fs",
                        held_s,
                    )
                    # No `reason` key, deliberately. That field is the wire-error enum
                    # the gateway maps back to an AvaAgentError subclass; this is a
                    # dispatch-level verdict, not one of those, and inventing a `reason`
                    # would make the gateway reconstruct an exception type that does not
                    # describe it.
                    return "failed", {
                        "error": (
                            "ClusterUpdateInProgress: a cluster_update is already "
                            "executing on this host; refusing a second one rather "
                            "than queueing behind it"
                        ),
                        "detail": f"concurrent cluster_update refused after {held_s:.0f}s",
                    }
                async with _cluster_update_lock:
                    _set_update_held_since(time.monotonic())
                    try:
                        return await _run_arm(kind, payload)
                    finally:
                        _set_update_held_since(None)
            case _:
                return await _run_arm(kind, payload)
    except AvaAgentError as exc:
        # Carry both detail and the wire `reason` enum value so the
        # gateway's `_raise_proxied_wire_error_from_payload` can
        # reconstruct the same AvaAgentError subclass (`EXCEPTION_BY_REASON[reason]`).
        return "failed", {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": str(exc),
            "reason": exc.reason.value,
        }
    except (ValidationError, ValueError) as exc:
        # ValidationError: a payload that failed its per-kind model_validate.
        # ValueError: ops_lifecycle.lifecycle_op raises it for an unparseable path.
        return "failed", {"error": f"{type(exc).__name__}: {exc}"}
    except ShellNotFoundError as exc:
        # A capture for a shell session that no longer exists (capture_shell's
        # business miss) is a normal 'failed' result the gateway turns into its
        # 404 — not a dispatch crash for _ops_route's catch-all to log.
        return "failed", {"error": f"{type(exc).__name__}: {exc}"}


async def _dispatch_idempotent(
    kind: str, payload: dict[str, Any], key: str, pool: ConnectionPool | None
) -> tuple[str, dict[str, object]]:
    """Dispatch one op with a dedup key, retrying a pass that dies on a
    closed DB connection.

    The body is `_dispatch_idempotent_pass`; a `psycopg.OperationalError`
    (a connection that died between checkout and commit) re-runs the whole
    pass up to `_DISPATCH_RETRY_ATTEMPTS` times. Re-running is safe: the
    dedup claim is atomic, so a retry either re-claims (original claim never
    committed) or observes the existing row and replays/waits for its outcome.
    Any other exception propagates unchanged.
    """
    if pool is None:
        return "failed", {
            "error": "_db_pool not initialized; _main must run before _dispatch_idempotent"
        }
    for attempt in range(_DISPATCH_RETRY_ATTEMPTS):
        try:
            return await _dispatch_idempotent_pass(kind, payload, key, pool)
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
    kind: str, payload: dict[str, Any], key: str, pool: ConnectionPool
) -> tuple[str, dict[str, object]]:
    """Execute one op, deduplicated by `key` — the retry-safe path for
    non-idempotent ops (spawn / cluster_update / lifecycle).

    The first dispatch with a given key runs the op and stores its
    (status, result) outcome in the shared `api_idempotency` table (method =
    'ops' rows — the same table the gateway's HTTP idempotency middleware
    uses); every later dispatch with the same key replays the stored outcome
    instead of re-executing. The owner is decided atomically
    (`INSERT ... ON CONFLICT DO NOTHING`), so two racing dispatches with the
    same key cannot both execute. A same-key dispatch that arrives while the
    owner is still executing waits for the owner's outcome, bounded by the
    operation kind's expected duration where one is known, then fails loud
    rather than re-executing.

    An unexpected crash inside the op deletes the row and re-raises: no outcome
    was stored, so a future same-key dispatch must re-execute, not replay or
    hang.

    Returns the same (status, result) contract as `_dispatch`.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        # Opportunistic TTL prune: one indexed-range delete per new key keeps
        # the shared table bounded (ops are rare; rows are small).
        cur.execute(
            "DELETE FROM api_idempotency WHERE completed_at < now() - make_interval(secs => %s)",
            (_DEDUP_TTL_S,),
        )
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body) "
            "VALUES (%s, 'ops', %s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, kind, json.dumps(payload, default=str)),
        )
        owned = cur.fetchone() is not None
    if owned:
        try:
            status, result = await _dispatch(kind, payload)
        except Exception:
            # No outcome was stored — a future same-key dispatch must be able to
            # re-execute rather than replay a half-done op or wait forever.
            with write_transaction(pool) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM api_idempotency WHERE key = %s AND method = 'ops'", (key,))
            raise
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_idempotency SET op_status = %s, response_body = %s, "
                "completed_at = now() WHERE key = %s AND method = 'ops'",
                (status, json.dumps(result, default=str), key),
            )
        return status, result
    # Another dispatch owns the key (and may still be executing): wait for its
    # stored outcome, then replay it. Most kinds retain the historical ~3s
    # waiter cap; known slow kinds use the owner's DB creation time as the
    # absolute deadline, so retries do not misdiagnose ordinary work as stuck.
    expected_duration_s = _DEDUP_EXPECTED_DURATION_S.get(kind)
    fallback_waits_left = _DEDUP_WAIT_ATTEMPTS
    while True:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT op_status, response_body, created_at FROM api_idempotency "
                "WHERE key = %s AND method = 'ops'",
                (key,),
            )
            row = cur.fetchone()
        if row is not None and row[0] is not None:
            result: dict[str, object] = row[1] or {}
            return row[0], result
        if row is not None and expected_duration_s is not None:
            owner_created_at: datetime = row[2]
            now = datetime.now(UTC)
            deadline = owner_created_at + timedelta(seconds=expected_duration_s)
            if now >= deadline:
                elapsed_s = max((now - owner_created_at).total_seconds(), 0.0)
                return "failed", {
                    "error": (
                        f"idempotency key {key!r} is owned by a dispatch that has been running "
                        f"for {elapsed_s:.1f}s without completing (kind {kind!r}); the owner is "
                        f"likely stuck; expected bound is {expected_duration_s:.1f}s"
                    )
                }
        elif expected_duration_s is None or row is None:
            fallback_waits_left -= 1
        await _sleep(_DEDUP_WAIT_STEP_S)
        if fallback_waits_left == 0:
            break
    return "failed", {
        "error": f"idempotency key {key!r} is owned by another dispatch that never "
        "completed (concurrent duplicate dispatch of one logical op?)"
    }


async def _ops_route(body: bytes) -> tuple[int, bytes, str]:
    """POST /ops route handler — parse {kind, payload}, dispatch, return result.

    Always responds HTTP 200 with {"status", "result"} once the body parses; a
    'failed' status is a semantic outcome the gateway re-raises, not an
    HTTP error. A malformed body (not JSON, missing kind) returns 400.
    """
    sem = _dispatch_sem
    if sem is None:
        raise RuntimeError("_dispatch_sem not initialized; _main must run before serving /ops")
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

    async with sem:
        with maintenance_activity.admission():
            try:
                if envelope.idempotency_key is not None:
                    # Non-idempotent ops retried by the gateway carry a dedup key:
                    # first dispatch executes + stores, later same-key dispatches
                    # replay (see _dispatch_idempotent).
                    status, result = await _dispatch_idempotent(
                        envelope.kind, envelope.payload, envelope.idempotency_key, _db_pool
                    )
                else:
                    status, result = await _dispatch(envelope.kind, envelope.payload)
            except Exception as exc:
                _log.exception("dispatch crashed for kind=%s", envelope.kind)
                status, result = "failed", {"error": f"{type(exc).__name__}: {exc}"}
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
    if _is_running():
        _log.info("ava-ops pidfile %s indicates another instance is alive — exiting", _PIDFILE)
        sys.exit(1)
    _write_pidfile()

    global _dispatch_sem, _db_pool  # noqa: PLW0603 — set once at startup, cleared in finally for test reuse
    _dispatch_sem = asyncio.Semaphore(settings.services.ops_concurrency)

    our_machine = machine_name()

    # Schema-current assertion — the ops server dispatches in-process gateway ops
    # that assume specific table columns. If the central DB is ahead of this
    # checkout (the gateway ran `ava cluster update` while this host stayed behind),
    # abort before serving any op rather than producing wire-level errors mid-flight.
    from shared.migrations import assert_schema_current

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

    try:
        bind_host = _ops_bind_host()
        if bind_host != "127.0.0.1":
            verify_transport_encryption(settings.data_plane.cluster_secret, bind_host)
        # The gateway presents the cluster secret on every /ops dial when the
        # cluster has one (including a single box's loopback self-dial); a
        # no-secret cluster serves /ops unauthenticated on loopback.
        auth_token = _ops_auth_token()
        server = await start_health_server(
            "ops",
            host=bind_host,
            extra_routes={("POST", "/ops"): _ops_route},
            components=lambda: health.ops_components(_cluster_update_held_since, _active_ops),
            extra=lambda: {
                "maintenance": maintenance_activity.progress(),
                "saturation": health.saturation(
                    _active_ops, max(1, settings.services.ops_concurrency)
                ),
            },
            auth_token=auth_token,
        )
        _log.info(
            "ava-ops up, machine=%s serving POST /ops on %s:%d",
            our_machine,
            bind_host,
            health_port("ops"),
        )
        _register_boot()
        try:
            async with server:
                await server.serve_forever()
        finally:
            await stop_health_server(server)
            _remove_pidfile()
    finally:
        pool.close()
        _db_pool = None
        _dispatch_sem = None
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


def _hard_exit(code: int) -> int:
    """End the process now, skipping interpreter teardown. Never returns.

    Teardown is precisely what hangs: `asyncio.run`'s close awaits
    `shutdown_default_executor`, and the atexit handler joins every non-daemon worker
    unboundedly (see `_op_executor`). One wedged arm therefore holds a daemon that has
    already finished every piece of cleanup it owns — `_main`'s `finally` layers run
    first (health server, pidfile, DB pool), and what is skipped after them is
    bookkeeping for an interpreter about to stop existing.

    Logs are flushed first: they are the one thing a skipped teardown would lose, and
    this log is where the next stall has to be legible.
    """
    with contextlib.suppress(Exception):
        from loguru import logger as _loguru

        _loguru.remove()  # closes (and so flushes) every sink
    with contextlib.suppress(Exception):
        logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def main() -> None:
    init_gateway_process(name="ops")
    install_graceful_shutdown("ops")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that awaits
    # `shutdown_default_executor` — inside the very hang being escaped. Never closed.
    runner = asyncio.Runner()
    try:
        runner.run(_main())
    except KeyboardInterrupt:
        _log.info("[ops] interrupted, shutting down")
    except Exception:
        _log.exception("[ops] daemon crashed — uncaught exception escaped _main()")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
