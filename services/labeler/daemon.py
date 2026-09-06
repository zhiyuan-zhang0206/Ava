"""Labeler daemon — standalone label auto-generation process.

Polls `agents` rows where `label IS NULL AND NOT label_user_set` every
second, takes the first chat inbound as the prompt, and calls
`generate_label_async` to generate a short name. Fully decoupled from
the Gateway; can be deployed independently.

Usage:
    .venv/bin/python -m services.labeler.daemon

Kept alive by the gateway watchdog's 60s healthcheck (`services/healthchecks/labeler.py`).
"""

import asyncio
import logging
import os
import sys
import time

import psycopg
from loguru import logger
from psycopg_pool import ConnectionPool

import shared.db
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.labeler.labeler import generate_label_async
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.labeler.daemon")

_POLL_INTERVAL_S = 1.0
# Liveness staleness ceiling — generous because one iteration may make up
# to 10 LLM label calls; beating per-item keeps a slow-but-legit call from
# tripping it, while a genuine wedge still flips /healthz 503 -> respawn.
_LIVENESS_TIMEOUT_S = 120.0
_PIDFILE = settings.services.labeler_pidfile

# Per-agent failure backoff. A label that persistently fails (bad key, rate
# limit, oversized prompt, model error) leaves `label` NULL, so the next poll
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
# Per-process like the rest of _BACKOFF: a daemon restart clears it and every
# retired agent gets one more chance.
_GIVE_UP_AFTER_FAILURES = 12
# agent_id -> (consecutive_failures, monotonic deadline before next retry)
_BACKOFF: dict[int, tuple[int, float]] = {}


def _is_retired(tid: int) -> bool:
    """Whether an agent has failed enough consecutive times to be given up on."""
    return _BACKOFF.get(tid, (0, 0.0))[0] >= _GIVE_UP_AFTER_FAILURES


def _cooling_ids(now: float) -> list[int]:
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
    for tid, (fails, deadline) in _BACKOFF.items():
        if fails >= _GIVE_UP_AFTER_FAILURES or now < deadline:
            cooling.append(tid)
        elif deadline < now - _BACKOFF_CAP_S:
            stale.append(tid)
    for tid in stale:
        del _BACKOFF[tid]
    return cooling


def _record_failure(tid: int, now: float) -> float:
    """Bump an agent's consecutive-failure count and push its next retry out
    exponentially (2s, 4s, 8s, ... capped at _BACKOFF_CAP_S). Returns the delay
    applied, for logging — meaningless once the agent is retired, which the
    caller checks with `_is_retired`."""
    fails = _BACKOFF.get(tid, (0, 0.0))[0] + 1
    delay = min(_BACKOFF_BASE_S * 2 ** (fails - 1), _BACKOFF_CAP_S)
    _BACKOFF[tid] = (fails, now + delay)
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


def _retry_note(tid: int, delay: float) -> str:
    """The retry half of a failure log line — the promise must match reality, so
    a retired agent says so rather than naming a retry that will never come."""
    if _is_retired(tid):
        return f"retired after {_GIVE_UP_AFTER_FAILURES} consecutive failures, label stays NULL"
    return f"backoff: next retry in >={delay:.0f}s"


def _clear_backoff(tid: int) -> None:
    """Drop an agent's backoff state after it labels successfully."""
    _BACKOFF.pop(tid, None)


def _select_unlabeled(cur: psycopg.Cursor, cooling: list[int]) -> list[tuple[int, str | None]]:
    """Poll up to 10 newest agents that still need a label, returning each
    `(agent_id, first_chat_prompt)`.

    ORDER BY t.id DESC prioritizes new agents — prevents old agents
    (spawn-without-prompt or with inbounds already cleaned) from clogging the
    head of the LIMIT window and starving the poll. EXISTS filters out threads
    that will never have a prompt (saves poll iterations). `cooling` ids (agents
    inside their failure-backoff window) are excluded in SQL — not after the
    LIMIT — so a cluster of persistently-failing agents neither burns an LLM call
    nor occupies the window ahead of a fresh agent.
    """
    cur.execute(
        "SELECT t.id, "
        "(SELECT im.content FROM inbound_messages im "
        " WHERE im.agent_id = t.id AND im.kind = 'chat' "
        " ORDER BY im.id LIMIT 1) AS prompt "
        "FROM agents t "
        "WHERE t.label IS NULL "
        "AND NOT t.label_user_set "
        "AND NOT (t.id = ANY(%s::bigint[])) "
        "AND EXISTS ("
        "  SELECT 1 FROM inbound_messages im2 "
        "  WHERE im2.agent_id = t.id AND im2.kind = 'chat'"
        ") "
        "ORDER BY t.id DESC "
        "LIMIT 10",
        (cooling,),
    )
    return cur.fetchall()


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.labeler.daemon"):
        _log.info("[labeler] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.labeler.daemon") or pidfile_holds_daemon(
        legacy_pid_path("labeler"), "services.labeler.daemon"
    )


async def _dispatch_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: every second, poll the newest unlabeled agents
    (`_select_unlabeled`, minus those in failure-backoff) -> grab first prompt ->
    generate label. A label that fails enters per-agent exponential backoff so a
    persistent failure does not become a hot retry loop.

    generate_label_async writes via internal CAS (WHERE label IS NULL
    AND NOT label_user_set), so user-edited labels are auto-skipped.
    """
    _log.info("[labeler] daemon started, pid=%s", os.getpid())
    while True:
        liveness.beat()
        try:
            await asyncio.sleep(_POLL_INTERVAL_S)
            now = time.monotonic()
            cooling = _cooling_ids(now)
            with pool.connection() as conn, conn.cursor() as cur:
                rows = _select_unlabeled(cur, cooling)
            for tid, prompt in rows:
                liveness.beat()  # per-item: a slow LLM call must not look like a wedge
                if not prompt:
                    continue
                try:
                    result = await generate_label_async(tid, prompt, settings.lm.labeler_model)
                except Exception as exc:
                    # Defensive: generate_label_async returns False on LLM
                    # failures instead of raising; an escaping exception is
                    # the DB CAS / publish path and is also a failure.
                    delay = _record_failure(tid, now)
                    _log.error(
                        "[labeler] generate label for thread %s failed: %r (%s)",
                        tid,
                        exc,
                        _retry_note(tid, delay),
                    )
                else:
                    if result is False:
                        # LLM failure — recorded as backoff. Keyed on the
                        # RETURN value, not an exception: generate_label_async
                        # swallows LLM errors, and the old except-keyed
                        # backoff never fired (audit round 2, P1).
                        delay = _record_failure(tid, now)
                        _log.error(
                            "[labeler] generate label for thread %s failed (%s)",
                            tid,
                            _retry_note(tid, delay),
                        )
                    else:
                        _clear_backoff(tid)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[labeler] schema / syntax error — code<->DB drift; retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[labeler] poll iteration failed")


async def run() -> None:
    """Start the daemon: healthz server -> write pidfile -> connect DB -> enter main loop."""
    if _is_running():
        _log.info("[labeler] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    _write_pidfile()
    _log.info("[labeler] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("labeler", liveness=liveness)
    _log.info("[labeler] healthz listening on :%s", health_port("labeler"))

    pool = shared.db.pool()
    try:
        await _dispatch_loop(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[labeler] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop `ava cluster update` sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `shared.daemon_shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from shared.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="labeler")
    install_graceful_shutdown("labeler")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[labeler] interrupted, shutting down")
    except Exception:
        _log.exception("[labeler] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
