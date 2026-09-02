"""`shared.cluster_lock` — the cluster-wide "a deploy owns this cluster" lease.

Real-DB tests (the lease IS a Postgres compare-and-set; mocking it would test
nothing). Verifies a second live holder is blocked, release frees it, release is
holder-scoped (no clobber), and an expired TTL is reclaimable — the lease serializes
gateway update orchestrations so two can't run at once (the 2026-06-01 collision).

The second half covers the lease as a *readable state* rather than only a gate: the
`DeployLease` read every "a deploy started by X is in progress" refusal is built
from, and the settle hold that keeps the cluster guarded after an orchestration
exits with agent-runners still converging (the 2026-07-29 incident)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from shared.cluster_lock import (
    SETTLE_TTL_S,
    DeployLease,
    acquire_update_lock,
    claim_recovery_lock,
    read_update_lease,
    release_settle_hold,
    release_update_lock,
    renew_update_lock,
    self_holder,
    settle_hosts,
    settle_update_lock,
    update_lock_holder,
)
from shared.config import settings


@pytest.fixture(autouse=True)
def _free_lock(db_conn: psycopg.Connection) -> Iterator[None]:
    """Reset the singleton lock to free before + after each test — deployment_state
    is not in the conftest TRUNCATE list (it is infra, not business data), so this
    module self-manages it the way tests/ava/test_migrations.py reseeds schema_migrations."""

    def _free() -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, expires_at=NULL, "
                "note=NULL, settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
                "phase='stable', kind=NULL "
                "WHERE id=1"
            )
        db_conn.commit()

    _free()
    yield
    _free()


def test_second_live_holder_is_blocked() -> None:
    assert acquire_update_lock("A") is True
    assert acquire_update_lock("B") is False  # A holds it
    assert update_lock_holder() == "A"


def test_read_lease_uses_the_callers_connection(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled snapshot can read the lease without opening or owning another connection."""
    assert acquire_update_lock("A", kind="rollout") is True

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("read_update_lease opened a fresh connection")

    monkeypatch.setattr("shared.db.connect", _fresh_connect)

    lease = read_update_lease(conn=db_conn)

    assert lease is not None
    assert lease.holder == "A"
    assert lease.kind == "rollout"


def test_release_frees_for_next_holder() -> None:
    assert acquire_update_lock("A") is True
    release_update_lock("A")
    assert update_lock_holder() is None
    assert acquire_update_lock("B") is True  # now free


def test_release_is_holder_scoped() -> None:
    """A release by a non-holder is a no-op — so a slow release after a TTL reclaim
    can't clobber the new owner's lock."""
    assert acquire_update_lock("A") is True
    release_update_lock("B")  # B never held it
    assert update_lock_holder() == "A"  # A still holds


def test_expired_lock_is_reclaimable() -> None:
    """A crashed holder's lock (TTL in the past) reports no live holder and is
    reclaimable — no permanent deadlock."""
    assert acquire_update_lock("A", ttl_s=-1.0) is True  # already expired
    assert update_lock_holder() is None  # expired -> not a live holder
    assert acquire_update_lock("B") is True  # reclaimed


def test_recovery_claim_loses_if_a_new_rollout_acquires_after_its_free_read() -> None:
    observed = read_update_lease()
    assert observed is None
    assert acquire_update_lock("winner", kind="rollout") is True

    claim = claim_recovery_lock("recovery", observed)

    assert claim.acquired is False
    assert update_lock_holder() == "winner"


def test_recovery_claim_replaces_only_the_exact_dead_lease_identity() -> None:
    assert acquire_update_lock("dead-holder", kind="rollout") is True
    observed = read_update_lease()
    assert observed is not None and observed.acquired_at is not None

    claim = claim_recovery_lock("recovery", observed)

    assert claim.acquired is True
    assert claim.previous_holder == "dead-holder"
    assert update_lock_holder() == "recovery"


def test_stale_recovery_snapshot_cannot_replace_a_reclaimed_lease() -> None:
    assert acquire_update_lock("old", kind="rollout") is True
    observed = read_update_lease()
    assert observed is not None
    release_update_lock("old")
    assert acquire_update_lock("new", kind="restart") is True

    claim = claim_recovery_lock("recovery", observed)

    assert claim.acquired is False
    assert update_lock_holder() == "new"


# ─── the lease as a readable state, and the settle hold ──────────────────────


def test_lease_describes_a_live_holder() -> None:
    """`read_update_lease` is the richer read the "a deploy started by X is in
    progress" refusal is built from — holder, age, and time to expiry, all measured
    server-side so a cross-host clock skew can't distort them."""
    assert acquire_update_lock("gateway-host:pid81319", ttl_s=1800.0) is True
    lease = read_update_lease()
    assert lease is not None
    assert lease.holder == "gateway-host:pid81319"
    assert 0 <= lease.held_for_s < 60  # just taken
    assert 1700 < lease.expires_in_s <= 1800
    assert lease.note is None  # an ordinary in-flight rollout has nothing extra to say
    assert "gateway-host:pid81319" in lease.describe()


def test_expired_lease_reads_as_free() -> None:
    """Same TTL semantics as update_lock_holder: an expired holder is not a live
    lease, which is what bounds the health probe's suppression window."""
    assert acquire_update_lock("A", ttl_s=-1.0) is True
    assert read_update_lease() is None


def test_settle_hold_keeps_the_lease_past_the_holders_exit() -> None:
    """A rollout that ends with a host still converging keeps the cluster held on a
    shorter TTL instead of releasing — the 2026-07-29 gap a second `ava cluster update` walked
    into. The reason rides in `note`, and the lease stays un-acquirable."""
    assert acquire_update_lock("gateway-host:pid81319", ttl_s=1800.0) is True
    assert settle_update_lock("gateway-host:pid81319", hosts=["wsl"], ttl_s=900.0) is True

    lease = read_update_lease()
    assert lease is not None
    assert lease.holder == "gateway-host:pid81319"  # holder untouched — see below
    # The note is the machine-readable record of WHICH hosts the hold waits for —
    # the release path re-probes exactly that set, so it round-trips rather than
    # being prose.
    assert settle_hosts(lease.note) == ["wsl"]
    assert lease.expires_in_s <= 900  # TTL shortened to the settle window
    assert "wsl" in lease.describe()  # the human still sees which host it waits for
    # The settle start is recorded server-side (C3, task #2189): the hold outlives
    # the orchestration process, so its duration is only computable from this
    # column — the telemetry the release path prints.
    assert lease.settle_started_at is not None
    assert lease.settle_elapsed_s is not None and 0 <= lease.settle_elapsed_s < 900
    assert acquire_update_lock("gateway-host:pid99999") is False  # still guards the cluster


def test_a_settle_hold_is_read_as_awaiting_exactly_the_hosts_it_names() -> None:
    """`DeployLease.awaits` is the discrimination a healer needs and the lease alone
    could not give it: an *executing* rollout means stand back, a settle hold naming
    this host means this host's convergence is the remaining work (issue #1020).

    Scoped three ways, all asserted here — a lease with no note is never permitted
    however the caller asks; a hold names only the hosts it names; and a note nothing
    can parse yields no permission at all, matching `settle_hosts`' rule that an
    unreadable note must never be read as convergence."""
    assert acquire_update_lock("gateway-host:pid81319") is True
    executing = read_update_lease()
    assert executing is not None
    assert executing.note is None
    assert executing.awaits("wsl") is False, "a rollout executing right now permits nobody"

    assert settle_update_lock("gateway-host:pid81319", hosts=["wsl", "laptop-host"]) is True
    hold = read_update_lease()
    assert hold is not None
    assert hold.awaits("laptop-host") is True
    assert hold.awaits("wsl") is True
    assert hold.awaits("win") is False, "the permission does not generalise to other hosts"

    unparseable = DeployLease(
        holder="gateway-host:pid1", held_for_s=1.0, expires_in_s=1.0, note="paused for maintenance"
    )
    assert unparseable.awaits("laptop-host") is False


def test_settle_hold_leaves_the_holder_string_parseable() -> None:
    """The reason must NOT be folded into `holder`: `ops.ops_cluster` parses that
    column as `<machine>:pid<N>` to decide whether the holder process is still alive,
    and a decorated holder would fail the parse and be treated as live — making
    `ava cluster recover` refuse to break a hold whose owner is definitively gone.
    """
    from ops.ops_cluster import _lock_holder_is_live

    assert acquire_update_lock("othermachine:pid4242") is True
    settle_update_lock("othermachine:pid4242", hosts=["wsl"])
    lease = read_update_lease()
    assert lease is not None
    # Still parseable as machine:pidN — a foreign machine is conservatively "live",
    # which is the answer only a successful parse can produce.
    assert _lock_holder_is_live(lease.holder) is True


def test_settle_hold_by_a_non_holder_is_refused() -> None:
    """Holder-scoped like release: a straggler must not shorten a lease that has
    already been reclaimed by a new owner to a settle window."""
    assert acquire_update_lock("A") is True
    assert settle_update_lock("B", hosts=["wsl"]) is False
    lease = read_update_lease()
    assert lease is not None and lease.note is None


def test_acquire_clears_a_previous_settle_note() -> None:
    """A fresh deploy must not inherit the last one's explanation."""
    assert acquire_update_lock("A", ttl_s=-1.0) is True
    settle_update_lock("A", hosts=["wsl"], ttl_s=-1.0)  # expired settle hold
    assert acquire_update_lock("B") is True  # reclaims the lapsed lease
    lease = read_update_lease()
    assert lease is not None and lease.holder == "B" and lease.note is None


def test_release_clears_the_note() -> None:
    assert acquire_update_lock("A") is True
    settle_update_lock("A", hosts=["wsl"])
    release_update_lock("A")
    assert read_update_lease() is None
    assert acquire_update_lock("B") is True
    lease = read_update_lease()
    assert lease is not None and lease.note is None


def test_holder_persists_across_connections() -> None:
    """acquire/holder use independent short connections (as the multi-process rollout
    does) — the lock state is the committed row, visible to a fresh connection."""
    assert acquire_update_lock("A") is True
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT holder FROM deployment_state WHERE id=1 AND expires_at > now()")
        assert cur.fetchone()[0] == "A"  # type: ignore[index]


# ─── renewal: the lease outlives the operation, not the reverse ───────────────


def test_renewal_extends_a_lease_that_would_otherwise_lapse() -> None:
    """The invariant: the lock must not expire before the operation it protects can
    finish. A rollout takes the lease on a TTL and renews while it runs, so how long a
    deploy is *allowed* to take stops being LOCK_TTL_S minus the sum of every phase's
    timeout — a quantity nobody re-audits when a slower host joins the fleet."""
    assert acquire_update_lock("A", ttl_s=0.5) is True
    assert renew_update_lock("A", ttl_s=600.0) is True
    lease = read_update_lease()
    assert lease is not None and lease.holder == "A"
    assert lease.expires_in_s > 500.0


def test_renewal_by_a_non_holder_is_refused() -> None:
    """A lease already reclaimed past its TTL by a new owner must not be re-armed under
    the old holder's name."""
    assert acquire_update_lock("A") is True
    assert renew_update_lock("B") is False
    assert update_lock_holder() == "A"


def test_renewal_never_re_arms_a_settle_hold() -> None:
    """`note IS NOT NULL` is the stronger guard. A settle hold's whole value is that it
    ENDS — on convergence or on SETTLE_TTL_S — so a stray renewal from a straggler
    would convert a stated waiting period into an unbounded hold on the cluster."""
    assert acquire_update_lock("A") is True
    assert settle_update_lock("A", hosts=["win"]) is True
    assert renew_update_lock("A", ttl_s=99999.0) is False
    lease = read_update_lease()
    assert lease is not None and lease.note is not None
    assert lease.expires_in_s < SETTLE_TTL_S + 1.0  # the settle window, not the renewal


def test_renewal_re_arms_a_lease_that_had_already_lapsed() -> None:
    """Leaving a live rollout unprotected is worse than a late re-arm, so a renewal
    that finds the row lapsed still takes it back (it cannot steal from anyone — a new
    owner would no longer match the holder). The WARNING it logs is the signal that a
    renewal round was missed."""
    assert acquire_update_lock("A", ttl_s=-1.0) is True  # already expired
    assert read_update_lease() is None
    assert renew_update_lock("A", ttl_s=600.0) is True
    lease = read_update_lease()
    assert lease is not None and lease.holder == "A"


def test_self_holder_is_the_format_the_liveness_probe_parses() -> None:
    """One builder, because `ops.ops_cluster._lock_holder_is_live` parses it to decide
    whether `ava cluster recover` may break a hold. It is also how the Phase-B poll
    re-finds its own lease without the holder being threaded down four frames."""
    from shared.machine import machine_name

    holder = self_holder()
    machine, sep, pid_str = holder.partition(":pid")
    assert sep == ":pid"
    assert machine == machine_name()
    assert pid_str == str(os.getpid())


# ─── the explicit-model row: phase + kind (R1 wave, Task #1021) ──────────────


def test_acquire_enters_updating_and_records_kind() -> None:
    """The deployment_state row answers "is a deploy running, of what kind" — the
    explicit replacement for the flag-file / session-probe conjunction."""
    assert acquire_update_lock("A", kind="rollout") is True
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT phase, kind FROM deployment_state WHERE id=1")
        phase, kind = cur.fetchone()  # type: ignore[misc]
        assert phase == "updating"
        assert kind == "rollout"


def test_acquire_without_kind_leaves_kind_null() -> None:
    """A rollback takes the lease but has no kind in the enumeration — the row still
    reads as "a deploy is in progress" (phase updating, holder set)."""
    assert acquire_update_lock("A") is True
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT phase, kind FROM deployment_state WHERE id=1")
        phase, kind = cur.fetchone()  # type: ignore[misc]
        assert phase == "updating"
        assert kind is None


def test_settle_enters_settling_with_structured_hosts() -> None:
    """A settle hold lands phase='settling' with the waiting hosts structured (array)
    and human-readable (note/settle_note) — one fact, three renderings."""
    assert acquire_update_lock("A") is True
    assert settle_update_lock("A", hosts=["wsl", "mac"]) is True
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phase, settle_hosts, settle_note, note, settle_started_at "
            "FROM deployment_state WHERE id=1"
        )
        phase, hosts, settle_note, note, settle_started_at = cur.fetchone()  # type: ignore[misc]
        assert phase == "settling"
        assert hosts == ["mac", "wsl"]  # sorted by settle_note()
        assert settle_note == note == "settling, waiting for: mac, wsl"
        assert settle_started_at is not None  # the telemetry anchor (C3, task #2189)


def test_release_returns_to_stable_and_clears_kind() -> None:
    assert acquire_update_lock("A", kind="restart") is True
    release_update_lock("A")
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT phase, kind, holder FROM deployment_state WHERE id=1")
        phase, kind, holder = cur.fetchone()  # type: ignore[misc]
        assert phase == "stable"
        assert kind is None
        assert holder is None


def test_release_settle_hold_returns_to_stable() -> None:
    assert acquire_update_lock("A") is True
    assert settle_update_lock("A", hosts=["wsl"]) is True
    assert release_settle_hold("A") is True
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phase, settle_hosts, settle_started_at FROM deployment_state WHERE id=1"
        )
        phase, hosts, settle_started_at = cur.fetchone()  # type: ignore[misc]
        assert phase == "stable"
        assert hosts is None
        assert settle_started_at is None  # the telemetry anchor clears with the hold


def test_read_lease_carries_kind() -> None:
    """DeployLease exposes kind so consumers can say WHAT is running, not just that
    something is."""
    assert acquire_update_lock("A", kind="rollout") is True
    lease = read_update_lease()
    assert lease is not None and lease.kind == "rollout"
    settle_update_lock("A", hosts=["wsl"])
    lease2 = read_update_lease()
    assert lease2 is not None and lease2.kind == "rollout"  # kind survives a settle
