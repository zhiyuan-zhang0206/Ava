"""Labeler daemon — standalone label auto-generation process.

Polls `agents` rows whose label is unset (NULL or empty string — the two
are the same "not set" state) and not user-owned every second, takes the
first request-bearing inbound as the prompt — a chat message, or a
task-tagged system note (task assignment briefs arrive as system notes
since the 2026-08-27 task-notification change) — and calls
`generate_label_async` to generate a short name. Fully decoupled from
the Gateway; can be deployed independently.

Usage:
    .venv/bin/python -m services.derived.labeler.daemon

Kept alive by the root supervisor's health monitor through the roster's `/healthz`
identity probe (`ops/roster/healthz.py`).
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings
from base.config.domains.lm import LmSettings
from base.config.profiles import PROCESS_PROFILES, profile_unknown_error
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.host.exit_codes import hard_exit as _hard_exit
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import ava_home
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.derived.labeler.config import LabelerConfig
from services.derived.labeler.labeler import generate_label_async
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.derived.labeler.daemon")

_POLL_INTERVAL_S = 1.0
# Liveness staleness ceiling — generous because one iteration may make up
# to 10 LLM label calls; beating per-item keeps a slow-but-legit call from
# tripping it, while a genuine wedge still flips /healthz 503 -> respawn.
_LIVENESS_TIMEOUT_S = 120.0


def labeler_config() -> LabelerConfig:
    """The composition root: the one place this package reads `settings`."""
    return LabelerConfig(
        labeler_model=settings.lm.labeler_model,
        labeler_max_chars=settings.services.labeler_max_chars,
    )


def labeler_model_overrides(*, profile: str | None, lm: LmSettings) -> ModelOverrides:
    """Freeze this caller's tuning at the composition boundary.

    Gateway boot removes agent-only tuning aliases, so this caller resolves
    through model defaults. Do not read those stripped fields. A full or agent-side boot retains its explicit tuning as before.
    """
    if profile is not None and profile not in PROCESS_PROFILES:
        raise profile_unknown_error(profile)
    if profile == "gateway":
        return ModelOverrides.from_pins({})
    return ModelOverrides.from_pins(
        {
            "reasoning_effort": lm.reasoning_effort,
            "claude_thinking_budget_tokens": lm.claude_thinking_budget_tokens,
        }
    )


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("labeler")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


# The inbound condition a label prompt may come from: a chat peer message,
# or a system note carrying a task notification (note_tag='task'). Task
# assignment briefs — the delegator's request a fresh worker exists for —
# have arrived as system notes since 2026-08-27 (Task #1838); a worker whose
# only inbound is its assignment would otherwise never get a labelable
# prompt. Heartbeat / impersonation / lifecycle notes carry other or no
# note_tag and stay excluded.
_PROMPT_INBOUND_CONDITION = (
    "(im.kind = 'chat' OR (im.kind = 'system_note' AND im.payload->>'note_tag' = 'task'))"
)


def _select_unlabeled(cur: psycopg.Cursor, attempted: list[int]) -> list[tuple[int, str | None]]:
    """Poll up to 10 newest agents that still need a label, returning each
    `(agent_id, first_eligible_prompt)`.

    A label is "missing" when it is NULL or the empty string — both are the
    same unset state, and an empty string must not wedge the row out of the
    poll (a stray '' is treated as NULL and overwritten by the labeler's
    equally broadened CAS).

    ORDER BY t.id DESC prioritizes new agents — prevents old agents
    (spawn-without-prompt or with inbounds already cleaned) from clogging the
    head of the LIMIT window and starving the poll. EXISTS filters out threads
    that will never have a prompt (saves poll iterations). `attempted` ids (agents already attempted by this process) are excluded in SQL — not after the
    LIMIT — so a cluster of persistently-failing agents neither burns an LLM call
    nor occupies the window ahead of a fresh agent.

    The batch of 10 is an internal scheduling quantity, not a user-facing
    surface, so it stays a literal rather than joining cluster config
    (task #3696 exception inventory).
    """
    cur.execute(
        "SELECT t.id, "  # noqa: S608 — _PROMPT_INBOUND_CONDITION is a module constant, never user input
        "(SELECT im.content FROM inbound_messages im "
        f" WHERE im.agent_id = t.id AND {_PROMPT_INBOUND_CONDITION} "
        " ORDER BY im.id LIMIT 1) AS prompt "
        "FROM agents t "
        "WHERE (t.label IS NULL OR t.label = '') "
        "AND NOT t.label_user_set "
        "AND NOT (t.id = ANY(%s::bigint[])) "
        "AND EXISTS ("
        "  SELECT 1 FROM inbound_messages im "
        f"  WHERE im.agent_id = t.id AND {_PROMPT_INBOUND_CONDITION}"
        ") "
        "ORDER BY t.id DESC "
        "LIMIT 10",
        (attempted,),
    )
    return cur.fetchall()


def _write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(_pidfile(config, runtime=runtime), "services.derived.labeler.daemon"):
        _log.info(
            "[labeler] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def _remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    remove_pidfile(_pidfile(config, runtime=runtime))


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _pidfile(config, runtime=runtime), "services.derived.labeler.daemon"
    )


async def _dispatch_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    liveness: Liveness,
    config: LabelerConfig,
    *,
    catalog: ModelCatalog,
    llm_override: str,
    overrides: ModelOverrides,
) -> None:
    """Main loop: every second, poll the newest unlabeled agents
    (`_select_unlabeled`, excluding previous attempts) -> grab first prompt ->
    generate label. Each eligible row is attempted once per process; failure leaves its label unset.

    False generation results are logged without scheduling another attempt. Escaping exceptions,
    including DB failures, reach run/main's existing service failure owner and
    stop this batch. No task is claimed or acknowledged here. A service restart may attempt an unset label once again; no retry state is persisted.

    generate_label_async writes via internal CAS (WHERE label is unset —
    NULL or empty string — AND NOT label_user_set), so user-edited labels
    are auto-skipped.
    """
    _log.info("[labeler] daemon started, pid=%s", os.getpid())
    attempted: set[int] = set()
    while True:
        liveness.beat()
        await asyncio.sleep(_POLL_INTERVAL_S)
        if admission.quiesced():
            continue
        with pool.connection() as conn, conn.cursor() as cur:
            rows = _select_unlabeled(cur, list(attempted))
        for tid, prompt in rows:
            liveness.beat()  # per-item: a slow LLM call must not look like a wedge
            if not prompt:
                continue
            attempted.add(tid)
            result = await generate_label_async(
                tid,
                prompt,
                config,
                db,
                bus,
                catalog=catalog,
                llm_override=llm_override,
                overrides=overrides,
            )
            if result is False:
                _log.warning(
                    "[labeler] label generation failed for thread %s; leaving label unset", tid
                )


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
) -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> enter main loop."""
    if _is_running(process_config, runtime=runtime):
        _log.info(
            "[labeler] daemon already running (pidfile=%s), exiting",
            _pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile(process_config, runtime=runtime)
    _log.info("[labeler] pidfile written: %s", _pidfile(process_config, runtime=runtime))

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint(process_config, runtime=runtime)
    health = await start_health_server(
        "labeler", endpoint.health_port, liveness=liveness, image=image
    )
    _log.info("[labeler] healthz listening on :%s", endpoint.health_port)

    db = database()
    pool = db.pool()
    try:
        await _dispatch_loop(
            pool,
            db,
            EventBus.from_settings(),
            liveness,
            labeler_config(),
            catalog=build_model_catalog(),
            llm_override=settings.lm.llm_override,
            overrides=labeler_model_overrides(profile=settings.profile, lm=settings.lm),
        )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile(process_config, runtime=runtime)
        _log.info("[labeler] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = machine_from_boot(machine_config)
    process_home = ava_home().resolve()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(machine_config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="labeler")

    def database() -> Database:
        return Database(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="labeler",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    install_graceful_shutdown("labeler")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    # The runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                process_config=machine_config,
                runtime=process_home / "run",
                database=database,
                image=image,
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[labeler] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still closes the DB pool, stops the
        # health server and removes the pidfile. The executor is deliberately
        # NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[labeler] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[labeler] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile(machine_config, runtime=process_home / "run")
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
