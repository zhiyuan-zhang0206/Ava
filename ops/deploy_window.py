"""Is a deploy or maintenance window open right now: what the health probe asks
before it grades an outage.

Two signals, in confidence order:

1. **A live deploy lease** (`base.deploy.state.cluster_lock.read_update_lease`).
   No current code takes a deploy lease except `ava cluster recover`, which
   claims one while it clears a stranded pause, so this fires for that claim or
   a leftover row until its TTL.
2. **Any machine's `host_deploy_state` posture row** not at `idle`. `ava stop`,
   maintenance holds and `ava start` write the row outside the services they
   restart, so it survives the whole window; a machine with no row has never
   transitioned and reads as idle. A stale `converging` row keeps the signal
   active until `ava cluster recover` unpauses the host (conservative: its
   checkout may have moved).

Signal 2 reads the cohort, not every row that ever registered. A machine the
operator has excluded (pause latch, staging flag, stop announcement) is not a
rollout target, and an excluded machine that is actually off runs no controller
that would return its row to `idle`, so its last posture can sit in the table
indefinitely. Read as "a deploy is running", one such row would refuse every
later transition (issue #2160). An excluded machine's row therefore counts only
with a live updater lease behind it. The withhold is never silent: every skipped
row logs the machine, its exclusion, the posture and its age, and a row that does
block carries the same freshness in `detail`.

`ava cluster status` shows the lease directly (`read_update_lease`) and must not
call `deploy_in_flight()`: that call reads every machine's row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from base.deploy.state.host_deploy_state import POSTURE_IDLE, HostDeployState, read_all
from base.log import logger


@dataclass(frozen=True)
class DeployWindow:
    """Whether a deploy owns the cluster, and the evidence for it.

    `detail` is a full sentence naming that evidence — printed verbatim to whoever
    is being refused or suppressed, because a second operator must SEE the conflict
    rather than discover it from two force-terminated agents afterwards.
    """

    active: bool
    detail: str

    def __bool__(self) -> bool:
        return self.active


_IDLE = DeployWindow(active=False, detail="no deploy in flight")


def _machines() -> list[tuple[str, str | None]]:
    """Every registered machine, or an empty list when the table cannot be read."""
    try:
        import base.cluster.machines

        return base.cluster.machines.list_all()
    except Exception as exc:
        logger.warning("[deploy-window] could not list machines: {exc!r}", exc=exc)
        return []


def _read_excluded() -> dict[str, tuple[str, datetime | None]]:
    """machine -> (reason, since) for the machines an operator latch excludes
    from the rollout cohort; {} when the read fails.

    The failure direction is the opposite of the reads below: this map can only
    *withhold* a refusal, so "could not read" must not come back as "nothing is
    excluded" — a Postgres hiccup would pardon exactly the stale row the
    exclusion exists to identify. It degrades to reading every posture row as
    before the refinement existed (issue #2160): a failed read refuses more,
    never less.
    """
    try:
        from base.cluster.machine_exclusions import list_excluded_machines

        return {name: (reason, since) for name, reason, since in list_excluded_machines()}
    except Exception as exc:
        logger.warning("[deploy-window] could not list excluded machines: {exc!r}", exc=exc)
        return {}


def _stamp(moment: datetime) -> str:
    """One timestamp at second precision — the row's own date, so a reader can
    match it against `host_deploy_state` without parsing microseconds."""
    return moment.isoformat(timespec="seconds")


def _age(now: datetime, then: datetime) -> str:
    """A compact age (`43s` / `12m` / `25h` / `3d`) — the freshness half of a
    deploy-window diagnostic, printed beside the exact timestamp."""
    seconds = max(0.0, (now - then).total_seconds())
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _exclusion_phrase(reason: str, since: datetime | None) -> str:
    """`operator-excluded (paused since 2026-09-09T13:54:31+00:00)` — status and
    date in one clause; the staging flag has no date column, so it reads bare."""
    dated = f" since {_stamp(since)}" if since is not None else ""
    return f"operator-excluded ({reason}{dated})"


def _mid_deploy_detail(
    name: str, state: HostDeployState, exclusion: tuple[str, datetime | None] | None
) -> str:
    """The refusal sentence: the machine, its posture, and — the issue #2160
    diagnostics — the evidence's freshness, plus the exclusion status when one
    is in play.

    A live updater lease is dated by its expiry; without one the row is stale
    and dated by its age. The exclusion appears only alongside a live lease,
    because that is the only combination that still blocks: a reader of a
    refusal must never have to guess why an excluded machine is in it.
    """
    if state.updater_live and state.updater_lease_expires_at is not None:
        evidence = f"live updater lease, expires {_stamp(state.updater_lease_expires_at)}"
        if exclusion is not None:
            evidence += f"; {_exclusion_phrase(*exclusion)} — a live updater outranks the exclusion"
    else:
        evidence = (
            f"no live updater lease; posture last written {_stamp(state.updated_at)} "
            f"({_age(state.db_now, state.updated_at)} ago)"
        )
    return f"machine {name!r} is mid-deploy (host_deploy_state.posture={state.posture}, {evidence})"


def _log_excluded_posture(
    name: str, state: HostDeployState, exclusion: tuple[str, datetime | None]
) -> None:
    """The trailing record for a row signal 2 deliberately did NOT read as a
    deploy — without it, "the excluded host is not blocking" and "the excluded
    host was never read" are the same silence in the log."""
    reason, since = exclusion
    logger.info(
        "[deploy-window] ignoring stale posture: machine {name!r} is {exclusion}; "
        "posture={posture} last written {stamp} ({age} ago), no live updater lease "
        "— excluded machines are not rollout targets, so this is not a competing deploy",
        name=name,
        exclusion=_exclusion_phrase(reason, since),
        posture=state.posture,
        stamp=_stamp(state.updated_at),
        age=_age(state.db_now, state.updated_at),
    )


def _remote_orchestration() -> DeployWindow | None:
    """Any machine mid-deploy — signal 2, read from the host_deploy_state
    table instead of probing each machine's ops server (R1, Task #1021).

    The posture row is written outside the services a stop restarts, so it
    survives the whole window, while an ops daemon stops with the services it
    would report on; a machine with no row has never transitioned and reads as
    idle. A stale `converging` row keeps the signal active until `ava cluster
    recover` unpauses the host — the conservative direction, since its checkout
    may have moved.

    **Operator exclusion is the one reading this signal withholds** (issue
    #2160): a machine the operator has excluded from the cohort — pause latch,
    staging flag, stop announcement — is not a rollout target, so a leftover
    posture row on it is not a competing deployment, *unless* it carries a live
    updater lease: a live owner is a deploy wherever it runs. (A running
    excluded host may still return its own row to `idle`; a stopped one never
    will — the filter does not depend on which.) See the module docstring
    ("Signal 2 reads the cohort"). Never raises.
    """
    machines = _machines()
    if not machines:
        return None
    states = _read_deploy_states()
    excluded = _read_excluded()
    for name, _url in machines:
        state = states.get(name)
        if state is None or state.posture == POSTURE_IDLE:
            continue
        exclusion = excluded.get(name)
        if exclusion is not None and not state.updater_live:
            _log_excluded_posture(name, state, exclusion)
            continue
        return DeployWindow(active=True, detail=_mid_deploy_detail(name, state, exclusion))
    return None


def _read_deploy_states() -> dict[str, HostDeployState]:
    """machine -> deploy-state row for every machine in the table; {} on failure.

    Best-effort by contract: an unreadable table degrades to "cannot tell"."""
    try:
        return read_all()
    except Exception as exc:
        logger.warning("[deploy-window] reading host_deploy_state failed: {exc!r}", exc=exc)
        return {}


def _lease_hold() -> DeployWindow | None:
    """A live deploy lease — signal 1.

    Never raises: this is consulted by the health probe, which by definition runs
    when the cluster may be broken, and an unreachable DB must not become a
    traceback.
    """
    try:
        from base.deploy.state.cluster_lock import read_update_lease

        lease = read_update_lease()
    except Exception as exc:
        logger.warning("[deploy-window] could not read the deploy lease: {exc!r}", exc=exc)
        return None
    if lease is None:
        return None
    return DeployWindow(active=True, detail=f"a cluster deploy is in progress — {lease.describe()}")


def deploy_in_flight() -> DeployWindow:
    """Whether a deploy or maintenance window is open on this cluster right now.

    The lease first, because it is the only signal that survives a host being
    unreachable; then the per-machine posture rows.

    Never raises: every signal degrades to "cannot tell". The caller is the health
    probe's alert grading, where an exception is worse than a miss.
    """
    if (signal := _lease_hold()) is not None:
        return signal
    if (signal := _remote_orchestration()) is not None:
        return signal
    return _IDLE
