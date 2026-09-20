"""The detached retained-image entry spawns: one unit's restricted hop.

Split out of `ops/cluster_deploy.py` at the file-size budget (the
`ops/updater_reap.py` pattern) so the normal-continuation spawn (task #4129
channel E) can land beside it: the deploy module sat fifteen lines under the
800-line ceiling, and the hop spawn is the deploy family's only non-deploy
trigger -- it pauses nothing, seeds no handoff, and refuses rather than acts
while the coordinator's exact pre-stop abort (task #4129 C-4) still holds.

`ops.cluster` re-exports `spawn_bootstrap_hop` from here for its existing
importers, on the same eager re-export terms as its siblings.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

import shared.cluster
import shared.ui_update_state
from ops import cluster_deploy, cluster_session, deploy_spawn, unit_local
from ops.cluster_session import _UPDATER_SERVICE
from ops.deploy_spawn import ClusterUpdateInProgress
from shared.config import settings

_log = logging.getLogger(__name__)


def spawn_bootstrap_hop(request_path: Path, *, artifact_digest: str) -> dict[str, str]:
    """Trigger a restricted bootstrap hop via a detached `ava-updater` session.

    The session runs the retained candidate image's `--bootstrap-hop` entry
    against `request_path`; every launch mechanic -- the live-session refusal
    pair (before the spawn and again inside the lifecycle mutex), the log
    allocation, the POSIX-only spelling -- lives in `_spawn_updater_entry`
    below.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: an orchestration session is already live.
        OrchestrationSpawnFailed: the session backend declined to start.
        ReleaseRejectedError: the announced digest names no retained image on
            this unit.
    """
    return _spawn_updater_entry(
        request_path,
        cli_flag="--bootstrap-hop",
        entry_label="hop",
        artifact_digest=artifact_digest,
    )


def _spawn_updater_entry(
    request_path: Path,
    *,
    cli_flag: str,
    entry_label: str,
    artifact_digest: str,
) -> dict[str, str]:
    """Spawn a detached `ava-updater` session that runs one retained-image entry.

    The shared body of the entry triggers (`spawn_bootstrap_hop`; the
    normal-continuation spawn lands beside it): the session runs the *retained
    candidate image's* interpreter (`unit_local.candidate_interpreter` --
    strictly resolved under this unit's `releases/`) on
    `-m cli.commands._update_agent_runner <cli_flag> <request>`. The announced
    digest selects an image, never authorizes one -- the entry re-derives every
    binding from the request bytes, and its own compare-and-set
    (`begin_bootstrap_after_dead_owner`) refuses a live or unproven handoff
    owner. Unlike `spawn_update`, nothing is paused and no handoff is seeded:
    the coordinator's exact pre-stop abort (task #4129 C-4) relies on "no unit
    has acted" staying true until the child's first effect.

    Refuses when any orchestration session on this host is already alive (the
    same first line as `spawn_update`), rechecked inside the lifecycle mutex
    before the spawn.

    POSIX-only by construction: the ops handlers refuse any non-Linux platform
    before calling here, so the `native_cmd` slot is unreachable -- it carries
    the POSIX spelling only to satisfy the session backend's two-flavor
    signature.

    Returns {"session": "ava-updater", "log": <path>}.

    Raises:
        ClusterUpdateInProgress: an orchestration session (`ava-updater` /
            rollout / cluster-restart) is already live on this host.
        OrchestrationSpawnFailed: the session backend declined to start the
            entry session.
    """
    deploy_spawn.assert_prod_home_has_its_own_checkout()
    updater_sess = shared.cluster.session_name(_UPDATER_SERVICE)
    if cluster_session._has_orchestration_session(updater_sess):
        raise ClusterUpdateInProgress(
            f"orchestration session {updater_sess!r} already exists; an update "
            f"or a {entry_label} is in flight. Wait for it to finish — a hung updater is "
            f"force-reaped automatically — or terminate the pid named in "
            f"$AVA_HOME/run/sessions/{updater_sess}.json if it is hung."
        )
    log_path = cluster_deploy._new_update_log("updater")
    home = settings.general.ava_home
    interpreter = unit_local.candidate_interpreter(home, artifact_digest)
    inner_cmd = (
        f"{{ export AVA_CLI_LOG_NAME=updater; "
        f"if cd {shlex.quote(str(home))}; "
        f"then {shlex.quote(str(interpreter))} -I -B -m cli.commands._update_agent_runner "
        f"{cli_flag} {shlex.quote(str(request_path))}; rc=$?; "
        f"else rc=$?; echo '[updater] cannot enter the unit home; nothing to run'; fi; "
        f'echo "[session-exit] rc=$rc"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    # Bracket the spawn so a stall inside it is attributable from the log alone
    # (the 2026-08-12 shape; the same pair `spawn_update` writes).
    _log.info("[cluster] spawning %s session %s (log=%s)", entry_label, updater_sess, log_path)
    with shared.ui_update_state.lifecycle_lock():
        live_session = cluster_session.live_orchestration_session()
        if live_session is not None:
            raise ClusterUpdateInProgress(
                f"orchestration session {live_session!r} already exists; "
                f"an update or a {entry_label} is in flight"
            )
        cluster_session._spawn_detached_session(
            updater_sess, shell_cmd=inner_cmd, native_cmd=inner_cmd
        )
    _log.info("[cluster] spawned %s session %s log=%s", entry_label, updater_sess, log_path)
    return {"session": updater_sess, "log": str(log_path)}
