"""Crash auto-resurrect — the "an involuntarily-dead agent still has work waiting"
reconcile.

Covers the pieces that make crash recovery correct and bounded:
- **termination_source stamping**: every code path that writes status='terminated'
  stamps WHO/WHAT killed the row — the reapers → 'reaper', the force-kill path →
  'user', the graceful self-exit → 'exit', the launch-confirm force AND the child's
  own early-boot schema/placement gates → 'launch-confirm', the respawn integrity
  fault → 'integrity'. A site that leaves NULL produces a corpse nothing can ever
  revive, so this set is also enforced statically by
  `scripts/lint_termination_source.py`.
- **eligibility (`_claim_crash_resurrect_candidates`)**: only 'reaper' / 'launch-confirm'
  corpses, with a pending inbound that is real work (not terminate/cancel), past the
  per-agent backoff window, machine-scoped. 'user' / 'exit' / 'integrity' / NULL are
  NEVER claimed — the intentional-death exemption, the corrupt-row exemption, and the
  conservative legacy-NULL default.
- **the two halves agreeing** (`TestBootGateCorpseIsResurrectable`): the corpse a real
  boot gate writes is actually selected by the real claim query. Stamping tests and
  eligibility tests each passed while the gates wrote rows the claim could never
  select, which is how the original hole survived.
- **the resurrect clear**: `resurrect_agent` clears termination_source on the
  terminated->idling transition, so the mark is strictly per-death.
- **the controller**: resurrects eligible corpses (stamping the backoff clock),
  gateway-health gated, 30s-throttled, no-op when disabled.

Launch is stubbed by the autouse `_guard_agent_launch`; `_require_released_agent_session` is
stubbed per-reconcile-test (resurrect_agent shells out to it before relaunch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent import _starting
from ops import agent_launch, agent_wake
from ops.agent_identity import AgentProcessIdentity
from ops.agents import respawn_agent, resurrect_agent
from ops.controllers import respawn as rd
from ops.controllers import resurrect as cr
from ops.controllers import wedged as wd
from ops.ops_lifecycle import _force_mark_terminated, mark_agent_exited_op
from shared.agents import ResurrectBudgetExhausted
from shared.config import settings
from shared.machine import machine_name
from shared.migrations import CodeBehindSchema
from tests.conftest import spawn_agent

_DEAD_PID = 424243
_LOCAL = machine_name()
_BACKOFF = 300.0


def _claim_ids(claims: list[agent_wake.AutoResurrectClaim]) -> list[int]:
    return [claim.agent_id for claim in claims]


@pytest.fixture
def sync_pool():
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
    live_lease: bool = True,
    produced_message: bool = True,
) -> int:
    """Seed a row in `status` with `pid`. `live_lease` grants the R1 liveness
    lease (default True — controllers that now gate on it see the row as
    alive); pass False to seed a lease-less zombie."""
    from datetime import UTC, datetime, timedelta

    aid = spawn_agent(spawner="user")
    lease = datetime.now(UTC) + timedelta(seconds=600) if live_lease else None
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status=%s, pid=%s, lease_expires_at=%s, "
            "last_message_text = CASE WHEN %s THEN 'done' ELSE NULL END WHERE id=%s",
            (status, pid, lease, produced_message, aid),
        )
    db.commit()
    return aid


def _open_page(db: psycopg.Connection, aid: int, name: str = "report") -> None:
    """Seed one open show() row, which terminate cascades may close (audit B2)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, title) "
            "VALUES (%s, %s, 18001, '127.0.0.1', 'Report')",
            (aid, name),
        )
    db.commit()


def _capture_page_closed(monkeypatch: pytest.MonkeyPatch, mod: object) -> list[tuple[int, str]]:
    """Capture publish_page_closed_sync calls on `mod` (audit B2)."""
    closed: list[tuple[int, str]] = []
    monkeypatch.setattr(mod, "publish_page_closed_sync", lambda i, n: closed.append((i, n)))  # pyright: ignore[reportUnknownArgumentType]
    return closed


def _corpse(
    db: psycopg.Connection,
    *,
    source: str | None = "reaper",
    last_resurrect_s_ago: float | None = None,
    machine: str | None = None,
) -> int:
    """Spawn a row forced into a terminated corpse with a chosen termination_source
    (None leaves it NULL — an un-stamped death) and optional last_resurrect_at
    backdate (None leaves it NULL — never auto-resurrected)."""
    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status='terminated', termination_source=%s WHERE id=%s",
            (source, aid),
        )
        if last_resurrect_s_ago is not None:
            cur.execute(
                "UPDATE agents_meta SET last_resurrect_at = now() - make_interval(secs => %s) "
                "WHERE id=%s",
                (last_resurrect_s_ago, aid),
            )
        if machine is not None:
            cur.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (machine, aid))
    db.commit()
    return aid


def _add_pending_inbound(db: psycopg.Connection, aid: int, kind: str = "chat") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, 'user')",
            (aid, "do the thing", kind),
        )
    db.commit()


def _add_pending_resurrect_inbound(db: psycopg.Connection, aid: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', 'system')",
            (aid,),
        )
    db.commit()


def _add_claimed_inbound(db: psycopg.Connection, aid: int, kind: str = "chat") -> None:
    """A message the agent had CLAIMED (status 'claimed', not 'pending') and was
    mid-processing when it died — the one in hand at crash time. Distinct from a
    'pending' inbound behind it in the queue."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
            "VALUES (%s, %s, %s, 'user', 'claimed')",
            (aid, "in flight when it died", kind),
        )
    db.commit()


def _row(db: psycopg.Connection, aid: int) -> tuple[str, str | None]:
    with db.cursor() as cur:
        cur.execute("SELECT status, termination_source FROM agents_meta WHERE id=%s", (aid,))
        return cur.fetchone()  # type: ignore[return-value]


def _last_resurrect_at(db: psycopg.Connection, aid: int):
    with db.cursor() as cur:
        cur.execute("SELECT last_resurrect_at FROM agents_meta WHERE id=%s", (aid,))
        return cur.fetchone()[0]  # type: ignore[index]


# ── termination_source stamping (every terminated-write site) ─────────────────


class TestTerminationSourceStamping:
    def test_reaper_dead_running_is_revived_not_stamped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        launched_agents: list,
    ) -> None:
        """G5 (Task #689): a dead post-message 'running' row is no longer reaped to
        'terminated' + stamped 'reaper' — the revive pass relaunches it in place
        (CAS to unclaimed 'idling', launch). Boot-phase deaths enter the separate
        crash-resurrect backoff path."""
        monkeypatch.setattr(rd, "probe_agent_process", lambda _pid, _aid: AgentProcessIdentity.GONE)  # pyright: ignore[reportUnknownArgumentType]
        aid = _park(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()
        assert aid in rd._revive_local_dead_running_idling(sync_pool, _LOCAL, max_revive=50)
        assert _row(db_conn, aid)[0] == "idling"  # revived, no 'reaper' stamp
        assert any(c.agent_id == aid for c in launched_agents)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_reaper_dead_boot_phase_stamps_reaper(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rd, "probe_agent_process", lambda _pid, _aid: AgentProcessIdentity.GONE)  # pyright: ignore[reportUnknownArgumentType]
        aid = _park(db_conn, status="running", pid=_DEAD_PID, produced_message=False)
        assert aid in rd._reap_local_dead_boot_phase_agents(sync_pool, _LOCAL)
        assert _row(db_conn, aid) == ("terminated", "reaper")

    def test_reaper_stale_unclaimed_idling_stamps_reaper(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        # grace 0 → any unclaimed idling row is stale
        assert aid in rd._reap_local_unclaimed_idling(sync_pool, _LOCAL, 0.0)
        assert _row(db_conn, aid) == ("terminated", "reaper")

    def test_force_mark_stamps_user(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="running", pid=_DEAD_PID)
        _force_mark_terminated(aid, sync_pool)
        assert _row(db_conn, aid) == ("terminated", "user")

    async def test_graceful_exit_stamps_exit(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _park(db_conn, status="running", pid=_DEAD_PID)
        await mark_agent_exited_op(aid, sync_pool)
        assert _row(db_conn, aid) == ("terminated", "exit")

    def test_launch_confirm_force_stamps_launch_confirm(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_confirm_launch_or_force_terminated` (spawn off-path: child never claimed)
        forces the unclaimed 'idling' row terminated + stamps 'launch-confirm'."""

        def _never_claims(_id: int) -> None:
            raise RuntimeError("confirm timeout")

        monkeypatch.setattr(agent_launch, "_wait_for_agent_claim", _never_claims)
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        _open_page(db_conn, aid)
        closed = _capture_page_closed(monkeypatch, agent_launch)
        agent_launch._confirm_launch_or_force_terminated(aid)
        assert _row(db_conn, aid) == ("terminated", "launch-confirm")
        # audit B2: an unclaimed 'idling' row can hold a show() page (resurrect
        # cascade_open reopens it) — force-terminate clears it via PageClosed.
        assert closed == [(aid, "report")]
        with db_conn.cursor() as cur:
            cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (aid,))
            assert cur.fetchone()[0] is True  # type: ignore[index]

    def test_launch_retry_exhausted_stamps_launch_confirm(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_launch_or_force_terminated` (resurrect/respawn relaunch) forces terminated
        + stamps 'launch-confirm' after retries are exhausted."""

        # Create the unclaimed 'idling' row BEFORE swapping in the failing launch (spawn
        # itself launches through the autouse spy — the boom is only for the relaunch).
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)

        def _boom(_id: int, config_overlay=None, **_kw: object) -> None:
            raise RuntimeError("launch failed")

        monkeypatch.setattr(agent_launch, "_launch_agent_process", _boom)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agent_launch, "_require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agent_launch, "_LAUNCH_MAX_RETRIES", 0)  # no retry sleeps
        _open_page(db_conn, aid)
        closed = _capture_page_closed(monkeypatch, agent_launch)
        with pytest.raises(RuntimeError):
            agent_launch._launch_or_force_terminated(aid)
        assert _row(db_conn, aid) == ("terminated", "launch-confirm")
        # audit B2: same PageClosed contract as the off-path confirm.
        assert closed == [(aid, "report")]

    def test_schema_gate_stamps_launch_confirm(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child's own early-boot schema gate rejects the boot before claiming its
        row and stamps 'launch-confirm' — the same source the launcher's confirm poll
        would have stamped, since this write is precisely what makes that poll fail.

        Left unstamped (the original defect) the corpse is unresurrectable forever.
        """
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)

        def _behind(_url: str) -> None:
            raise CodeBehindSchema("applied 3 > required 2")

        monkeypatch.setattr(_starting, "assert_schema_current", _behind)
        _open_page(db_conn, aid)
        closed = _capture_page_closed(monkeypatch, _starting)
        with pytest.raises(CodeBehindSchema):
            _starting.claim_agent_row_or_die_on_stale_schema(aid)
        assert _row(db_conn, aid) == ("terminated", "launch-confirm")
        # audit B2: the boot gate clears the popover for pages reopened by a
        # prior resurrect the same way every other terminated-write does.
        assert closed == [(aid, "report")]

    def test_placement_gate_stamps_launch_confirm(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The placement gate (agents_meta.machine names another host) rejects the boot
        before claiming and stamps 'launch-confirm'."""
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (aid,))
        db_conn.commit()
        _open_page(db_conn, aid)
        closed = _capture_page_closed(monkeypatch, _starting)
        with pytest.raises(RuntimeError, match="placement mismatch"):
            _starting.claim_agent_row(aid)
        assert _row(db_conn, aid) == ("terminated", "launch-confirm")
        # audit B2: PageClosed for the open page.
        assert closed == [(aid, "report")]

    def test_preclaim_terminate_keeps_daemon_page_open(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected boot closes only its agent-owned show() pages."""
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pages (agent_id, name, port, host, title, serve_dir) "
                "VALUES (%s, 'persistent', 18001, '127.0.0.1', 'Persistent', '/tmp/serve')",
                (aid,),
            )
        db_conn.commit()
        closed = _capture_page_closed(monkeypatch, _starting)

        _starting._mark_preclaim_terminated(aid)

        assert _row(db_conn, aid) == ("terminated", "launch-confirm")
        assert closed == []
        with db_conn.cursor() as cur:
            cur.execute("SELECT closed_at IS NULL FROM agent_pages WHERE agent_id = %s", (aid,))
            assert cur.fetchone()[0] is True  # type: ignore[index]

    def test_boot_gate_leaves_a_row_some_other_process_took_alone(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unclaimed-idling guard still scopes the write: a row that has
        already moved on (another process legitimately took it) is not clobbered, and
        crucially not mis-stamped 'launch-confirm' on top of its real source. Its open
        pages stay open too — no PageClosed may be published for a row that did not
        transition (audit B2)."""
        aid = _park(db_conn, status="running", pid=_DEAD_PID)
        _open_page(db_conn, aid)
        closed = _capture_page_closed(monkeypatch, _starting)
        _starting._mark_preclaim_terminated(aid)
        assert _row(db_conn, aid) == ("running", None)
        assert closed == []  # no transition -> pages stay open -> no events
        with db_conn.cursor() as cur:
            cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (aid,))
            assert cur.fetchone()[0] is False  # type: ignore[index]

    def test_respawn_integrity_fault_stamps_integrity(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`respawn_agent` finding status='restarting' with no 'restart' inbound forces
        the row terminated on a DB-integrity fault. It stamps 'integrity' — its own
        source, deliberately NOT resurrectable: the row's restart history is corrupt,
        so ops must look before it comes back (see TestClaimScan)."""
        aid = _park(db_conn, status="restarting")
        _open_page(db_conn, aid, name="panel")
        closed = _capture_page_closed(monkeypatch, agent_wake)
        with pytest.raises(RuntimeError, match="DB integrity violated"):
            respawn_agent(aid)
        assert _row(db_conn, aid) == ("terminated", "integrity")
        # audit B2: a 'restarting' row can hold open pages (restart keeps them
        # open); the integrity force-terminate clears the popover.
        assert closed == [(aid, "panel")]


# ── eligibility / claim scan ──────────────────────────────────────────────────


class TestClaimScan:
    def test_reaper_with_pending_claimed_and_stamps_backoff(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        assert _last_resurrect_at(db_conn, aid) is None
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))
        assert _last_resurrect_at(db_conn, aid) is not None  # backoff clock stamped by the claim

    def test_launch_confirm_with_pending_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="launch-confirm")
        _add_pending_inbound(db_conn, aid)
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))

    @pytest.mark.parametrize("source", ["user", "exit", "integrity", None])
    def test_intentional_and_null_never_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool, source: str | None
    ) -> None:
        """The exemption: an intentional death ('user'/'exit'), a corrupt-row death
        ('integrity' — ops inspects before it comes back), or a legacy un-stamped one
        (NULL — pre-column rows) is never resurrected, even with a pending inbound."""
        aid = _corpse(db_conn, source=source)
        _add_pending_inbound(db_conn, aid)
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )
        assert _last_resurrect_at(db_conn, aid) is None  # not even the clock is touched

    def test_no_pending_inbound_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="reaper")
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    @pytest.mark.parametrize(
        "kind", ["terminate", "cancel", "restart", "heartbeat", "compact_summary", "resurrect"]
    )
    def test_non_work_kind_only_pending_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool, kind: str
    ) -> None:
        """The pending filter is an explicit work-bearing ALLOWLIST (chat /
        compact_request), so a corpse whose only pending inbound is any other kind —
        a control signal (terminate/cancel/restart), a nudge (heartbeat), a
        self-artifact (compact_summary), a lifecycle marker (resurrect) — is not
        revived. In particular a lone pending `terminate` never revives an agent to
        process its own kill signal (codex)."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid, kind=kind)
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    @pytest.mark.parametrize("kind", ["chat", "compact_request"])
    def test_work_kind_pending_is_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool, kind: str
    ) -> None:
        """The two allowlisted work-bearing kinds — the exact set a live delivery
        resurrects for — each make a corpse eligible."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid, kind=kind)
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))

    def test_real_work_alongside_control_signal_is_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """A chat queued next to a terminate still counts — the real work wins."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid, kind="terminate")
        _add_pending_inbound(db_conn, aid, kind="chat")
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))


# ── boot-gate corpse <-> claim query agreement ────────────────────────────────
#
# The stamping tests above assert what the boot gates WRITE; the eligibility tests
# assert what the claim query READS. Neither alone catches the two disagreeing —
# which is exactly how the original defect survived: the gates wrote a corpse the
# claim could never select. These drive the REAL gate and then the REAL claim query
# end to end, so the two halves are pinned against each other rather than against a
# hand-rolled restatement of either.


class TestBootGateCorpseIsResurrectable:
    def test_schema_mismatch_boot_corpse_is_claimed(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A schema-mismatch boot rejection with work still queued is picked up by
        CrashResurrectController — so it retries once the host catches its code up
        (`ava cluster update`) instead of stranding the queue forever."""
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        _add_pending_inbound(db_conn, aid)

        def _behind(_url: str) -> None:
            raise CodeBehindSchema("applied 3 > required 2")

        monkeypatch.setattr(_starting, "assert_schema_current", _behind)
        with pytest.raises(CodeBehindSchema):
            _starting.claim_agent_row_or_die_on_stale_schema(aid)

        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))

    def test_placement_mismatch_boot_corpse_is_claimed_by_the_row_s_own_host(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """A placement-mismatch corpse is claimed by the machine the ROW names, not the
        one that mis-launched it — the scan is machine-scoped, so resurrecting is what
        puts the agent on its correct host. That is the repair, not a retry loop."""
        aid = _park(db_conn, status="idling", pid=None, live_lease=False)
        _add_pending_inbound(db_conn, aid)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (aid,))
        db_conn.commit()

        with pytest.raises(RuntimeError, match="placement mismatch"):
            _starting.claim_agent_row(aid)

        # The wrong host (the one that ran the doomed launch) must NOT claim it...
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )
        # ...the host the row actually names does.
        assert aid in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, "other-host", _BACKOFF)
        )

    def test_respawn_integrity_corpse_is_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """The other end of the same coin: the integrity fault stamps a source too (so
        it is never a silent NULL), but that source is deliberately outside the
        allowlist — a corrupt row waits for ops even with work queued."""
        aid = _park(db_conn, status="restarting")
        _add_pending_inbound(db_conn, aid)
        with pytest.raises(RuntimeError, match="DB integrity violated"):
            respawn_agent(aid)
        assert _row(db_conn, aid)[1] == "integrity"
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    @pytest.mark.parametrize("kind", ["chat", "compact_request"])
    def test_claimed_work_inbound_is_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool, kind: str
    ) -> None:
        """The message the agent had CLAIMED and was processing when it died
        (status 'claimed', not 'pending') now makes the corpse eligible — closing
        the hole where the ONLY work left is the in-flight message with nothing
        'pending' behind it. Before this, such an agent never revived and its
        claimed row was never reconciled (reconciliation only runs on fresh boot).
        The kind allowlist still applies."""
        aid = _corpse(db_conn, source="reaper")
        _add_claimed_inbound(db_conn, aid, kind=kind)
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))

    @pytest.mark.parametrize("kind", ["terminate", "cancel", "restart", "heartbeat"])
    def test_claimed_non_work_kind_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool, kind: str
    ) -> None:
        """The work-bearing allowlist gates 'claimed' rows the same as 'pending':
        a claimed control signal / nudge never revives the corpse."""
        aid = _corpse(db_conn, source="reaper")
        _add_claimed_inbound(db_conn, aid, kind=kind)
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    def test_done_inbound_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """Only 'pending'/'claimed' count — an already-'done' chat is finished
        work and must not revive a corpse (the predicate widened to 'claimed', not
        to every status)."""
        aid = _corpse(db_conn, source="reaper")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'handled', 'chat', 'user', 'done')",
                (aid,),
            )
        db_conn.commit()
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    def test_force_terminate_overwrites_reaper_stamp(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """Race (codex): the reaper stamps 'reaper' (a crash), then the user force-
        terminates the same row (a terminate that found the pid already dead). The
        force MUST overwrite the stale 'reaper' with 'user', so the user's kill is not
        undone by an auto-resurrect of the still-queued work."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)  # real work still queued
        _force_mark_terminated(aid, sync_pool)  # user force-kills the already-dead agent
        assert _row(db_conn, aid) == ("terminated", "user")  # stamp overwritten
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    def test_within_backoff_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="reaper", last_resurrect_s_ago=10)  # < 300s window
        _add_pending_inbound(db_conn, aid)
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )

    def test_past_backoff_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="reaper", last_resurrect_s_ago=9999)  # > window
        _add_pending_inbound(db_conn, aid)
        assert aid in _claim_ids(cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF))

    def test_claim_is_idempotent_within_backoff(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        """The claim stamps last_resurrect_at, so a second immediate scan finds the
        row inside its fresh backoff window — no double-resurrect."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        first = cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        second = cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        assert aid in _claim_ids(first)
        assert aid not in _claim_ids(second)

    def test_other_machine_not_claimed(
        self, db_conn: psycopg.Connection, sync_pool: ConnectionPool
    ) -> None:
        aid = _corpse(db_conn, source="reaper", machine="other-box")
        _add_pending_inbound(db_conn, aid)
        assert aid not in _claim_ids(
            cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)
        )


class TestControllerFinalClaimGuard:
    def test_crash_claim_then_user_force_is_stale_at_final_cas(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        launched_agents: list,
    ) -> None:
        """A controller task claimed for a reaper death cannot reverse a user
        force that lands before its final resurrection transition."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        claim = cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)[0]
        _force_mark_terminated(aid, sync_pool)
        launched_agents.clear()
        with pytest.raises(agent_wake.ResurrectClaimStaleError):
            resurrect_agent(
                aid,
                resurrected_by="system",
                auto_claim=claim,
            )

        assert _row(db_conn, aid) == ("terminated", "user")
        assert launched_agents == []
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect'",
                (aid,),
            )
            assert cur.fetchone() == (0,)

    def test_crash_claim_death_epoch_blocks_same_source_aba(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
        launched_agents: list,
    ) -> None:
        """source+backoff can repeat after manual revive and another crash;
        the exact status_changed_at death epoch makes the old claim stale."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        old_claim = cr._claim_crash_resurrect_candidates(sync_pool, _LOCAL, _BACKOFF)[0]

        resurrect_agent(aid, resurrected_by="user")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' "
                "WHERE id = %s",
                (aid,),
            )
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = %s + interval '1 second' WHERE id = %s",
                (old_claim.termination_epoch, aid),
            )
        db_conn.commit()
        launched_agents.clear()
        with pytest.raises(agent_wake.ResurrectClaimStaleError):
            resurrect_agent(
                aid,
                resurrected_by="system",
                auto_claim=old_claim,
            )

        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert launched_agents == []
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect'",
                (aid,),
            )
            assert cur.fetchone() == (1,)


# ── resurrect clears the per-death mark ──────────────────────────────────────


class TestResurrectClearsSource:
    def test_resurrect_agent_clears_termination_source(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        launched_agents: list,
    ) -> None:
        """The mark is per-death: bringing a corpse back to unclaimed 'idling' clears
        termination_source, so a write site that later forgets to stamp leaves NULL
        (not eligible) instead of a stale 'reaper'."""
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]
        aid = _corpse(db_conn, source="reaper")
        resurrect_agent(aid, resurrected_by="system")
        assert _row(db_conn, aid) == ("idling", None)


class TestSystemResurrectBudget:
    def test_reads_cluster_file_budget_when_gateway_has_only_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gateway pop must not turn an operator-set budget into the default."""
        from shared import runtime_config

        def _gateway_default(_name: str) -> int:
            return 3

        monkeypatch.setattr(agent_wake, "get_field", _gateway_default)
        monkeypatch.setattr(
            runtime_config,
            "read_env_aliases",
            lambda: {"AVA_AUTO_RESURRECT_MAX_ATTEMPTS": "5"},
        )

        assert agent_wake._auto_resurrect_max_attempts() == 5

    def test_system_resurrect_stops_at_pending_budget(self, db_conn: psycopg.Connection) -> None:
        """A failed system recovery leaves the corpse unchanged at its budget."""
        aid = _corpse(db_conn, source="reaper")
        for _ in range(settings.daemon.auto_resurrect_max_attempts):
            _add_pending_resurrect_inbound(db_conn, aid)

        with pytest.raises(ResurrectBudgetExhausted, match="exhausted"):
            resurrect_agent(aid, resurrected_by="system")

        assert _row(db_conn, aid) == ("terminated", "reaper")

    def test_system_resurrect_allows_pending_rows_below_budget(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A system recovery below the limit still wakes the terminated agent."""
        aid = _corpse(db_conn, source="reaper")
        for _ in range(settings.daemon.auto_resurrect_max_attempts - 1):
            _add_pending_resurrect_inbound(db_conn, aid)

        resurrect_agent(aid, resurrected_by="system")

        assert _row(db_conn, aid) == ("idling", None)

    def test_manual_resurrect_bypasses_pending_budget(self, db_conn: psycopg.Connection) -> None:
        """Manual recovery remains available after automatic retries are exhausted."""
        aid = _corpse(db_conn, source="reaper")
        for _ in range(settings.daemon.auto_resurrect_max_attempts):
            _add_pending_resurrect_inbound(db_conn, aid)

        resurrect_agent(aid, resurrected_by="user")

        assert _row(db_conn, aid) == ("idling", None)


# ── controller reconcile ─────────────────────────────────────────────────────


def _confirm_launch(_agent_id: int) -> bool:
    return True


@pytest.fixture(autouse=True)
def _host_is_serving(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Controller cases start from a host that already passed its start gate."""
    from shared import start_serving

    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True


class TestControllerReconcile:
    @pytest.fixture(autouse=True)
    def _tune(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.daemon, "auto_resurrect_enabled", True)
        monkeypatch.setattr(settings.daemon, "auto_resurrect_backoff_seconds", _BACKOFF)
        monkeypatch.setattr(cr, "_gateway_healthy", lambda: True)
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            agent_launch,
            "_confirm_launch_or_force_terminated",
            _confirm_launch,
        )

    def test_resurrects_involuntary_corpse_with_pending(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)  # resurrected + source cleared
        assert _last_resurrect_at(db_conn, aid) is not None  # backoff clock stamped
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_attempt_budget_stops_crash_resurrect_at_limit(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """Three unconsumed lifecycle rows prove three failed recoveries, so the
        crash scan leaves the corpse alone instead of starting a fourth boot."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        for _ in range(3):
            _add_pending_resurrect_inbound(db_conn, aid)
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert aid not in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_attempt_budget_allows_crash_resurrect_below_limit(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """Two unconsumed lifecycle rows remain below the default budget, so an
        eligible corpse still gets its third recovery attempt."""
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        for _ in range(2):
            _add_pending_resurrect_inbound(db_conn, aid)
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True

        result = controller.reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_resurrects_corpse_with_only_claimed_work(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """End-to-end claimed-only case: a corpse whose sole in-hand message is a
        'claimed' chat (in flight when it died, nothing 'pending' behind it) is
        resurrected. Before the predicate matched 'claimed' this agent stranded —
        the specific laptop-death shape where the DB outage killed the process
        mid-turn with one message already claimed."""
        aid = _corpse(db_conn, source="reaper")
        _add_claimed_inbound(db_conn, aid, kind="chat")
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)  # resurrected + source cleared
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_does_not_resurrect_user_terminate(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        aid = _corpse(db_conn, source="user")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "user")
        assert aid not in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_gateway_unhealthy_defers_without_stamping(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gateway down → defer BEFORE the claim, so the backoff clock is untouched:
        the corpse stays eligible and is retried immediately once the gateway is back."""
        monkeypatch.setattr(cr, "_gateway_healthy", lambda: False)
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert _last_resurrect_at(db_conn, aid) is None  # not burned during the outage
        assert launched_agents == []

    def test_new_start_before_continuous_claim_leaves_backoff_unstamped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A newly-starting host closes recovery before a crash claim mutates it."""
        from shared import start_serving

        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True
        original = controller._resurrect_crashed

        def close_before_claim(local_machine: str) -> tuple[bool, bool]:
            start_serving.begin_start()
            return original(local_machine)

        monkeypatch.setattr(controller, "_resurrect_crashed", close_before_claim)

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert _last_resurrect_at(db_conn, aid) is None
        assert launched_agents == []

    def test_closed_boundary_does_not_throttle_the_next_serving_scan(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """A failed start defers recovery without delaying its ready successor."""
        from shared import start_serving

        start_serving.clear_serving()
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True

        assert controller.reconcile("agent-runner").acted is False
        assert controller._last_scan == 0.0

        generation = start_serving.begin_start()
        assert start_serving.mark_serving(generation) is True

        assert controller.reconcile("agent-runner").acted is True
        assert _row(db_conn, aid) == ("idling", None)
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_closed_boundary_does_not_throttle_when_gateway_is_down(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unavailable gateway cannot turn a failed start into a delayed retry."""
        from shared import start_serving

        start_serving.clear_serving()
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        monkeypatch.setattr(cr, "_gateway_healthy", lambda: False)
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True

        assert controller.reconcile("agent-runner").acted is False
        assert controller._last_scan == 0.0
        assert _last_resurrect_at(db_conn, aid) is None

    def test_disabled_is_noop(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings.daemon, "auto_resurrect_enabled", False)
        aid = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, aid)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert launched_agents == []

    def test_throttled_second_pass_does_not_scan(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """The scan is throttled to _SCAN_INTERVAL_S: a second reconcile right after
        the first does not pick up a corpse that appeared in between."""
        controller = cr.CrashResurrectController(sync_pool)
        controller._boot_pass_done = True  # skip the one-shot boot revive; test the throttle only
        controller.reconcile("agent-runner")  # first crash scan sets _last_scan

        late = _corpse(db_conn, source="reaper")
        _add_pending_inbound(db_conn, late)
        launched_agents.clear()

        result = controller.reconcile("agent-runner")  # throttled

        assert result.acted is False
        assert _row(db_conn, late) == ("terminated", "reaper")  # untouched
        assert launched_agents == []


class TestBootRevivePass:
    """The one-shot boot revive (Task #694 G5): the restarter's FIRST reconcile
    resurrects this host's involuntary corpses WITHOUT a pending-inbound
    requirement — the machine-reboot fleet restore. A resident agent reaped
    with no queued work used to stay dead across reboots forever (crash-
    resurrect needs a pending inbound; the running/idling revive pass never
    sees an already-'terminated' row)."""

    @pytest.fixture(autouse=True)
    def _tune(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.daemon, "auto_resurrect_enabled", True)
        monkeypatch.setattr(settings.daemon, "auto_resurrect_backoff_seconds", _BACKOFF)
        monkeypatch.setattr(cr, "_gateway_healthy", lambda: True)
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            agent_launch,
            "_confirm_launch_or_force_terminated",
            _confirm_launch,
        )

    def test_boot_pass_resurrects_corpse_without_pending(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """The gap: a 'reaper' corpse with NO pending inbound is invisible to
        crash-resurrect; the boot pass brings it back on the first reconcile."""
        aid = _corpse(db_conn, source="reaper")
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)  # resurrected + source cleared
        assert _last_resurrect_at(db_conn, aid) is not None
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_boot_pass_attempt_budget_stops_at_limit(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """A daemon restart cannot bypass three failed recovery attempts."""
        aid = _corpse(db_conn, source="reaper")
        for _ in range(settings.daemon.auto_resurrect_max_attempts):
            _add_pending_resurrect_inbound(db_conn, aid)
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert launched_agents == []

    def test_boot_pass_attempt_budget_allows_below_limit(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
    ) -> None:
        """A corpse below the recovery budget still joins the fleet restore."""
        aid = _corpse(db_conn, source="reaper")
        for _ in range(settings.daemon.auto_resurrect_max_attempts - 1):
            _add_pending_resurrect_inbound(db_conn, aid)
        controller = cr.CrashResurrectController(sync_pool)

        result = controller.reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)

    def test_boot_pass_defers_until_the_host_is_serving(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed start leaves involuntary corpses available for its successor.

        Removing the serving gate would relaunch the corpse in the first pass;
        marking the boot pass done while deferred would strand it in the second.
        """
        from shared import start_serving

        start_serving.clear_serving()
        aid = _corpse(db_conn, source="reaper")
        launched_agents.clear()
        controller = cr.CrashResurrectController(sync_pool)

        result = controller.reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert launched_agents == []

        generation = start_serving.begin_start()
        assert start_serving.mark_serving(generation) is True
        result = controller.reconcile("agent-runner")

        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_boot_pass_excludes_explicit_termination(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """user / exit / integrity / NULL stay dead — the same eligibility rule
        as crash-resurrect: only involuntary deaths come back."""
        for source in ("user", "exit", "integrity", None):
            _corpse(db_conn, source=source)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        for source in ("user", "exit", "integrity", None):
            # all four remain terminated with their source intact
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status, termination_source FROM agents_meta "
                    "WHERE termination_source IS NOT DISTINCT FROM %s AND status = 'terminated'",
                    (source,),
                )
                assert cur.fetchone() is not None
        assert launched_agents == []

    def test_boot_pass_runs_once_per_daemon_start(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """The pass is one-shot: a corpse appearing AFTER the first reconcile is
        left for the delivery path / crash-resurrect (throttled scan), not
        re-revived by a second boot pass."""
        controller = cr.CrashResurrectController(sync_pool)
        controller.reconcile("agent-runner")  # boot pass runs (nothing to revive)

        late = _corpse(db_conn, source="reaper")
        launched_agents.clear()

        controller.reconcile("agent-runner")  # boot pass done; crash scan throttled

        assert _row(db_conn, late) == ("terminated", "reaper")  # untouched
        assert launched_agents == []

    def test_boot_pass_gateway_down_defers_and_retries(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gateway down at boot → the pass is skipped WITHOUT burning the
        one-shot: once the gateway is back the SAME daemon start still revives
        the corpses (nothing is stamped while down)."""
        monkeypatch.setattr(cr, "_gateway_healthy", lambda: False)
        aid = _corpse(db_conn, source="reaper")
        controller = cr.CrashResurrectController(sync_pool)
        launched_agents.clear()

        result = controller.reconcile("agent-runner")
        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert _last_resurrect_at(db_conn, aid) is None  # not stamped during the outage

        monkeypatch.setattr(cr, "_gateway_healthy", lambda: True)
        result = controller.reconcile("agent-runner")
        assert result.acted is True
        assert _row(db_conn, aid) == ("idling", None)
        assert aid in [c.agent_id for c in launched_agents]  # pyright: ignore[reportUnknownMemberType]

    def test_boot_pass_respects_backoff(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """A corpse whose last_resurrect_at is inside the backoff window is not
        re-claimed by the boot pass (a recent failed resurrect is spaced)."""
        aid = _corpse(db_conn, source="reaper", last_resurrect_s_ago=10)
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, aid) == ("terminated", "reaper")
        assert launched_agents == []

    def test_boot_pass_machine_scoped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
    ) -> None:
        """A foreign-machine corpse is never claimed locally (the resurrect
        would launch a process on the wrong host)."""
        foreign = _corpse(db_conn, source="reaper", machine="other-host")
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is False
        assert _row(db_conn, foreign) == ("terminated", "reaper")
        assert launched_agents == []

    def test_boot_pass_capped(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        launched_agents: list,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The anti-storm cap (`revive_max_per_pass`) bounds one pass: a
        mass-death set drains over daemon starts, never as a burst."""
        monkeypatch.setattr(settings.daemon, "revive_max_per_pass", 2)
        ids = [_corpse(db_conn, source="reaper") for _ in range(5)]
        launched_agents.clear()

        result = cr.CrashResurrectController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        revived = {c.agent_id for c in launched_agents}  # pyright: ignore[reportUnknownMemberType]
        assert len(revived) == 2  # capped  # pyright: ignore[reportUnknownArgumentType]
        assert revived <= set(ids)
        # the other three stay terminated and eligible
        for aid in set(ids) - revived:
            assert _row(db_conn, aid)[0] == "terminated"


# ── wedged controller: force-terminate PageClosed (audit B2) ────────────────


class TestWedgedForceTerminatePublishesPageClosed:
    """The wedged controller force-marks the stuck row 'terminated' before
    killing it — agent-owned show() pages cascade-close and must be announced
    (audit B2), exactly like the reaper / launch paths. Daemon-supervised
    serve() pages stay open. The immediate resurrect reopens show() rows the
    cascade closed, so the frontend ends consistent either way."""

    def test_reconcile_publishes_page_closed_for_open_pages(
        self,
        db_conn: psycopg.Connection,
        sync_pool: ConnectionPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aid = _park(db_conn, status="running", pid=12345)
        _open_page(db_conn, aid, name="panel")

        def _claimed_candidate(
            _pool: ConnectionPool,
            _machine: str,
            _running_age_s: float,
            _idling_age_s: float,
            _backoff_s: float,
        ) -> list[tuple[int, int, datetime]]:
            return [(aid, 12345, datetime.now(UTC))]

        def _owned_process(_pid: int, _agent_id: int) -> AgentProcessIdentity:
            return AgentProcessIdentity.OWNED

        monkeypatch.setattr(
            wd,
            "_claim_wedged_candidates",
            _claimed_candidate,
        )
        monkeypatch.setattr(
            wd,
            "probe_agent_process",
            _owned_process,
        )
        killed: list[int] = []
        monkeypatch.setattr(wd, "force_kill", killed.append)
        monkeypatch.setattr(wd, "resurrect_agent", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            agent_launch,
            "_confirm_launch_or_force_terminated",
            _confirm_launch,
        )
        monkeypatch.setattr(wd, "_gateway_healthy", lambda: True)
        monkeypatch.setattr(settings.daemon, "wedged_agent_enabled", True)
        closed = _capture_page_closed(monkeypatch, wd)

        result = wd.WedgedAgentController(sync_pool).reconcile("agent-runner")

        assert result.acted is True
        assert killed == [12345]
        assert closed == [(aid, "panel")]
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (aid,))
            assert cur.fetchone()[0] == "terminated"  # type: ignore[index]
            cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (aid,))
            assert cur.fetchone()[0] is True  # type: ignore[index]
