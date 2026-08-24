"""Shared refusal and ownership helpers for detached deploy sessions."""

from __future__ import annotations

import logging
import shlex
import time

import shared.ui_update_state
from ops import cluster_session
from shared.deploy_timing import ORCHESTRATION_OWNER_WAIT_S

_log = logging.getLogger(__name__)
_UI_OWNER_POLL_S = 0.05


class ClusterUpdateInProgress(RuntimeError):  # noqa: N818 — state description
    """A whole-cluster or host updater orchestration is already in flight."""


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
