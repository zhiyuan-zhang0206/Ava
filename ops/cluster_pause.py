"""The pause/unpause lifecycle — put this host into 503 mode and bring it back.

One half of a deploy's quiescing: `pause_local_cluster` is what a gateway's
Phase A fan-out reaches on each agent-runner, and `unpause_local_cluster` is its
exact inverse (also delivered as the compensating resume on a failed rollout).
`is_paused` is the posture read the gateway middleware does on every request.

The paused state is the `host_deploy_state.posture` row (R1, Task #1021) —
one row per machine in the central DB, so the gateway's middleware, the
stranded-pause controller and the rollout poll all read the same authority.
The old `cluster_paused` file and `updating.flag` were retired by the
old-signal sweep (PR5): this module writes the row and the local mirror only.
"""

from __future__ import annotations

import logging

import shared.cluster
import shared.db
from ops.cluster_session import (
    _REPO_ROOT,
    _RESTARTER_SERVICE,
)

_log = logging.getLogger(__name__)

# Restarter launch command — duplicated from cli/commands/_repo.py because gateway
# must not import cli (layering shared < gateway < cli); one constant is
# acceptable. unpause_local_cluster respawns the session with it.
_RESTARTER_CMD = ".venv/bin/python -m services.restarter.daemon"


def is_paused() -> bool:
    """Whether this host is paused — the `host_deploy_state.posture` row written
    by the gateway's pause fan-out (R1, Task #1021).

    Gateway middleware checks this on every request. The row is read from the
    central DB, which the gateway owns; a read failure (DB unreachable) reads as
    NOT paused — the same conservative direction the old file stat had (an
    unreadable flag was an absent flag), and the offline "updating" label comes
    from the mirror file, not from here.
    """
    from shared.host_deploy_state import read

    try:
        state = read()
    except Exception:
        _log.warning(
            "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
            exc_info=True,
        )
        return False
    return state is not None and state.posture == "paused"


def pause_local_cluster() -> None:
    """Phase A handler logic — put this host into paused state.

    1. write `host_deploy_state.posture = paused` (with `paused_at` = now) ->
       the middleware immediately returns 503 for SDK paths
    2. kill the `ava-restarter` session -> restarter daemon stops
       polling DB (the gateway is about to migrate; old code
       reading new schema would cause issues)

    Watchdog reads the same row and skips an entire healthcheck round
    when paused — otherwise the watchdog would 60s later resurrect the
    restarter and fight this function.

    Idempotent: already paused -> harmless repeat (upsert / kill are both
    idempotent).
    """
    # R1 (Task #1021): the posture row + the local mirror file are the pause —
    # the row is the authority the 503 middleware and stranded-pause controller
    # read; the mirror labels the offline "updating" page. The old flag files
    # were retired with the old-signal sweep (PR5).
    from shared.host_deploy_state import set_posture

    set_posture("paused")

    # Snapshot agent status counts before killing the restarter. An idling row
    # with no pid is mid-launch (respawn / resurrect committed its wake but the
    # child has not yet claimed): killing the restarter here can leave it for
    # the boot reaper. Logging that count makes the race visible in the pause
    # log.
    # Best-effort: this is pure observability on the rollout-critical pause path,
    # so a transient DB blip must not abort the pause — log and carry on.
    try:
        with shared.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM agents_meta GROUP BY status ORDER BY status")
            status_counts = dict(cur.fetchall())
            cur.execute("SELECT count(*) FROM agents_meta WHERE status = 'idling' AND pid IS NULL")
            unclaimed_row = cur.fetchone()
            unclaimed_count = 0 if unclaimed_row is None else unclaimed_row[0]
        _log.info(
            "[cluster] pausing: agent status snapshot %s (unclaimed_idling=%d in-flight launch)",
            status_counts,
            unclaimed_count,
        )
    except Exception:  # fail-fast-ok: status snapshot is logging-only, never blocks pause
        _log.warning("[cluster] pausing: agent status snapshot failed", exc_info=True)

    # Kill the restarter session via platform backend (idempotent —
    # absent session is a silent noop).
    restarter_sess = shared.cluster.session_name(_RESTARTER_SERVICE)
    from shared.session_backend import get_backend

    ok, mode = get_backend().kill_session(restarter_sess, graceful=False, expected=True)
    if ok:
        _log.info("[cluster] killed session %s (mode=%s)", restarter_sess, mode)


def unpause_local_cluster() -> None:
    """Inverse of `pause_local_cluster` — bring this host back from paused.

    1. write `host_deploy_state.posture = idle` (with `paused_at` = NULL) ->
       the middleware stops returning 503
    2. respawn the `ava-restarter` session **only if not already alive** -> the
       restarter resumes claiming agents

    Idempotent: already idle / restarter already alive -> harmless no-op. The
    gateway's compensating unpause may be delivered after a host already
    recovered on its own, so a repeat call must do nothing.
    """
    # R1 (Task #1021): the posture row + the local mirror file are the unpause —
    # the row the 503 middleware reads returns to idle (paused_at cleared), and
    # the offline "updating" label goes with it: the gate must not keep showing
    # "updating" once this host is serving again (or about to be: every recovery
    # path that unpauses ends in `ava start`, and a host that is down after a
    # failed recovery is DOWN, not updating).
    from shared.host_deploy_state import set_posture

    set_posture("idle")
    _log.info("[cluster] unpaused: posture -> idle")

    restarter_sess = shared.cluster.session_name(_RESTARTER_SERVICE)
    # The restarter is a SERVICE, so it lives on the session backend (native
    # supervisor on POSIX, winproc on Windows) — the same backend `ava start`
    # and the healthcheck respawn use, and the same backend the orchestration
    # sessions moved onto in S7. It spawns through the backend directly, not
    # via the orchestration helper (`_spawn_detached_session`): the restarter's
    # command carries no tee / `[session-exit]` wrapper, and keeping one spawn
    # shape per session kind means a start-spawned restarter and an
    # unpause-spawned one can never diverge into double-running the same
    # agents.
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

    if get_backend().has_session(restarter_sess):
        _log.info("[cluster] %s already alive; not respawning", restarter_sess)
        return
    try:
        ok = get_backend().new_session(
            restarter_sess, _RESTARTER_CMD, _REPO_ROOT, env=forward_env_dict()
        )
    except Exception:
        ok = False
    if not ok:
        # A concurrent resume delivery may have created the session between the
        # has-session check above and here — that is success, not failure. Only
        # raise if the session still is not up.
        if get_backend().has_session(restarter_sess):
            _log.info("[cluster] %s came up concurrently; treating as success", restarter_sess)
            return
        raise RuntimeError(f"could not respawn {restarter_sess} session")
    _log.info("[cluster] respawned %s session", restarter_sess)
