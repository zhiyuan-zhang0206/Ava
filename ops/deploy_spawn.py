"""Shared refusal and ownership helpers for detached deploy sessions."""

from __future__ import annotations

import logging
import shlex
import time

import shared.ui_update_state
from ops import cluster_session
from shared.deploy_timing import ORCHESTRATION_OWNER_WAIT_S
from shared.paths import repo_root

_log = logging.getLogger(__name__)
_UI_OWNER_POLL_S = 0.05


class ClusterUpdateInProgress(RuntimeError):  # noqa: N818 — state description
    """A whole-cluster or host updater orchestration is already in flight."""


class ProdHomeFromForeignCheckout(RuntimeError):  # noqa: N818 — state description
    """The resolved home is the production home but this checkout is not its own.

    The same refusal `ava start` / `respawn_service` / `os_cron` already make
    (``shared.paths.prod_service_checkout_error`` — the 01:13 worktree accident,
    Task #966): the prod home's services may only be driven from its anchored
    checkout ``~/.ava/source``. The deploy triggers and the pause/unpause pair
    were the last destructive surfaces that lacked it; a process from any other
    checkout — a test subprocess, a stray dev clone — must fail before it
    writes a handoff, pauses the host, or spawns a session.
    """


def assert_prod_home_has_its_own_checkout() -> None:
    """Refuse a detached deploy when the resolved home is the production home but
    the executing checkout is not the production-anchored checkout.

    The deploy family's entry points used to be reachable from any checkout:
    ``cli.preflight.require_anchored_home`` stops an UNANCHORED checkout, but
    an env-supplied ``AVA_HOME`` reads as anchored (dotenv_boot rule 1), and a
    direct call to ``spawn_update`` / ``spawn_rollout`` / ``spawn_restart`` /
    ``unpause_local_cluster`` (a test subprocess, an agent) bypasses the CLI
    gate entirely. A test subprocess inheriting the operator's production
    ``AVA_HOME`` sailed through both and ``spawn_update`` wrote the pending
    updater handoff and paused the production host (2026-08-27 incident,
    Gateway 503). This closes that shape: prod home + non-prod checkout =
    refuse, before any deploy side effect — no updater-log dir creation, no
    handoff write, no pause, no session spawn.

    The executing checkout is ``shared.paths.repo_root()`` — derived from this
    module's own physical location, so no caller (cwd / env / sys.path) can
    point it at a different checkout.

    A non-prod home (a dev worktree cluster, a CI tmpfs home) always passes.
    """
    from shared.paths import prod_service_checkout_error

    refusal = prod_service_checkout_error(repo_root())
    if refusal is not None:
        raise ProdHomeFromForeignCheckout(refusal)


def wait_for_ui_owner(
    *, session: str, kind: shared.ui_update_state.UiUpdateKind, origin: str
) -> None:
    """Wait until the detached DB-lock winner publishes maintenance ownership.

    This runs outside the lifecycle mutex: the child needs that same mutex to
    acquire the authoritative deploy lease and publish the marker. A timeout
    never clears state because the launch may have succeeded despite a late
    backend bookkeeping error; the child alone owns its eventual generation.
    """
    deadline = time.monotonic() + ORCHESTRATION_OWNER_WAIT_S
    while time.monotonic() < deadline:
        snapshot = shared.ui_update_state.read()
        if (
            snapshot.status == "updating"
            and snapshot.kind == kind
            and (snapshot.legacy or snapshot.origin == origin)
        ):
            return
        if not cluster_session._has_orchestration_session(session):
            raise cluster_session.OrchestrationSpawnFailed(
                f"detached {kind} session exited before publishing its persistent "
                "maintenance owner",
                started=True,
            )
        time.sleep(_UI_OWNER_POLL_S)
    raise cluster_session.OrchestrationSpawnFailed(
        f"detached {kind} session did not publish its persistent maintenance "
        f"owner within {ORCHESTRATION_OWNER_WAIT_S:g}s; the session was not "
        "cleared because it is still alive and may be acquiring the "
        "authoritative deploy lease",
        started=True,
    )


def assert_no_orchestration_in_flight(*, force: bool = False) -> None:
    """Refuse a second deploy locally and, unless forced, cluster-wide."""
    session = cluster_session.live_orchestration_session()
    if session is not None:
        raise ClusterUpdateInProgress(
            f"orchestration session {session!r} already exists; a rollout / restart "
            f"/ update is in flight. Wait for it to finish — a hung session is "
            f"force-reaped automatically — or terminate the pid named in "
            f"$AVA_HOME/run/sessions/{session}.json if it is hung."
        )
    if force:
        _log.warning("[cluster] --force: skipping the cluster-wide deploy-window check")
        return

    from ops.deploy_window import deploy_in_flight

    window = deploy_in_flight()
    if window.active:
        raise ClusterUpdateInProgress(
            f"a deploy is already in flight: {window.detail}. Two concurrent deploys "
            f"defeat the rollout's own safety (each pins its own commit and quiesces "
            f"into the other's window — the 2026-07-28 collision cost two agents). "
            f"Wait for `ava cluster status` to show every host on the pin, or re-run "
            f"with --force if you are certain that deploy is dead."
        )


def update_entry_args(
    *,
    target_sha: str | None = None,
    mode: str,
    force_reap: bool,
    handoff_generation: str,
) -> str:
    """Build the flag tail for the detached POSIX updater entry."""
    args = f" --mode {shlex.quote(mode)}"
    args += f" --handoff-generation {shlex.quote(handoff_generation)}"
    if target_sha:
        args += f" --target-sha {shlex.quote(target_sha)}"
    if force_reap:
        args += " --force-reap"
    return args
