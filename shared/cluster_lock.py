"""Cluster-wide "a deploy owns this cluster" lease.

A single row in `deployment_state` (central DB) that the gateway update
orchestration takes before Phase A and releases at the end, so two update rollouts
cannot run concurrently — the 2026-06-01 collision, where a manual rollout raced the
cluster's own `ava.self.update` and both advanced the central schema. The
session-name guard in `ops/cluster_deploy.py` is a TOCTOU-prone first line; this DB
compare-and-set is the authoritative one. The row IS the R1 deployment-state row:
besides the lease it carries `phase` (stable/updating/settling) and `kind`
(rollout/restart/update), the explicit model that replaces the implicit conjunction
of flag files, session names and log mtimes (okf/design/r1-state-liveness).
This module owns the cluster-level transitions; host-level state lives in
`host_deploy_state`, agents in `agents_meta` leases.

**This row is the cluster's single "intentionally mid-transition" signal, not just a
mutex.** Every automated healer already reads it before acting — the pin controller,
the code controller, the schema controller, the stranded-pause controller, `ava
status`, and `ava stop`'s unpause discriminator all ask "is a deploy running?" of this
row and defer when one is. That is deliberate and is why there is no second
"deploy in progress" flag: a second source of truth for the same fact is free to
disagree with this one, which is the whole bug class this row exists to close.

What differs between them is how much of the row they need. `update_lock_holder()` is
the bare "is it held", enough wherever any hold means stand back (stranded-pause, `ava
status`). `read_update_lease()` returns the row itself, for consumers that must
*explain* the hold to a human (a refused `ava cluster update`, the health probe's cron log) or
must tell an executing rollout apart from a settle hold before branching — the pin and
code controllers, through `DeployLease.awaits` below. Neither is a second source of
truth: they are two reads of this one row.

The lease carries a TTL (`expires_at`) so a *crashed* holder is reclaimable rather
than a permanent deadlock. **The TTL is not what makes it long enough** — a lease
whose length has to be audited against the sum of every phase's timeout is one
slower host away from expiring mid-operation. The process running the orchestration
re-arms its own lease on a timer instead (`renew_update_lock`, driven from the
Phase-B poll), so `LOCK_TTL_S` bounds only how long a *crashed* holder blocks the
next rollout, and a genuinely hung holder still releases because renewal dies with
the process. See `shared.deploy_timing` for the family this belongs to.

A connection-scoped Postgres advisory lock would auto-release on disconnect but
cannot model a lease that must outlive the many short connections a multi-phase,
multi-process rollout opens — the TTL'd row can.

**Settle holds.** A rollout is not finished when the gateway's orchestration
returns. Phase B polls each agent-runner back to `paused=false` for a bounded time;
a host whose checkout + `uv sync` + restart outruns that is left mid-transition and
the orchestration exits — while that host is still swapping its processes. Releasing
there is what let a second `ava cluster update` start into a half-transitioned cluster on
2026-07-29, force-terminating two agents that had done nothing wrong. So an
orchestration that ends with hosts still converging calls `settle_update_lock`
instead of releasing: the lease stays held, on its own TTL, with `note` recording who
it is waiting for. See `SETTLE_TTL_S`.

**A settle hold is not the same instruction to a healer as a running rollout, and the
`note` is what tells them apart.** "A deploy is mutating the cluster" means stand
back; "this hold is waiting for host X to converge" means X's own convergence is the
remaining work. A healer on X that reads only "is it held" defers to a hold whose
entire content is that it is waiting for the thing the healer would do — the mutual
wait of issue #1020, which cost ~16 minutes of a mixed-code host and would not clear
at all under a renewed window. `DeployLease.awaits` is that discrimination, and it is
the *only* one: nothing about a settle hold's refusal of a second `ava cluster update`
changes, and no host but the named one is permitted anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import shared.db
from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.log import logger

# Crash-reclaim bound and nothing else: how long a holder that *died* blocks the
# next rollout. A live orchestration outlives it by renewing (`renew_update_lock`),
# so this deliberately does NOT have to exceed the longest possible rollout — that
# coupling is what made a slower fleet expire its own protection.
LOCK_TTL_S = 1800.0

# How long the lease is held after an orchestration exits with agent-runners still
# converging (see the module docstring). The family's whole-run no-progress
# definition: the bound after which the host the hold waits for can no longer be
# "slow rather than stopped" over a full leg. The host-local reaper may end the
# hold earlier than that — its per-stage clock (`STAGE_NO_PROGRESS_TIMEOUT_S`)
# kills an updater stuck inside one stage well before this lapses, and the
# convergence path releases the hold on the news — so this is the outer bound for
# when nothing on the host ends the hold. Nobody is executing during a settle
# hold — it is a stated waiting period, and `ava cluster recover` breaks it early
# once an operator has looked.
SETTLE_TTL_S = NO_PROGRESS_TIMEOUT_S


# A settle hold's `note` is both the sentence a human reads and the machine-readable
# record of WHICH hosts the hold is waiting for. The release path has to re-probe
# exactly that set — the same acked population the hold was taken over — so the
# format is an owned contract with one builder and one parser rather than free text
# a later edit could reword. `settle_hosts` returning [] (absent / reworded note) is
# read by the caller as "cannot tell", which falls back to the TTL rather than
# releasing: a note we cannot parse must never look like convergence.
_SETTLE_NOTE_PREFIX = "settling, waiting for: "


def settle_note(hosts: list[str]) -> str:
    """The `note` a settle hold carries, naming the hosts it waits for."""
    return _SETTLE_NOTE_PREFIX + ", ".join(sorted(hosts))


def settle_hosts(note: str | None) -> list[str]:
    """The hosts a settle note names; empty when the note is absent or not one."""
    if note is None or not note.startswith(_SETTLE_NOTE_PREFIX):
        return []
    body = note[len(_SETTLE_NOTE_PREFIX) :].strip()
    return [h for h in (part.strip() for part in body.split(",")) if h]


@dataclass(frozen=True)
class DeployLease:
    """A *live* hold on the cluster, as the DB sees it.

    Durations are computed server-side (`now()` in the same statement that reads the
    row) rather than against the caller's clock: the consumers are spread across
    machines — a refused `ava cluster update` on one host, the health probe on another — and
    a cross-host clock skew must never turn into a wrong "expires in" or a wrong
    suppression window.
    """

    holder: str
    held_for_s: float
    expires_in_s: float
    # Why it is held, when the holder wanted to say more than its name — currently
    # set only by `settle_update_lock`. None for an ordinary in-flight rollout.
    note: str | None
    # What kind of orchestration holds the lease (rollout/restart/update), the
    # explicit replacement for session-name probing. None for a kind-less holder
    # (a rollback) and for legacy rows.
    kind: Literal["rollout", "restart", "update"] | None = None
    # Exact DB identity used only by operator recovery's compare-and-set claim.
    # Optional keeps legacy/test-constructed snapshots readable; a real DB read
    # always supplies it.
    acquired_at: datetime | None = None
    # When the settle hold started (server-side now() at settle_update_lock), and
    # how long it has already been held, computed server-side like `held_for_s` so
    # cross-host clock skew never distorts the number (C3, task #2189). Both None
    # on a non-settle lease, and on a settle hold that predates the column — the
    # duration of those is unknowable and is reported as such, never guessed.
    settle_started_at: datetime | None = None
    settle_elapsed_s: float | None = None

    def awaits(self, machine: str) -> bool:
        """True when this lease is a **settle hold whose recorded waiting set names
        `machine`** — that is, the hold exists *because* this host has not converged.

        The distinction a healer needs and could not previously make. Deferring to a
        lease is right while a rollout is *executing*: mid-flight, "checkout ahead of
        the processes" is the deploy's own transient and healing it would fight Phase
        B. It is self-defeating during a settle hold naming this host, because nothing
        is executing (`SETTLE_TTL_S`'s docstring: "Nobody is executing during a settle
        hold — it is a stated waiting period") and the convergence the hold waits for
        is exactly what the healer would produce. A host in that position converged on
        the 16-minute TTL and no sooner (issue #1020).

        Permitting is deliberately narrow: it says nothing about *other* hosts' heals,
        it does not release the hold, and it does not weaken the refusal a second
        `ava cluster update` gets. It also answers only the question the *lease* can answer.
        A caller that consults a second signal — `ops.cluster.current_orchestration`,
        which sees the watchdog-spawned updater that takes no lease — must still
        consult it: a True here is not a verdict that nothing is running on this host. `note is None` — a lease an orchestration is executing under
        — is never permitted, which is the same line `release_settle_hold` and
        `renew_update_lock` are already scoped on. An absent or unparseable note yields
        an empty host set and therefore a deferral, matching `settle_hosts`' rule that
        a note we cannot parse must never be read as permission.
        """
        return machine in settle_hosts(self.note)

    def describe(self) -> str:
        """One line naming the holder, how long it has been held, and when it lapses
        — the "a deploy started by X at T is in progress" a refused second operator
        must see instead of discovering the conflict from the wreckage."""
        detail = f" — {self.note}" if self.note else ""
        return (
            f"{self.holder} (held {self.held_for_s / 60:.0f}m, "
            f"lease expires in {self.expires_in_s / 60:.0f}m){detail}"
        )

    def refusal(self, action: str) -> str:
        """Why `action` is refused, and every way out — the message a second operator
        must be handed at the moment they try, rather than left to reconstruct from
        two force-terminated agents afterwards.

        Always names the break, because a lease nobody can break is its own outage:
        `ava cluster recover` clears a hold whose holder process is provably gone
        (it probes the pid), and the TTL clears one whose holder was on another
        machine and cannot be probed from here.
        """
        return (
            f"{action} refused: a cluster deploy is in progress — {self.describe()}. "
            f"Wait for it to finish, or `ava cluster recover` to break the hold once "
            f"you have confirmed that deploy is dead (it refuses while the holder is "
            f"still alive). The lease lapses on its own in "
            f"{self.expires_in_s / 60:.0f}m."
        )


def holder_pid_if_local(holder: str) -> int | None:
    """The live local pid `holder` names, or None.

    Parses the `<machine>:pid<N>` format `self_holder` builds. Refusals are the
    point: a holder naming another machine must answer None (its pid is
    meaningless in this host's namespace, and signalling it would hit an
    unrelated local process), as must an unparseable string and a pid that is not
    alive. Used by the stalled-rollout controller's interrupt and by the formal
    cancel op (`ops.ops_cluster.cluster_cancel_op`), which ask "may I signal this
    pid" — the opposite question from `ops.ops_cluster._lock_holder_is_live`,
    which answers "no" on every doubt.
    """
    from shared.machine import machine_name
    from shared.proc import process_alive

    machine, sep, pid_str = holder.partition(":pid")
    if sep == "":
        return None
    try:
        if machine != machine_name():
            return None
    except Exception:
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    return pid if process_alive(pid) else None


def self_holder() -> str:
    """This process's holder string, `<machine>:pid<N>`.

    One builder, because the format is *parsed* elsewhere:
    `ops.ops_cluster._lock_holder_is_live` splits it to probe whether the owner
    process is still alive, which is what lets `ava cluster recover` break a hold
    whose owner is provably gone. It is also how a renewal re-finds its own lease
    without the holder having to be threaded through four call frames — the
    orchestration and its Phase-B poll are the same process, so the string is
    derivable rather than passed.
    """
    from shared.machine import machine_name

    return f"{machine_name()}:pid{os.getpid()}"


def acquire_update_lock(
    holder: str,
    *,
    kind: Literal["rollout", "restart", "update"] | None = None,
    ttl_s: float = LOCK_TTL_S,
) -> bool:
    """Take the single cluster update lock for `holder`. Returns True if acquired
    (the row was free, or a previous holder's TTL had expired), False if a *live*
    holder still holds it. The conditional UPDATE is the atomic compare-and-set.

    `kind` names what kind of orchestration is starting (rollout / restart /
    update) — the explicit-model replacement for session-name probing. The
    gateway orchestration passes it (`_run_gateway_orchestration`); a rollback
    takes the lease without a kind (the enumeration has no rollback value and the
    hold still reads as "a deploy is in progress" to every consumer). The row
    enters `phase='updating'` on acquire.

    **`note = NULL` here is load-bearing, not hygiene.** `release_settle_hold` is
    scoped `note IS NOT NULL` precisely so it can only ever clear a *settle* hold and
    never a lease an orchestration is executing under. An empty note is therefore
    what marks "a rollout is running"; giving a run lease a note for diagnostics
    would silently make live rollouts eligible for convergence-release. If a run
    lease ever needs to say something, it needs a different column, not this one.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state "
            "SET holder = %s, acquired_at = now(), expires_at = now() + make_interval(secs => %s), "
            "    note = NULL, settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'updating', kind = %s "
            "WHERE id = 1 AND (holder IS NULL OR expires_at < now())",
            (holder, ttl_s, kind),
        )
        acquired = cur.rowcount == 1
        # Both outcomes logged: a refused acquire is the start of every
        # "why is my update not running" question, and the acquire is the
        # opening bracket of the whole orchestration.
        if acquired:
            logger.info(
                "[cluster-lock] acquired by {holder} (ttl {ttl:.0f}s)", holder=holder, ttl=ttl_s
            )
        else:
            logger.warning(
                "[cluster-lock] acquire by {holder} REFUSED — a live holder exists", holder=holder
            )
        return acquired


def renew_update_lock(holder: str, *, ttl_s: float = LOCK_TTL_S) -> bool:
    """Re-arm `holder`'s lease for another `ttl_s`. Returns True if it landed.

    **This is what makes the lease outlast the operation instead of the reverse.**
    Without it, "how long a deploy is allowed to take" is `LOCK_TTL_S` minus the sum
    of every phase's timeout — a quantity nobody re-audits when a slower host joins
    the fleet, and the 2026-07-29 incident is what it looks like when that sum is
    wrong. With it, the lease is a function of the orchestration still *running*: the
    process renews while it works, and stops renewing the instant it dies, so a
    crashed rollout still releases within one TTL. A hung-but-alive rollout keeps
    renewing on purpose — that is a real hold on a real half-transitioned cluster,
    and `ava cluster recover` (which probes the holder pid) is its escape hatch.

    Scoped exactly like `release_update_lock` plus one more guard:

    - `holder = %s` — a lease already reclaimed past its TTL by a new owner must not
      be re-armed under the old holder's name.
    - `note IS NULL` — renewal may only extend a lease an orchestration is
      *executing* under, never re-arm a **settle hold**. A settle hold is a stated
      waiting period whose whole value is that it ends (`SETTLE_TTL_S`, or early on
      convergence); a stray renewal would turn it into an unbounded one.

    A renewal that finds the row already lapsed still re-arms it — leaving a live
    rollout unprotected is worse — but says so at WARNING, because a lapse means the
    invariant was already broken and re-arming it silently would hide that.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        # MATERIALIZED + FOR UPDATE for the same reason as force_release_update_lock:
        # the pre-UPDATE `expires_at` has to be read under a row lock, or an inlined
        # CTE would re-read it after the UPDATE and never report a lapse.
        cur.execute(
            "WITH prev AS MATERIALIZED "
            "(SELECT expires_at < now() AS lapsed FROM deployment_state "
            " WHERE id = 1 FOR UPDATE), "
            "renewed AS "
            "(UPDATE deployment_state "
            " SET expires_at = now() + make_interval(secs => %s) "
            " WHERE id = 1 AND holder = %s AND note IS NULL "
            " RETURNING 1) "
            "SELECT (SELECT count(*) FROM renewed), (SELECT lapsed FROM prev)",
            (ttl_s, holder),
        )
        row = cur.fetchone()
        renewed = row is not None and row[0] == 1
        lapsed = row is not None and bool(row[1])
    if not renewed:
        logger.warning(
            "[cluster-lock] renewal by {holder} did not land — the lease is not theirs "
            "(reclaimed past TTL, or already converted to a settle hold)",
            holder=holder,
        )
    elif lapsed:
        logger.warning(
            "[cluster-lock] renewed {holder}'s lease AFTER it had already lapsed — the "
            "cluster was briefly unguarded mid-deploy; a renewal round was missed",
            holder=holder,
        )
    return renewed


def release_update_lock(holder: str) -> None:
    """Release the lock iff `holder` still holds it — a no-op when another holder
    has since reclaimed it past a TTL expiry, so a slow release never clobbers a
    newer owner's lock.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder = NULL, acquired_at = NULL, expires_at = NULL, "
            "    note = NULL, settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'stable', kind = NULL "
            "WHERE id = 1 AND holder = %s",
            (holder,),
        )
        if cur.rowcount == 1:
            logger.info("[cluster-lock] released by {holder}", holder=holder)
        else:
            # We did not hold it at release time — another orchestration reclaimed it
            # past the TTL. That means this rollout outran LOCK_TTL_S (or two ran at
            # once); surface it rather than swallow, since it signals the serialization
            # was breached.
            logger.warning(
                "update lock not held by {holder} at release (reclaimed past TTL — "
                "a rollout outran LOCK_TTL_S, or two ran concurrently)",
                holder=holder,
            )


def force_release_update_lock() -> str | None:
    """Clear the lock unconditionally, whatever holder/TTL it carries. Returns the
    holder that was cleared (None if it was already free).

    For operator-driven stranded-cluster recovery only: a hard-killed rollout can
    leave the lock held until its TTL expires (up to LOCK_TTL_S), blocking the next
    rollout's acquire. Unlike `release_update_lock`, this ignores the holder — the
    caller (`cluster_recover_op`) has already established that no orchestration is
    actually live (no rollout/restart/updater session), so the held row is stale.
    """
    # Single statement so capture-and-clear is atomic: the `prev` CTE takes a
    # row lock (FOR UPDATE) before the UPDATE, so a concurrent acquire_update_lock
    # serializes behind it instead of slipping a new holder in between a separate
    # SELECT and UPDATE (which would be silently cleared, returning a stale None).
    # MATERIALIZED is load-bearing: a plain CTE inlines (PG12+) and the RETURNING
    # subquery would then re-read the row *after* the UPDATE (NULL); materializing
    # pins the pre-UPDATE holder.
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH prev AS MATERIALIZED "
            "(SELECT holder FROM deployment_state WHERE id = 1 FOR UPDATE), "
            "cleared AS "
            "(UPDATE deployment_state SET holder = NULL, acquired_at = NULL, expires_at = NULL, "
            "    note = NULL, settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'stable', kind = NULL "
            "WHERE id = 1) "
            "SELECT holder FROM prev"
        )
        row = cur.fetchone()
        return row[0] if row is not None else None


@dataclass(frozen=True)
class RecoveryClaim:
    """Result of atomically replacing the exact lease recovery inspected."""

    acquired: bool
    previous_holder: str | None = None


def claim_recovery_lock(
    recovery_holder: str,
    observed: DeployLease | None,
    *,
    ttl_s: float = 60.0,
) -> RecoveryClaim:
    """CAS-claim the deploy row for one short recovery critical section.

    ``observed=None`` may claim only a still-free/expired row. A dead live
    lease may be replaced only while both its holder and ``acquired_at`` still
    match the snapshot whose process liveness the caller proved. Therefore a
    new rollout that lands after the proof wins the race and recovery refuses;
    it is never unconditionally deleted by a stale observation from another
    machine.
    """
    observed_holder = observed.holder if observed is not None else None
    observed_acquired_at = observed.acquired_at if observed is not None else None
    if observed is not None and observed_acquired_at is None:
        logger.warning(
            "[cluster-lock] recovery claim refused: observed lease lacks acquired_at identity"
        )
        return RecoveryClaim(acquired=False)
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH prev AS MATERIALIZED ("
            "  SELECT holder FROM deployment_state WHERE id = 1 AND ("
            "    (%s::text IS NULL AND (holder IS NULL OR expires_at < now())) OR "
            "    (%s::text IS NOT NULL AND holder = %s AND acquired_at = %s)"
            "  ) FOR UPDATE"
            "), claimed AS ("
            "  UPDATE deployment_state SET holder = %s, acquired_at = now(), "
            "    expires_at = now() + make_interval(secs => %s), note = NULL, "
            "    settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, phase = 'updating', kind = NULL "
            "  WHERE id = 1 AND EXISTS (SELECT 1 FROM prev) RETURNING 1"
            ") SELECT (SELECT count(*) FROM claimed), (SELECT holder FROM prev)",
            (
                observed_holder,
                observed_holder,
                observed_holder,
                observed_acquired_at,
                recovery_holder,
                ttl_s,
            ),
        )
        row = cur.fetchone()
    acquired = row is not None and row[0] == 1
    previous_holder = row[1] if acquired and row is not None else None
    if acquired:
        logger.warning(
            "[cluster-lock] recovery claimed by {holder}; replaced {previous}",
            holder=recovery_holder,
            previous=previous_holder,
        )
    else:
        logger.warning(
            "[cluster-lock] recovery claim by {holder} refused: inspected lease changed",
            holder=recovery_holder,
        )
    return RecoveryClaim(acquired=acquired, previous_holder=previous_holder)


def settle_update_lock(holder: str, *, hosts: list[str], ttl_s: float = SETTLE_TTL_S) -> bool:
    """Keep the lease held after `holder` has stopped executing, for a bounded settle
    window, because the cluster is still converging. Returns True if the hold landed.

    Called instead of `release_update_lock` when a rollout's Phase B poll gave up on
    a host that had *acked* its self-update — that host's checkout has moved and its
    processes have not, which is the exact state a second deploy must not start into
    (the 2026-07-29 incident). A host that never acked is not covered and must not be:
    it never began transitioning, so a permanently-offline agent-runner cannot hold
    the cluster hostage on every rollout.

    Holder-scoped like `release_update_lock`, and for the same reason: if the lease
    was already reclaimed past its TTL, the new owner's hold must not be shortened to
    a settle window by a straggler.

    `holder` is left **exactly** as it was, never decorated with the reason — the
    dead-holder probe in `ops.ops_cluster._lock_holder_is_live` parses it as
    `<machine>:pid<N>`, and a decorated holder would fail that parse and be read as
    live, which would make `ava cluster recover` refuse to break a hold whose owner
    is definitively gone. The reason goes in `note`, which is what `describe()`
    renders.
    """
    note = settle_note(hosts)
    sorted_hosts = sorted(hosts)  # one order in all three renderings
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        # The settle fact lands three ways: the structured `settle_hosts` array
        # (the new truth), `settle_note` (human-readable), and the legacy `note`
        # column every current reader parses. All three are written together and
        # the legacy one retires with the old-signal sweep; until then `note`
        # staying non-NULL is what keeps `renew`/`release_settle_hold` scoped.
        cur.execute(
            "UPDATE deployment_state "
            "SET expires_at = now() + make_interval(secs => %s), note = %s, "
            "    settle_note = %s, settle_hosts = %s, settle_started_at = now(), phase = 'settling' "
            "WHERE id = 1 AND holder = %s",
            (ttl_s, note, note, sorted_hosts, holder),
        )
        held = cur.rowcount == 1
        if held:
            logger.warning(
                "[cluster-lock] HELD past {holder}'s exit for a {ttl:.0f}s settle window: {note}",
                holder=holder,
                ttl=ttl_s,
                note=note,
            )
        else:
            logger.warning(
                "settle hold by {holder} did not land — the lease is no longer theirs "
                "(reclaimed past TTL). The cluster is unguarded while it settles.",
                holder=holder,
            )
        return held


def release_settle_hold(holder: str) -> bool:
    """End a **settle hold** early because the cluster has finished converging.
    Returns True if this call is what released it.

    Scoped two ways, and both matter. `holder = %s` is the same guard
    `release_update_lock` uses: a hold reclaimed past its TTL by a new owner must not
    be cleared by a straggler. `note IS NOT NULL` is the stronger one — it releases
    only a *settle* hold, never a lease an orchestration is actively executing under.
    Without it a convergence check that ran at the wrong moment could unlock a live
    rollout, which is the failure this whole module exists to prevent.

    Why early release exists at all: a hold that can only expire on a timer is a
    state that outlives the condition it represents, and a settle hold whose hosts
    converged in the first thirty seconds would otherwise block the next deploy —
    and suppress auto-rollback — for the rest of `SETTLE_TTL_S`. The caller
    (`ops.deploy_window`) establishes convergence; this is only the write.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder = NULL, acquired_at = NULL, "
            "    expires_at = NULL, note = NULL, settle_hosts = NULL, settle_note = NULL, settle_started_at = NULL, "
            "    phase = 'stable', kind = NULL "
            "WHERE id = 1 AND holder = %s AND note IS NOT NULL",
            (holder,),
        )
        released = cur.rowcount == 1
    if released:
        logger.info(
            "[cluster-lock] settle hold by {holder} released early — the cluster converged",
            holder=holder,
        )
    return released


def read_update_lease() -> DeployLease | None:
    """The *live* lease (None if free, or only an expired holder remains).

    The read every consumer that must explain the hold to a human goes through — a
    refused `ava cluster update`, the health probe's decision to treat a down gateway as an
    expected transition rather than an outage. `update_lock_holder()` is the
    name-only form kept for the branch-on-it callers.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT holder, extract(epoch FROM now() - acquired_at), "
            "       extract(epoch FROM expires_at - now()), note, kind, acquired_at, "
            "       settle_started_at, extract(epoch FROM now() - settle_started_at) "
            "FROM deployment_state WHERE id = 1 AND expires_at > now()"
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return DeployLease(
            holder=row[0],
            held_for_s=float(row[1] or 0.0),
            expires_in_s=float(row[2] or 0.0),
            note=row[3],
            kind=row[4],
            acquired_at=row[5],
            settle_started_at=row[6],
            settle_elapsed_s=float(row[7]) if row[7] is not None else None,
        )


def update_lock_holder() -> str | None:
    """The current *live* holder (None if free or only an expired holder remains) —
    for the "another update in progress (held by ...)" diagnostic."""
    lease = read_update_lease()
    return lease.holder if lease is not None else None
