# meiva:note
# A committed request claim remains after effect failure, cancellation or result-write failure: pending means uncertain and never authorizes re-execution. Duplicate keys validate immutable request identity and wait only for a bounded interval. Only OperationalError is eligible for connection retry. Running workers remain in maintenance accounting after their HTTP waiter is cancelled; repeated stop signals cannot interrupt bounded draining, and default-executor joining cannot prevent process exit. A modeled failed operation remains an HTTP 200 response so callers distinguish its result from transport failure; unexpected result values are string-serialized rather than breaking the control-plane response.
from __future__ import annotations

import argparse
import sys

if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    from base.log import prepare_process_logging

    prepare_process_logging()


import asyncio
import functools
import hashlib
import json
import logging
import re
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base import paths
from base.agents.context.clients import ClientSet
from base.agents.incarnation.native_restart_models import NativeRestartOperation
from base.api_contracts.agent_contract import MeivaAgentError, ResurrectRefused
from base.api_contracts.launch_retry import LaunchReconcileRequest
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.cluster.transport_encryption import verify_transport_encryption_declaration
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.db.transaction import write_transaction
from base.host.exit_codes import hard_exit as _hard_exit
from base.host.system.hostport import Host
from base.lm.catalog import ModelCatalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from ops import lifecycle
from ops.cluster_status import ShellNotFoundError
from ops.rpc_schemas import LaunchAgentRequest, LifecyclePayload, OpEnvelope, OpStatus, is_op_kind
from services.agent_runner.agent_ops import boot, health
from services.agent_runner.agent_ops import maintenance as maintenance_activity
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync
from services.agent_runner.agent_ops.to_user_resource import resource_route
from services.agent_runner.agent_ops.to_user_watch import watch_to_user

_log = logging.getLogger("services.agent_runner.agent_ops.daemon")


DEDUP_WAIT_STEP_S = 0.1
DEDUP_WAIT_ATTEMPTS = 30


DISPATCH_RETRY_ATTEMPTS = 3
_DISPATCH_RETRY_BACKOFF_S = 0.5
_sleep = asyncio.sleep


ActiveOps = dict[str, tuple[str, float]]


async def run_arm(
    kind: str,
    payload: dict[str, Any],
    *,
    active_ops: ActiveOps,
    workers: maintenance_activity.WorkerFutures,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    loop = asyncio.get_running_loop()
    active = (kind, time.monotonic())
    active_ops[kind] = active
    try:
        future = loop.run_in_executor(
            executor,
            functools.partial(
                bind_dispatch_sync,
                kind,
                payload,
                pool=pool,
                authority=authority,
                database=database,
                image=image,
                local_machine=local_machine,
                host=host,
            ),
        )
        maintenance_activity.track_worker(future, workers=workers)
        return await asyncio.shield(future)
    finally:
        if active_ops.get(kind) == active:
            active_ops.pop(kind)


def bind_dispatch_sync(
    kind: str,
    payload: dict[str, Any],
    *,
    pool: ConnectionPool,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    return dispatch_sync(
        kind,
        payload,
        pool=pool,
        db=boot.ops_handles(database=database, authority=authority)[0],
        authority=authority,
        image=image,
        local_machine=local_machine,
        host=host,
    )


async def _dispatch(
    kind: str,
    payload: dict[str, Any],
    *,
    active_ops: ActiveOps,
    workers: maintenance_activity.WorkerFutures,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}

    try:
        match kind:
            case "spawn-launch":
                db, bus = boot.ops_handles(database=database, authority=authority)
                spawned = await lifecycle.launch_agent_op(
                    db,
                    bus,
                    LaunchAgentRequest.model_validate(payload),
                    pool,
                    catalog=catalog,
                    local_machine=local_machine.name(),
                )

                return OpStatus.COMPLETED, spawned.model_dump(mode="json", exclude_none=True)
            case "launch-reconcile":
                from ops.lifecycle.launch_reconcile import reconcile_launch_op

                db, bus = boot.ops_handles(database=database, authority=authority)
                result = await reconcile_launch_op(
                    db,
                    bus,
                    LaunchReconcileRequest.model_validate(payload),
                    pool,
                    local_machine=local_machine.name(),
                )
                return OpStatus.COMPLETED, result.model_dump(mode="json")
            case "lifecycle":
                lc = LifecyclePayload.model_validate(payload)
                db, bus = boot.ops_handles(database=database, authority=authority)
                resp = await lifecycle.lifecycle_op(
                    db,
                    bus,
                    lc.path,
                    lc.body,
                    pool,
                    trigger_inbound_id=lc.trigger_inbound_id,
                    trigger_inbound_kind=lc.trigger_inbound_kind,
                    catalog=catalog,
                    local_machine=local_machine.name(),
                )
                return OpStatus.COMPLETED, resp.model_dump(mode="json")
            case _:
                return await run_arm(
                    kind,
                    payload,
                    active_ops=active_ops,
                    workers=workers,
                    pool=pool,
                    executor=executor,
                    authority=authority,
                    database=database,
                    image=image,
                    local_machine=local_machine,
                    host=host,
                )
    except MeivaAgentError as exc:
        return OpStatus.FAILED, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": str(exc),
            "reason": exc.reason.value,
        }
    except (ValidationError, ValueError, InvalidConfigOverlay) as exc:
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    except (ShellNotFoundError, ResurrectRefused) as exc:
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}


async def dispatch_idempotent(
    kind: str,
    payload: dict[str, Any],
    key: str,
    pool: ConnectionPool,
    *,
    active_ops: ActiveOps,
    workers: maintenance_activity.WorkerFutures,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}
    for attempt in range(DISPATCH_RETRY_ATTEMPTS):
        try:
            return await dispatch_idempotent_pass(
                kind,
                payload,
                key,
                pool,
                active_ops=active_ops,
                workers=workers,
                executor=executor,
                catalog=catalog,
                authority=authority,
                database=database,
                image=image,
                local_machine=local_machine,
                host=host,
            )
        except psycopg.OperationalError:
            if attempt + 1 >= DISPATCH_RETRY_ATTEMPTS:
                raise
            _log.warning(
                "dispatch idempotent pass died with OperationalError (attempt %d/%d); retrying",
                attempt + 1,
                DISPATCH_RETRY_ATTEMPTS,
            )
            await _sleep(_DISPATCH_RETRY_BACKOFF_S)
    raise AssertionError("unreachable")


async def dispatch_idempotent_pass(
    kind: str,
    payload: dict[str, Any],
    key: str,
    pool: ConnectionPool,
    *,
    active_ops: ActiveOps,
    workers: maintenance_activity.WorkerFutures,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    if (
        kind == "lifecycle"
        and isinstance(payload.get("path"), str)
        and re.fullmatch(r"/api/agents/[1-9][0-9]*/restart-work", payload["path"])
    ):
        try:
            operation = NativeRestartOperation.model_validate(payload.get("body"))
        except ValidationError:
            return OpStatus.FAILED, {"error": "invalid guarded restart envelope"}
        if operation.operation_key != key:
            return OpStatus.FAILED, {"error": "guarded restart envelope identity differs"}
        return await _dispatch(
            kind,
            payload,
            active_ops=active_ops,
            workers=workers,
            pool=pool,
            executor=executor,
            catalog=catalog,
            authority=authority,
            database=database,
            image=image,
            local_machine=local_machine,
            host=host,
        )
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
        status, result = await _dispatch(
            kind,
            payload,
            active_ops=active_ops,
            workers=workers,
            pool=pool,
            executor=executor,
            catalog=catalog,
            authority=authority,
            database=database,
            image=image,
            local_machine=local_machine,
            host=host,
        )
        status = OpStatus(status)
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_idempotency SET op_status = %s, response_body = %s, "
                "completed_at = now() WHERE key = %s AND method = 'ops'",
                (status, json.dumps(result, default=str), key),
            )
        return status, result

    for _ in range(DEDUP_WAIT_ATTEMPTS):
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
        await _sleep(DEDUP_WAIT_STEP_S)
    return OpStatus.FAILED, {
        "error": f"idempotency key {key!r} is owned by another dispatch that never "
        "completed; outcome uncertain, inspect the stored operation before recovery"
    }


async def ops_route(
    body: bytes,
    *,
    active_ops: ActiveOps,
    workers: maintenance_activity.WorkerFutures,
    dispatch_sem: asyncio.Semaphore,
    requests: maintenance_activity.RequestTokens,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[int, bytes, str]:

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
            with maintenance_activity.admission(envelope.kind, requests=requests):
                if envelope.idempotency_key is not None:
                    status, result = await dispatch_idempotent(
                        envelope.kind,
                        envelope.payload,
                        envelope.idempotency_key,
                        pool,
                        active_ops=active_ops,
                        workers=workers,
                        executor=executor,
                        catalog=catalog,
                        authority=authority,
                        database=database,
                        image=image,
                        local_machine=local_machine,
                        host=host,
                    )
                else:
                    status, result = await _dispatch(
                        envelope.kind,
                        envelope.payload,
                        active_ops=active_ops,
                        workers=workers,
                        pool=pool,
                        executor=executor,
                        catalog=catalog,
                        authority=authority,
                        database=database,
                        image=image,
                        local_machine=local_machine,
                        host=host,
                    )
        except Exception as exc:
            _log.exception("dispatch refused or crashed for kind=%s", envelope.kind)
            status, result = OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    status = OpStatus(status)

    return (
        200,
        json.dumps({"status": status, "result": result}, default=str).encode(),
        "application/json",
    )


async def serve_ops_daemon(
    *,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    config: ConfigBoot,
    host: Host,
) -> None:
    runtime = config.ensure_eager()
    catalog = build_model_catalog()
    authority = ConfigAuthority(
        runtime=runtime,
        all_domains=runtime if runtime.profile is None else config.complete_read_model(),
        env_path=paths.meiva_home() / ".env",
    )

    active_ops: ActiveOps = {}
    requests: maintenance_activity.RequestTokens = set()
    workers: maintenance_activity.WorkerFutures = set()
    runtime_dir = paths.run_dir(paths.meiva_home())
    if boot.is_running(config, runtime=runtime_dir):
        _log.info(
            "meiva-ops pidfile %s indicates another instance is alive — exiting",
            boot.pidfile(config, runtime=runtime_dir),
        )
        sys.exit(1)
    boot.write_pidfile(config, runtime=runtime_dir)

    dispatch_sem = asyncio.Semaphore(config.view.services.ops_concurrency)

    our_machine = local_machine.name()

    from base.deploy.schema.migrations import assert_schema_current

    try:
        assert_schema_current(config.view.data_plane.db_url)
    except Exception as exc:
        _log.error(
            "schema version mismatch on ops-server startup: %s — run `git pull && uv sync && meiva start` to catch up",
            exc,
        )
        sys.exit(1)

    pool = boot.open_db_pool(database=database, authority=authority)
    executor = boot.op_thread_pool(authority=authority)
    liveness = LivenessGroup()

    try:
        acceptance = boot.ops_acceptance()
        bind_host = boot.ops_bind_host(machine=local_machine)
        if bind_host != "127.0.0.1":
            verify_transport_encryption_declaration(
                bind_host, config.view.data_plane.transport_encryption, authenticated=True
            )
        endpoint = boot.endpoint(config, runtime=runtime_dir)
        server = await start_health_server(
            "ops",
            endpoint.health_port,
            host=bind_host,
            extra_routes={
                ("POST", "/ops"): functools.partial(
                    ops_route,
                    database=database,
                    image=image,
                    executor=executor,
                    catalog=catalog,
                    authority=authority,
                    active_ops=active_ops,
                    dispatch_sem=dispatch_sem,
                    requests=requests,
                    workers=workers,
                    pool=pool,
                    local_machine=local_machine,
                    host=host,
                ),
                ("POST", "/to-user/resource"): functools.partial(
                    resource_route,
                    pool=pool,
                    machine=our_machine,
                    executor=executor,
                    dispatch_sem=dispatch_sem,
                    requests=requests,
                    workers=workers,
                ),
            },
            liveness=liveness,
            components=lambda: health.ops_components(active_ops),
            extra=lambda: {
                "maintenance": maintenance_activity.progress(requests=requests, workers=workers),
                "saturation": health.saturation(
                    active_ops, max(1, config.view.services.ops_concurrency)
                ),
            },
            auth_digests=acceptance,
            image=image,
        )
        _log.info(
            "meiva-ops up, machine=%s serving POST /ops on %s:%d",
            our_machine,
            bind_host,
            endpoint.health_port,
        )
        boot.register_boot(
            database=database,
            config=config,
            machine=local_machine,
            ops_port=endpoint.health_port,
            home=paths.meiva_home(),
        )
        _, bus = boot.ops_handles(database=database, authority=authority)
        try:
            async with server, asyncio.TaskGroup() as background:
                watcher = background.create_task(watch_to_user(paths.workspaces_root(), bus))
                try:
                    await server.serve_forever()
                finally:
                    watcher.cancel()
        finally:
            await bus.async_redis().aclose()
            await stop_health_server(server)
            boot.remove_pidfile(config, runtime=runtime_dir)
    finally:
        pool.close()
        boot.shutdown_op_pool(executor)


def main(*, argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    image = LoadedCommit.capture()
    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    machine = machine_from_boot(config)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="ops", exempt=False)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(
            database=database, machine=machine, home=paths.meiva_home().resolve()
        ),
    )
    code = 0
    primary: BaseException | None = None

    try:
        init_gateway_process(
            name="ops", producer=clients.event_pipeline, machine_reader=machine.name, image=image
        )
        install_graceful_shutdown("ops")
        runner = asyncio.Runner()
        try:
            runner.run(
                serve_ops_daemon(
                    database=database,
                    image=image,
                    local_machine=machine,
                    config=config,
                    host=Host.system(),
                )
            )
        except KeyboardInterrupt as interrupted:
            primary = interrupted

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _log.info("[ops] interrupted, shutting down")

            failures = cancel_and_drain(runner, interrupted_by=interrupted)
            if failures:
                _log.error("[ops] async shutdown failed: %r", failures)
                code = 1
        except Exception as exc:
            primary = exc
            _log.exception("[ops] daemon crashed — uncaught exception escaped serve_ops_daemon()")
            code = 1
    finally:
        failure = primary if primary is not None else sys.exception()
        try:
            clients.close(pipeline_timeout=2)
        except Exception as exc:
            if failure is not None:
                failure.add_note(f"ops event pipeline shutdown failed: {exc!r}")
            _log.exception("[ops] event pipeline shutdown failed")
            code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main(argv=[])
