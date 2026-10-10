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
import time
from collections.abc import Callable
from pathlib import Path

import psycopg
from loguru import logger
from psycopg_pool import ConnectionPool

from base.cluster.machine import validate_machine_name
from base.config import settings
from base.config.domains.lm import LmSettings
from base.config.profiles import PROCESS_PROFILES, profile_unknown_error
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from base.log import init_gateway_process
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import build_pipeline
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

    Gateway boot removes agent-only tuning aliases, so the old model factory
    resolved this caller through model defaults. Do not read those stripped
    fields. A full or agent-side boot retains its explicit tuning as before.
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


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("labeler")


def _pidfile() -> Path:
    return _endpoint().pidfile


# Per-agent failure backoff. A trusted transient provider failure or an empty /
# rejected label leaves `label` NULL, so the next poll
# re-selects the same agent and retries — without a bound this is an unbounded
# hot loop of build_chat_model + LLM round-trips (~1/s). Each failure pushes the
# agent's next eligible retry out exponentially (capped), and cooling agents are
# excluded from the poll SELECT so they neither burn an LLM call nor occupy the
# LIMIT window ahead of a fresh agent. State is in-memory (per daemon process): a
# restart clears it and retries once — correct for a transient failure, one extra
# attempt for a permanent one.
_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 300.0
# Give-up threshold. Backoff bounds the RATE of retries but not their NUMBER: at
# the 300s cap a permanently-unlabelable agent costs ~12 LLM calls an hour, for
# the life of the process. The validity check in labeler.py enlarges the
# population that can fail permanently (a model that answers the brief instead of
# summarizing it is rejected on every draw, not just an unlucky one), so the
# change that enlarges it carries the bound. After this many consecutive
# failures the agent is RETIRED: permanently excluded from the poll SELECT, its
# label left NULL — already the honest resting state for an agent whose prompt
# cannot be summarized, and where ~50 prod agents sit today.
#
# 12 is ~28 minutes of retrying (2+4+8+...+256, then four waits at the cap), so
# an ordinary provider outage is still ridden out rather than retired through.
# Per-process like the rest of the backoff state: a daemon restart clears it and every
# retired agent gets one more chance.
_GIVE_UP_AFTER_FAILURES = 12


class _Backoff:
    """The per-agent failure state of one dispatch loop: `agent_id -> (consecutive_failures,
    monotonic deadline before next retry)`."""

    def __init__(self) -> None:
        self._entries: dict[int, tuple[int, float]] = {}

    def is_retired(self, tid: int) -> bool:
        """Whether an agent has failed enough consecutive times to be given up on."""
        return self._entries.get(tid, (0, 0.0))[0] >= _GIVE_UP_AFTER_FAILURES

    def cooling_ids(self, now: float) -> list[int]:
        """agent ids to keep out of the poll SELECT at `now` (a `time.monotonic`
        reading): those still inside their backoff window, plus those retired
        outright. Opportunistically drops entries whose retry was due more than one
        full cap-window ago: an expired entry that is still label-eligible would have
        been re-selected and cleared/re-failed by now, so a long-stale one means the
        agent was labeled out of band (or removed) and its backoff state can go.

        A retired entry is deliberately never pruned — pruning it would readmit the
        agent to the SELECT and restart the whole attempt cycle, which is the
        unbounded loop this is here to stop. The retained entries are bounded by the
        number of permanently-unlabelable agents seen in one process lifetime."""
        cooling: list[int] = []
        stale: list[int] = []
        for tid, (fails, deadline) in self._entries.items():
            if fails >= _GIVE_UP_AFTER_FAILURES or now < deadline:
                cooling.append(tid)
            elif deadline < now - _BACKOFF_CAP_S:
                stale.append(tid)
        for tid in stale:
            del self._entries[tid]
        return cooling

    def record_failure(self, tid: int, now: float) -> float:
        """Bump an agent's consecutive-failure count and push its next retry out
        exponentially (2s, 4s, 8s, ... capped at _BACKOFF_CAP_S). Returns the delay
        applied, for logging — meaningless once the agent is retired, which the
        caller checks with `is_retired`."""
        fails = self._entries.get(tid, (0, 0.0))[0] + 1
        delay = min(_BACKOFF_BASE_S * 2 ** (fails - 1), _BACKOFF_CAP_S)
        self._entries[tid] = (fails, now + delay)
        if fails == _GIVE_UP_AFTER_FAILURES:
            # Terminal for this process — emitted once, on the crossing, so the
            # event counts agents given up on rather than retry attempts.
            logger.error(
                "label generation retired for agent {agent_id} after {failures} consecutive failures",
                event="label_generate_retired",
                agent_id=tid,
                failures=fails,
            )
        return delay

    def retry_note(self, tid: int, delay: float) -> str:
        """The retry half of a failure log line — the promise must match reality, so
        a retired agent says so rather than naming a retry that will never come."""
        if self.is_retired(tid):
            return f"retired after {_GIVE_UP_AFTER_FAILURES} consecutive failures, label stays NULL"
        return f"backoff: next retry in >={delay:.0f}s"

    def clear(self, tid: int) -> None:
        """Drop an agent's backoff state after it labels successfully."""
        self._entries.pop(tid, None)


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


def _select_unlabeled(cur: psycopg.Cursor, cooling: list[int]) -> list[tuple[int, str | None]]:
    """Poll up to 10 newest agents that still need a label, returning each
    `(agent_id, first_eligible_prompt)`.

    A label is "missing" when it is NULL or the empty string — both are the
    same unset state, and an empty string must not wedge the row out of the
    poll (a stray '' is treated as NULL and overwritten by the labeler's
    equally broadened CAS).

    ORDER BY t.id DESC prioritizes new agents — prevents old agents
    (spawn-without-prompt or with inbounds already cleaned) from clogging the
    head of the LIMIT window and starving the poll. EXISTS filters out threads
    that will never have a prompt (saves poll iterations). `cooling` ids (agents
    inside their failure-backoff window) are excluded in SQL — not after the
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
        (cooling,),
    )
    return cur.fetchall()


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.derived.labeler.daemon"):
        _log.info("[labeler] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.derived.labeler.daemon")


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
    (`_select_unlabeled`, minus those in failure-backoff) -> grab first prompt ->
    generate label. A label that fails enters per-agent exponential backoff so a
    persistent failure does not become a hot retry loop.

    Only explicit False generation results enter backoff. Escaping exceptions,
    including DB failures, reach run/main's existing service failure owner and
    stop this batch. No task is claimed or acknowledged here. Restart clears
    the in-memory cooldown and retirement state; it does not isolate a bad row.

    generate_label_async writes via internal CAS (WHERE label is unset —
    NULL or empty string — AND NOT label_user_set), so user-edited labels
    are auto-skipped.
    """
    _log.info("[labeler] daemon started, pid=%s", os.getpid())
    backoff = _Backoff()
    while True:
        liveness.beat()
        await asyncio.sleep(_POLL_INTERVAL_S)
        if admission.quiesced():
            continue
        now = time.monotonic()
        cooling = backoff.cooling_ids(now)
        with pool.connection() as conn, conn.cursor() as cur:
            rows = _select_unlabeled(cur, cooling)
        for tid, prompt in rows:
            liveness.beat()  # per-item: a slow LLM call must not look like a wedge
            if not prompt:
                continue
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
                delay = backoff.record_failure(tid, now)
                _log.error(
                    "[labeler] generate label for thread %s failed (%s)",
                    tid,
                    backoff.retry_note(tid, delay),
                )
            else:
                backoff.clear(tid)


async def run(*, database: Callable[[], Database], image: LoadedCommit) -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> enter main loop."""
    if _is_running():
        _log.info("[labeler] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile()
    _log.info("[labeler] pidfile written: %s", _pidfile())

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint()
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
        _remove_pidfile()
        _log.info("[labeler] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="labeler")

    def database() -> Database:
        return Database.from_settings(gate=gate)

    pipeline = build_pipeline(database=database)
    init_gateway_process(
        name="labeler",
        producer=lambda: pipeline,
        machine_reader=lambda: validate_machine_name(settings.general.machine_name),
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
        runner.run(run(database=database, image=image))
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
        _remove_pidfile()
    _hard_exit(code)


if __name__ == "__main__":
    main()
