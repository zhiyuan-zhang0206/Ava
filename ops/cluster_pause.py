"""The pause/unpause lifecycle — put this host into 503 mode and bring it back.

One half of a deploy's quiescing: `pause_local_cluster` is what a gateway's
Phase A fan-out reaches on each agent-runner, and `unpause_local_cluster` is its
exact inverse (also delivered as the compensating resume on a failed rollout).
`is_paused` is the posture read the gateway middleware does on every request.

The paused state is the `host_deploy_state.posture` row (R1, Task #1021) —
one row per machine in the central DB, so the gateway's middleware, the
stranded-pause controller and the rollout poll all read the same authority.
The old `cluster_paused` file and `updating.flag` were retired by the
old-signal sweep (PR5): this module writes only the host posture row.
"""

from __future__ import annotations

import logging
from typing import cast

import shared.cluster
import shared.db
import shared.host_deploy_state
from ops.cluster_session import (
    _REPO_ROOT,
    _RESTARTER_SERVICE,
)

_log = logging.getLogger(__name__)
_UNSET = object()

# Restarter launch command — duplicated from cli/commands/_repo.py because gateway
# must not import cli (layering shared < gateway < cli); one constant is
# acceptable. unpause_local_cluster respawns the session with it.
_RESTARTER_CMD = ".venv/bin/python -m services.restarter.daemon"


def is_paused(
    state: shared.host_deploy_state.HostDeployState | None | object = _UNSET,
) -> bool:
    """Whether this host is paused — the `host_deploy_state.posture` row written
    by the gateway's pause fan-out (R1, Task #1021).

    Gateway middleware checks this on every request. The row is read from the
    central DB, which the gateway owns; a read failure (DB unreachable) reads as
    NOT paused — the same conservative direction the old file stat had (an
    unreadable flag was an absent flag). The offline maintenance page is owned
    separately by the cluster orchestrator's Gate marker.
    """
    if state is _UNSET:
        try:
            resolved_state = shared.host_deploy_state.read()
        except Exception:
            _log.warning(
                "[cluster] is_paused: host_deploy_state read failed; reading as not paused",
                exc_info=True,
            )
            return False
    else:
        resolved_state = cast(shared.host_deploy_state.HostDeployState | None, state)
    return resolved_state is not None and resolved_state.posture == "paused"


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
    # R1 (Task #1021): the posture row is the host pause authority read by the
    # 503 middleware and stranded-pause controller. The cluster orchestrator's
    # separate Gate marker owns the offline maintenance page.
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
    2. respawn the `ava-restarter` session **only if not already alive and the
       roster still enables it** (hosted mode gates the restarter off; a pause
       must not resurrect a service ``ava start`` would skip) -> the restarter
       resumes claiming agents

    Idempotent: already idle / restarter already alive -> harmless no-op. The
    gateway's compensating unpause may be delivered after a host already
    recovered on its own, so a repeat call must do nothing.
    """
    # Same prod-home refusal as the deploy triggers (ops.deploy_spawn): the
    # restarter respawn below launches a SERVICE from this checkout, so a
    # foreign checkout acting on the prod home would run prod's restarter on
    # disposable code (the 2026-07-24 outage class) — refuse before the
    # posture write or the respawn.
    from ops.deploy_spawn import assert_prod_home_has_its_own_checkout
    from shared import maintenance

    maintenance.require_start_allowed()
    assert_prod_home_has_its_own_checkout()
    # R1 (Task #1021): unpause owns host posture only. The cluster UI marker is
    # deliberately separate and spans local pause/start plus the full Phase-B
    # tail; only its orchestration generation or proven recovery clears it.
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
    # `--disable-service restarter` is operator intent, not a hint local
    # recovery may override.  The watchdog already honors the same marker; the
    # direct unpause path must do so as well because it is the rollout's final
    # resume boundary and otherwise bypasses the watchdog entirely.
    from ops.spec import gate_reason_for_session
    from shared.disabled_services import is_skipped, read_skipped
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

    # The ROSTER gate outranks the durable marker: a service the roster
    # disables for THIS host (hosted mode's restarter — per-agent process
    # supervision retired) must not be resurrected by an orchestration
    # finally, or every pause/unpause boundary (a rollout's resume) relaunches
    # it on the new SHA — the 18:56 / 22:20 / 22:28 restarter incidents of
    # 2026-09-02, where ``ava start`` skipped the service correctly and this
    # respawn undid the skip. Asked through ops.spec's gate so the decision
    # cannot drift from the start roster; a lookup surprise leaves it down
    # (same fail-closed direction as an unreadable marker below).
    try:
        gate_reason = gate_reason_for_session(_RESTARTER_SERVICE)
    except Exception:
        _log.error(
            "[cluster] roster gate lookup failed; leaving restarter down",
            exc_info=True,
        )
        return
    if gate_reason is not None:
        _log.info(
            "[cluster] restarter is roster-disabled on this host (%s); leaving it down",
            gate_reason,
        )
        return

    try:
        restarter_disabled = is_skipped(_RESTARTER_SERVICE, read_skipped())
    except Exception:
        # Unpause is routinely called from a compensating ``finally``.  Do not
        # replace the original rollout outcome with a marker read exception,
        # and do not guess that agent relaunch is permitted.
        _log.error(
            "[cluster] disabled-services marker unreadable; leaving restarter down",
            exc_info=True,
        )
        return
    if restarter_disabled:
        _log.info("[cluster] restarter is durably disabled; leaving it down")
        return

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


def finalize_pause_owner_journal() -> None:
    """Generation-scoped successful-finalize of the pause-owner journal for a
    host that returned to serving WITHOUT a `cluster/resume` op — the natural
    resume (a Phase-B `ava start`), or the gateway-local finally's own unpause.

    Records the journaled paused generation as `resumed` — the same CAS
    `mark_resumed` writes on the explicit resume path — so a rollout that
    finished cleanly never leaves deploy-pause-owner.json `paused` forever
    (the 2026-08-26 residue: rollout rc=0, the journal still paused). It is
    generation-scoped by construction and never force-clears: an absent /
    legacy / invalid journal is left for recovery's no-live-owner proof.

    Never raises: the journal is bookkeeping, and the callers run on paths (a
    start tail, a rollout `finally`) where a raise would mask the real
    outcome; a failure is logged and swallowed.
    """
    from shared import pause_owner

    try:
        if pause_owner.finalize_natural_resume():
            _log.info("[cluster] pause-owner journal: recorded the deploy pause as resumed")
    except Exception:
        _log.warning(
            "[cluster] pause-owner journal finalize failed (non-fatal); the paused "
            "record stays for recovery's no-live-owner proof",
            exc_info=True,
        )
