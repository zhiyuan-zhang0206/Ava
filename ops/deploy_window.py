"""Is a deploy happening right now — the one answer every actor that can move the
cluster pin consults, and the one place that ends a settle hold when it stops being
true.

The cluster has **more than one actor able to move the pin**: a human or agent
running `ava cluster update`, the OS-scheduled health probe's `--auto-rollback` (which moves
it *backwards*), and the watchdog's pin / code controllers. On 2026-07-29 the first
two collided — a second `ava cluster update` started while a rollout was still converging,
advanced the pin to an unreviewed commit, and quiesced into the first rollout's
window, force-terminating two agents that had done nothing wrong.

The mutual exclusion was never actually missing. `cluster_update_lock` existed and
every automated healer already read it. **The defect was that its protected window
was shorter than the dangerous one**: Phase B polled each agent-runner back for at
most a POSIX-era 120 s, and a host that outran that was written off while its
checkout had moved and its processes had not. The orchestration returns, the
`finally` releases, and the cluster is open in exactly the state a second deploy must
not start into. Two things close that: `settle_update_lock` keeps the lease held
across the window, and `shared.deploy_timing` removes the mismatch that opened it —
one no-progress definition shared by the poll, the settle TTL and the host-local
stall reaper, plus a lease renewed while the orchestration runs so its TTL is no
longer a budget the rollout has to fit inside.

## The two signals, and why the polarity differs between them

1. **A live lease** (`shared.cluster_lock.read_update_lease`) — the floor. The
   gateway holds it, so it stays true even while the transitioning host is
   unreachable, which is precisely when the danger is highest. Everything else here
   is secondary to it.
2. **Any machine's `host_deploy_state` posture row** (R1, Task #1021) —
   covers a deploy that takes **no lease at all**: the watchdog's pin and code
   controllers spawn a host-local `ava-updater` without going near the gateway's
   orchestration. The code controller added on 2026-07-28 means that path gets
   *more* traffic, not less. Read from the table rather than by probing each
   machine: the old probe's `current_orchestration` field was a session-name
   judgment that died with the very daemon that answered it. This signal covers
   the orchestrator's own host too — Phase A pauses it like every agent-runner —
   which is why the old "this host's own orchestration session" leg was dropped
   in the old-signal sweep (PR5): a lease-less local updater writes `converging`,
   and a rollout holds the lease before its own pause lands.

**Signal 2's original blind spot is closed by reading the posture table rather
than the probe** (R1, Task #1021): `ops` is itself a ServiceSpec, so a runner's
self-update stops the very daemon that used to answer this probe. Through the
whole stop -> start window — checkout moved, processes swapping, the longest
stretch on a Windows host — the old probe read "not deploying" exactly when the
danger was highest; the posture row is written by the pause fan-out and the
updater's lease, both outside the restarted services, and survives the window.
The convergence check below still probes (it needs `head_sha`/`running_sha`,
which the row does not carry), so the permissive/conservative polarity split
between the two questions remains. Signal 1 stays the floor and this an
addition to it, never a substitute.

## Releasing the hold when it stops being warranted

A settle hold that only expires on a timer is *a state that outlives the condition
it represents* — the bug class of the whole 2026-07-28/29 night (the stale
`stopped_at` latch, the orphaned launchd probe still armed at prod,
`pin_heal_attempt`). So a settle hold is re-examined every time this question is
asked: once every agent-runner has converged onto the pin (checkout AND running
process), the hold is released immediately rather than idling out `SETTLE_TTL_S`.

The convergence check reads the **same** per-host probe as signal 2 — and reads it
in the opposite direction, deliberately. Deciding whether to *refuse*, an
unreachable host is "not deploying" (permissive; the blind spot above). Deciding
whether to *release a hold*, an unreachable host is "not converged" (conservative;
it keeps the hold). Each question takes the reading that fails safe for that
question, which is what makes the same weak evidence usable for both.

## Displaying a hold is not asking this question

`ava cluster status` shows the hold (a banner + the `hold` column beside `pin` /
`code`) so a refused operator can see it from the roster instead of the cron log.
That display reads the lease row directly (`read_update_lease` + `settle_hosts`) and
must keep doing so — calling `deploy_in_flight()` from a status GET would probe every
machine and, on a converged cluster, *release* the hold as a side effect of someone
looking at it. It also means the roster shows signal 1 only, which is why the column
is labelled as the hold's recorded waiting set and never as a convergence verdict:
the permissive and conservative readings above both belong to callers deciding
something, not to a table.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from shared.host_deploy_state import POSTURE_IDLE, HostDeployState, read_all
from shared.log import logger

# One short probe per machine, in parallel. This runs on the rollout-refusal path
# and the health probe's suppression path, neither latency-sensitive; an absent host
# times out and is read per the polarity rule above.
_PROBE_TIMEOUT_S = 5.0


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


async def _probe_machines(machines: list[tuple[str, str | None]]) -> dict[str, dict[str, object]]:
    """`status_probe` every machine in parallel; name -> result, omitting hosts that
    did not answer. One dial serves both questions asked of it below."""
    from ops import cluster_rpc as cr

    async def _one(name: str, url: str | None) -> tuple[str, dict[str, object] | None]:
        try:
            result = await cr.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=_PROBE_TIMEOUT_S,
                ops_url=url,
                # No transport retry for the deploy-window probe: it is a
                # best-effort auxiliary signal (a failed probe degrades to
                # 'no deploy in flight' for that host), not a load-bearing op.
                retries=0,
            )
        except Exception:
            return name, None
        return name, result if isinstance(result, dict) else None

    return {
        name: result
        for name, result in await asyncio.gather(*[_one(n, u) for n, u in machines])
        if result is not None
    }


def _machines() -> list[tuple[str, str | None]]:
    """Every registered machine, or an empty list when the table cannot be read."""
    try:
        import shared.machines

        return shared.machines.list_all()
    except Exception as exc:
        logger.warning("[deploy-window] could not list machines: {exc!r}", exc=exc)
        return []


def _remote_orchestration() -> DeployWindow | None:
    """Any machine mid-deploy — signal 2, read from the host_deploy_state
    table instead of probing each machine's ops server (R1, Task #1021).

    The old probe's `current_orchestration` field was a session-name judgment
    that died with the very daemon that answered it — `ops` stops mid
    self-update, so the middle of the window read "not deploying" (the blind
    spot in the module docstring). The posture row is written by the pause
    fan-out and the updater's lease, both outside the restarted services, so
    it survives the whole window; a machine with no row has never transitioned
    and reads as idle. A stale `converging` row (updater crashed) keeps the
    signal active until the stranded-pause recovery unpauses the host — the
    conservative direction, since its checkout may have moved. Never raises.
    """
    machines = _machines()
    if not machines:
        return None
    states = _read_deploy_states()
    for name, _url in machines:
        state = states.get(name)
        if state is not None and state.posture != POSTURE_IDLE:
            return DeployWindow(
                active=True,
                detail=(
                    f"machine {name!r} is mid-deploy (host_deploy_state.posture={state.posture})"
                ),
            )
    return None


def _read_deploy_states() -> dict[str, HostDeployState]:
    """machine -> deploy-state row for every machine in the table; {} on failure.

    Best-effort by contract — signal 2 is an addition to the lease floor, never
    a substitute, so an unreadable table degrades to "cannot tell" like the
    probe it replaced."""
    try:
        return read_all()
    except Exception as exc:
        logger.warning("[deploy-window] reading host_deploy_state failed: {exc!r}", exc=exc)
        return {}


def settle_hosts_converged(hosts: list[str]) -> bool:
    """Whether every host a settle hold is waiting for has reached the pin — checkout
    AND running process — so the hold is no longer warranted.

    **The population is the hold's own, not the machine table.** The hold is taken
    over the hosts that *acked* Phase B and had not come back when the poll ended —
    whether still converging or provably stalled
    (`cli/commands/update.py:_still_converging`) — and the release re-probes exactly
    those, read back from the lease's note. Asking a wider question is not a
    conservative choice, it is a broken one: `shared.machines.list_all()` is every row
    that ever registered — no `stopped_at`, capability or liveness filter — so one
    intentionally-stopped host, one decommissioned row, or one gateway-only unit (which
    runs no `ops` daemon at all and so can never answer a `status_probe`) would pin
    this to False for the lifetime of the cluster and the hold would only ever expire
    on its timer. That is the timer behaviour this function exists to remove, and it
    is the same population confusion `_still_converging`'s acked-only rule was written
    to avoid — re-imported through the other side.

    False whenever convergence cannot be *proven*: an unreadable pin, a host that does
    not answer, or a host that has vanished from the machine table since the hold was
    taken. **A host that does not answer is "still converging", never "converged"** —
    the opposite of the refusal path's reading of the same silence, and deliberately
    so: a host is typically unreachable *because* its `ops` daemon is mid-restart, and
    releasing on that is precisely the blind spot the lease was introduced to cover.
    Inheriting the permissive reading here would rebuild it inside the fix, where it
    would be much harder to find because the mechanism is believed correct.

    `running_sha` is what makes this more than the roster's pin column: a host whose
    checkout landed but whose processes were never replaced reads on-pin and is still
    mid-transition. Bounded caveat: `running_sha` speaks for the process that *answers*
    the probe — the ops daemon — so "converged" here means that daemon is on the pin,
    not that every process on the host is. A sibling daemon respawned at a different
    commit is the `code` controller's dimension, not this one.
    """
    if not hosts:
        return False  # an unparseable / absent note is not evidence of convergence
    try:
        from shared.cluster_pin import get_cluster_target_sha

        pin = get_cluster_target_sha()
    except Exception as exc:
        logger.warning("[deploy-window] could not read the cluster pin: {exc!r}", exc=exc)
        return False
    if pin is None:
        return False
    # `list_all()` is used here only as a name -> dial-URL lookup for the held hosts,
    # never as the population itself.
    urls = dict(_machines())
    held = [(name, urls.get(name)) for name in hosts]
    if any(name not in urls for name, _u in held):
        return False  # a held host is no longer registered: cannot prove it converged
    try:
        probed = asyncio.run(_probe_machines(held))
    except Exception as exc:
        logger.warning("[deploy-window] probing settle hosts failed: {exc!r}", exc=exc)
        return False
    for name in hosts:
        result = probed.get(name)
        if result is None:
            return False  # silence is not convergence
        if result.get("head_sha") != pin or result.get("running_sha") != pin:
            return False
    return True


def _lease_hold() -> DeployWindow | None:
    """A live deploy lease — signal 1, the floor.

    A **settle hold** (a lease carrying a note, kept after the orchestration stopped
    executing) is re-examined here rather than simply honoured: if the cluster has
    since converged, it is released now instead of idling out its TTL. That is the
    difference between a hold that represents a condition and one that merely
    outlives it.

    Never raises: this is consulted by the health probe, which by definition runs
    when the cluster may be broken, and an unreachable DB must not become a
    traceback on the rollback path.
    """
    try:
        from shared.cluster_lock import read_update_lease

        lease = read_update_lease()
    except Exception as exc:
        logger.warning("[deploy-window] could not read the deploy lease: {exc!r}", exc=exc)
        return None
    if lease is None:
        return None
    if lease.note is None:
        # An orchestration is executing right now — nothing to re-examine.
        return DeployWindow(
            active=True, detail=f"a cluster deploy is in progress — {lease.describe()}"
        )
    from shared.cluster_lock import settle_hosts

    if settle_hosts_converged(settle_hosts(lease.note)):
        from shared.cluster_lock import release_settle_hold

        if release_settle_hold(lease.holder):
            logger.info(
                "[deploy-window] settle hold released early: every agent-runner is on the pin "
                "(was {holder}, {note})",
                holder=lease.holder,
                note=lease.note,
            )
            # The settle phase's one telemetry record (C3, task #2189): the hold
            # started in the orchestration process that has already exited and
            # ends HERE, so this is the only place its duration can be printed.
            # Server-side elapsed (`settle_elapsed_s`) — no cross-host clock
            # skew — and the held host set read back from the note just released.
            from shared.rollout_telemetry import settle_ended

            settle_ended(dur_s=lease.settle_elapsed_s, hosts=settle_hosts(lease.note))
            return None
        # Another actor moved the lease between the read and the release; whatever it
        # holds now is not ours to reason about, so report the hold we saw.
    return DeployWindow(
        active=True, detail=f"a cluster deploy is still settling — {lease.describe()}"
    )


def deploy_in_flight(*, include_remote: bool = True) -> DeployWindow:
    """Whether a deploy owns this cluster right now.

    Order is the confidence order, not the cost order: the lease first, because it
    is the only signal that survives a host being unreachable; then the per-machine
    posture probe that catches a lease-less, host-local update (including one on
    this very host — Phase A pauses the orchestrator like every agent-runner).

    `include_remote=False` skips that last leg for a caller that cannot afford the
    round-trips.

    Never raises: every signal degrades to "cannot tell". Both callers are
    refusal/suppression paths where an exception is worse than a miss — a spurious
    traceback blocks a deploy nobody can then start, or breaks the very auto-rollback
    that exists to catch a bad release.
    """
    signal = _lease_hold()
    if signal is not None:
        return signal
    if include_remote and (signal := _remote_orchestration()) is not None:
        return signal
    return _IDLE
