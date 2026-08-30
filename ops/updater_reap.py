"""The hung-updater reaper — judge a host's `ava-updater` session, kill it when
it has stopped making progress.

Split out of `ops/cluster_deploy.py` (P1, 2026-08-30): that file sat one line
under the 800-line hard ceiling and its own docstring says to split when a
*topic* leaves — the reaper trio (`_updater_hung` + `_reap_stalled_updater` +
`reap_stalled_updater_if_hung`) is one topic, and the stage-evidence no-progress
judgment made it exactly too big for the room left.

The evidence, both halves (R1, Task #1021 + P1 2026-08-30):

- **the updater lease** (`shared.host_deploy_state`) — liveness. A lease armed by
  THIS pause window that has run out means the updater stopped renewing and is
  hung; a live lease means "still working"; no lease cannot be judged (killing on
  missing evidence is the worse mistake — the retired log-mtime fallback).
- **the stage markers** (`ops.updater_outcome.stage_evidence_stuck`) — progress. A
  lease is one write at the run's start, so an updater stuck inside one stage (a
  hung `uv` download on the Windows runner) reads "still working" for the whole
  bound; the tail's last `t=` marker judged against
  `STAGE_NO_PROGRESS_TIMEOUT_S` — the same bound the Phase-B poll judges — is the
  fact that cuts it loose early.

`spawn_update` (still in `ops.cluster_deploy`) reaps inline when a new update
arrives; `ops.controllers.stalled_updater` runs the scheduled half once a
watchdog round. Everything the facade re-exports from `ops/cluster.py` keeps
resolving.
"""

from __future__ import annotations

import contextlib
import logging

import shared.cluster
from ops import cluster_session
from ops.cluster_session import _UPDATER_SERVICE
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S

_log = logging.getLogger(__name__)

# The most a True from `_reap_stalled_updater` may claim. Both call sites — `spawn_update`
# in cluster_deploy and `ops.controllers.stalled_updater` — interpolate it instead of wording the
# ambiguity themselves, which is how one of them drifted into claiming a kill nobody made.
REAP_CLEARED_QUALIFIER = "killed, or already exited by the recheck"


def _updater_hung(session: str) -> bool:  # noqa: ARG001 — the session name is the judgment's subject; the evidence is the lease + stage markers
    """Whether this host's updater session is hung — the lease-expiry judgment,
    plus the stage-evidence no-progress fact (P1, 2026-08-30).

    R1 (Task #1021): liveness = the updater's lease in `host_deploy_state`
    (`shared.host_deploy_state.touch_updater_lease`, written first thing by the
    update chains and the in-process self-update). A live lease means "still
    working"; a lease armed by THIS pause window that has run out means the
    updater stopped renewing and is hung (`HostDeployState.updater_expired` —
    which is also what stops a *previous* run's uncleared expiry from condemning
    the session this update just spawned). A session with NO lease cannot be
    judged — a lease write that failed at entry, or an updater spawned before the
    lease existed — and reads as NOT hung (the old log-mtime fallback was retired
    with the old-signal sweep, PR5; killing on missing evidence is the worse
    mistake).

    **The lease alone was not enough (P1, 2026-08-30).** It is one write at the
    run's start, so an updater stuck inside one stage (a hung `uv` download on the
    Windows runner) reads "still working" for the whole bound. The stage markers
    are the progress fact: `ops.updater_outcome.stage_evidence_stuck` judges the
    tail's last `t=` marker against the same `STAGE_NO_PROGRESS_TIMEOUT_S` the
    Phase-B poll uses. An idle host is never judged on stage evidence — its
    transition already finished, and the only shape that lingers there is the
    ladder's final `done` marker behind a stuck lease clear, which a reap would
    not improve.
    """
    from shared.host_deploy_state import POSTURE_IDLE, read

    try:
        state = read()
    except Exception:
        return False  # unreadable liveness -> not hung; the caller refuses instead
    if state is None or state.updater_expired:
        return state is not None and state.updater_expired
    if state.posture == POSTURE_IDLE:
        return False
    from ops.updater_outcome import stage_evidence_stuck

    return stage_evidence_stuck()


def _reap_stalled_updater(session: str) -> bool:
    """True if `session` (the ava-updater session) is gone because this call cleared it.

    A hung updater session blocks every future update, rollout, restart, and even
    /api/cluster/recover on this host until a human kills the session by hand. The
    caller supplies the liveness half (`_updater_hung`); this function kills and
    handles the kill race.
    """
    # The updater session lives on the service session backend (S7 — the same
    # backend as every other session; before S7 it stayed on its own), so the kill
    # goes to get_backend(), never get_shell_backend().
    from shared.session_backend import get_backend

    _log.warning("[cluster] %s judged hung — force-killing as hung", session)
    try:
        killed, mode = get_backend().kill_session(session, graceful=False, expected=True)
    except Exception:
        _log.exception("[cluster] failed to kill stalled updater session %s", session)
        return False
    if not killed:
        # `ok=False` now means the backend re-asked `has-session` and the session was
        # still there (issue #1015 made that the contract; it used to be the raw
        # kill-session exit status, which is *also* non-zero for a target that
        # cannot be found — indistinguishable from a session that finished on its own
        # between the caller's liveness check and this kill). Re-ask anyway. It costs
        # one `has-session` and it is the only question that matters here, so this
        # stays correct whichever way a future backend answers `ok`; treating a benign
        # race as a failure would make `spawn_update` raise `ClusterUpdateInProgress`
        # naming a session that is not there, blocking an update for no reason.
        try:
            still_there = cluster_session._has_orchestration_session(session)
        except Exception:
            # Cannot tell: keep the error. A false "cleared" is the expensive
            # direction — it spawns a second updater over a live one, which is the
            # 2026-05-25 incident that `ClusterUpdateInProgress` exists to prevent.
            still_there = True
        if not still_there:
            _log.info(
                "[cluster] %s exited on its own before the kill landed — nothing to reap",
                session,
            )
            return True
        # It survived its own kill. Reporting it as reaped would tell `spawn_update`
        # to spawn over a session that is still there, and would make the watchdog
        # round log a fresh "force-killing as hung" every 60s while claiming success.
        _log.error(
            "[cluster] backend declined to kill stalled updater session %s (mode=%s); "
            "kill it by hand: terminate the pid named in "
            "$AVA_HOME/run/sessions/%s.json",
            session,
            mode,
            session,
        )
        return False
    # A killed updater cannot clear its own lease, and the lease is what keeps the
    # host reading "live updater" — clear it so the poll sees the stall in two
    # probes and this host's controllers stop deferring to a corpse (P1,
    # 2026-08-30). Fail-soft: an unreachable DB leaves the expiry to its TTL.
    with contextlib.suppress(Exception):
        from shared.host_deploy_state import clear_updater_lease

        clear_updater_lease()
    return True


def reap_stalled_updater_if_hung() -> bool:
    """Kill this host's `ava-updater` session if it is alive and has stopped writing.

    The scheduled half of the reaper — `ops.controllers.stalled_updater` calls this
    once a watchdog round, so a hung updater is cut loose on a clock of its own
    instead of waiting for someone to try to deploy.

    **Waiting for the next attempt does not work, because the corpse refuses it.**
    A live `ava-updater` makes `current_orchestration()` answer `"update"` forever,
    and that answer is read by everything that could have cleared it:
    `_assert_no_orchestration_in_flight` fails the gateway's next `ava cluster update`
    cluster-wide on signal 3 of `ops.deploy_window` (so Phase B never fans out, and
    the `spawn_update` that carries the other reap call is never reached), and this
    host's own **code**, **pin** and **stranded-pause** controllers all defer to it as
    the normal mid-deploy transient. Pin and pause once gated on the
    `update_lock_holder()` lease alone — which a watchdog-spawned `spawn_update` never
    takes, so an off-pin host reached `spawn_update`'s inline reap and recovered
    on its own — but that same blindness let the pin controller force-checkout
    underneath a live updater and flap prod between two commits (issue #1074), so all
    three now read the session. A hung session therefore refuses the cluster's deploy
    path and every one of this host's self-heals, and the only exits are `--force`
    (which also suppresses the real deploy-window protection this is impersonating) or
    a human killing the pid named in `$AVA_HOME/run/sessions/ava-updater.json`. Bounding
    the Phase-B poll does not touch that:
    the poll protects a rollout that *started*, and this refuses the start.

    Returns True when the hung session is gone on our account — killed here, or found
    already exited on the recheck after a kill that reported failure (see
    `_reap_stalled_updater`, which treats that benign race as cleared). False means
    nothing was hung, or the session survived its own kill. Never raises — it runs
    inside a controller round.
    """
    try:
        session = shared.cluster.session_name(_UPDATER_SERVICE)
        if not cluster_session._has_orchestration_session(session):
            return False
        if not _updater_hung(session):
            return False
        return _reap_stalled_updater(session)
    except Exception:
        _log.exception("[cluster] stalled-updater reap failed; retrying next round")
        return False


# The family's one no-progress definition (`shared.deploy_timing`), shared with the
# settle-hold TTL and the gateway's Phase-B poll: three clocks that used to disagree
# about when a host has stopped making progress, and the smallest of them decided
# when the deploy lease stopped protecting the deploy.
_UPDATER_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S
