"""`services.healthchecks.restarter` — the 2026-07-24 outage, replayed.

A pytest-leaked restarter daemon (its own `$AVA_HOME`, but fallen back to prod's
default health port) answered 200 on prod's port for 98 minutes while prod's own
restarter was dead. The healthcheck believed the status code, no-op'd every
round, and every `restarting` agent in the cluster stayed frozen — then, when it
finally did restart, it logged "daemon restarted successfully" nine times over
nine crashes.

Both halves are pinned here at the `main()` level, where the outage actually
played out: an unidentified 200 must NOT be a no-op, and an unverified respawn
must NOT be reported as a success.

The third half is the **ordering**. A DB-reading catch-up used to run between the
dead verdict and the respawn, so a dead DB raised out of `main()` and the respawn
was never attempted — the recovery path for a daemon crash was gated on an
unrelated component being healthy. The respawn now runs first, and the tests below
pin that a dead DB can neither block it nor mask its failure.
"""

from __future__ import annotations

import logging

import psycopg
import pytest

from ops.controllers.base import BlockScope, ReconcileResult
from services.healthchecks import restarter as hc
from shared.config import settings
from shared.daemon_health import DaemonProbe


class _FakePool:
    """Stand-in for a psycopg_pool ConnectionPool; records that it was closed."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the DB is touched at all, by any route.

    The alive and revived-successfully paths must be entirely DB-free: the daemon's
    own RespawnController owns the dispatch, so a connection on either path is a
    regression toward the coupling this module was fixed to remove.

    Both `connect` and `pool` are stopped. Naming only the helper the current
    implementation happens to call would let the next one reintroduce the coupling
    through the other door — and would have let the *previous* one pass: the suite
    provisions a live throwaway DB, so a `connect()`-based catch-up simply succeeded
    against an empty `agents_meta` and these tests went green for the wrong reason."""

    def _forbidden(*_a: object, **_kw: object) -> object:
        pytest.fail("healthcheck must not touch the DB on this path")

    monkeypatch.setattr(hc.shared.db, "connect", _forbidden)
    monkeypatch.setattr(hc.shared.db, "pool", _forbidden)


@pytest.fixture
def dead_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable data plane: every route to it raises, not just one.

    Same reason as `no_db` — "the DB is down" has to be a fact about the DB, not
    about which helper the code under test picked."""

    def _raise(*_a: object, **_kw: object) -> object:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(hc.shared.db, "connect", _raise)
    monkeypatch.setattr(hc.shared.db, "pool", _raise)


@pytest.fixture
def standin_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record stand-in dispatch invocations without letting it reach a DB."""
    calls: list[int] = []
    monkeypatch.setattr(hc, "_standin_dispatch", lambda: calls.append(1))
    return calls


@pytest.fixture(autouse=True)
def _quiet_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` calls init_gateway_process(); no need for real log wiring here."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def test_probe_is_scoped_to_this_units_restarter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity is checked against name=restarter + this unit's pidfile."""
    seen: dict[str, object] = {}

    def fake_probe_daemon(name, url, *, pidfile, **_kw) -> DaemonProbe:
        seen.update(name=name, url=url, pidfile=pidfile)  # pyright: ignore[reportUnknownArgumentType]
        return DaemonProbe.up("stub")

    monkeypatch.setattr(hc, "probe_daemon", fake_probe_daemon)  # pyright: ignore[reportUnknownArgumentType]
    hc._probe()
    assert seen["name"] == "restarter"
    assert seen["pidfile"] == settings.services.restarter_pidfile


def test_live_daemon_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, standin_calls: list[int], no_db: None
) -> None:
    """A verified-alive daemon must not be restarted — the check has to stay a
    no-op in the common case or it would churn the cluster every 60s. And it must
    reach no DB (see `no_db`)."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.up("pid 111"))
    monkeypatch.setattr(
        hc, "_restart_daemon", lambda: pytest.fail("must not restart a live daemon")
    )

    hc.main()

    assert standin_calls == []


def test_a_same_cluster_stray_triggers_a_restart(
    monkeypatch: pytest.MonkeyPatch, standin_calls: list[int]
) -> None:
    """THE outage: something answers healthz but is not our daemon → restart, not
    the silent no-op that stranded the cluster for 98 minutes.

    The stray holds the port, so the respawn cannot bind it and never verifies —
    which is exactly the case where the stand-in has to carry the round's restarts,
    because no daemon will be dispatching them until a human evicts the stray.

    The verdict here is DOWN while the detail *reads* as an identity mismatch: a stray
    of this same cluster, which `respawn_service`'s kill-session does clear, so trying
    is right. A foreign-cluster occupant is the terminal verdict instead and skips the
    respawn entirely — see `test_healthcheck_terminal_state.py`. The branch follows
    the verdict, never the wording of the detail."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("identity mismatch: pid 999 != 111"))
    restarts: list[int] = []
    monkeypatch.setattr(
        hc,
        "_restart_daemon",
        lambda: (restarts.append(1), DaemonProbe.down("[Errno 48] Address already in use"))[1],
    )

    hc.main()  # no SystemExit since #1941 — the round reports and returns

    assert restarts == [1]
    assert standin_calls == [1]


def test_unverified_restart_reports_and_returns(
    monkeypatch: pytest.MonkeyPatch,
    standin_calls: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A respawn the daemon never confirmed is a FAILURE. Previously any
    a session spawn that returned cleanly was logged as a success, so nine
    consecutive crashes read as nine successes. Since #1941 the round reports
    the failure (WARNING naming the scheduled next attempt) and returns instead
    of exiting — the backoff, not an exit code, paces the retries."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("healthz unreachable"))
    monkeypatch.setattr(hc, "_restart_daemon", lambda: DaemonProbe.down("healthz unreachable"))

    with caplog.at_level(logging.WARNING):
        hc.main()  # no SystemExit
    assert (
        "daemon restart FAILED (healthz unreachable) — next respawn attempt in 60s" in caplog.text
    )
    assert standin_calls == [1]


def test_verified_restart_does_not_exit(
    monkeypatch: pytest.MonkeyPatch, standin_calls: list[int], no_db: None
) -> None:
    """A verified respawn ends the healthcheck's job: the daemon sweeps this host's
    'restarting' rows on its own first tick (~1s), so there is no catch-up to run
    and no reason to open a connection."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("healthz unreachable"))
    monkeypatch.setattr(hc, "_restart_daemon", lambda: DaemonProbe.up("pid 222"))

    hc.main()  # no SystemExit

    assert standin_calls == []


def test_db_down_still_restarts_the_daemon(monkeypatch: pytest.MonkeyPatch, dead_db: None) -> None:
    """THE coupling this module was fixed to remove: a dead DB must not stop the
    respawn.

    A DB outage and a daemon crash are independent events. The catch-up that used to
    run first opened a connection, so an `OperationalError` escaped `main()`; the
    watchdog isolates each check, so the round survived and `_restart_daemon` was
    simply never called. The one condition under which the restarter is most needed
    was the one condition under which it could not be revived."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("healthz unreachable"))
    restarts: list[int] = []
    monkeypatch.setattr(
        hc, "_restart_daemon", lambda: (restarts.append(1), DaemonProbe.up("pid 333"))[1]
    )

    hc.main()

    assert restarts == [1]


def test_respawn_precedes_any_db_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordering invariant, pinned directly: nothing that can fail on the DB may
    sit between the dead verdict and the respawn."""
    order: list[str] = []
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("healthz unreachable"))
    monkeypatch.setattr(
        hc,
        "_restart_daemon",
        lambda: (order.append("restart"), DaemonProbe.down("still down"))[1],
    )
    monkeypatch.setattr(hc, "_standin_dispatch", lambda: order.append("standin"))

    hc.main()  # no SystemExit since #1941

    assert order == ["restart", "standin"]


def test_db_failure_does_not_mask_the_restart_failure(
    monkeypatch: pytest.MonkeyPatch, dead_db: None, caplog: pytest.LogCaptureFixture
) -> None:
    """When both fail, the respawn failure is still reported.

    The old shape lost it: the DB exception escaped before the respawn ran, so the
    more important signal — "this daemon needs a human" — was never produced."""
    monkeypatch.setattr(hc, "_probe", lambda: DaemonProbe.down("healthz unreachable"))
    monkeypatch.setattr(hc, "_restart_daemon", lambda: DaemonProbe.down("healthz unreachable"))

    with caplog.at_level("WARNING"):
        hc.main()  # no SystemExit since #1941

    assert "daemon restart FAILED" in caplog.text


def test_standin_dispatch_delegates_to_respawn_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stand-in reuses the daemon's controller instead of re-implementing the
    dispatch.

    This is why the hand-rolled version was deleted: its SELECT had no `machine`
    filter and no gateway-health gate, so it respawned other machines' agents
    locally (tripping the boot placement gate) and pushed restarts at a gateway that
    could not accept them. Delegating inherits both, for free."""
    seen: dict[str, object] = {}
    fake_pool = _FakePool()

    class _FakeController:
        def __init__(self, pool: object) -> None:
            seen["pool"] = pool

        def reconcile(self, role: str) -> ReconcileResult:
            seen["role"] = role
            return ReconcileResult(dimension="respawn", blocks=BlockScope.NONE, acted=True)

    def fake_pool_factory(**kwargs: object) -> _FakePool:
        seen["kwargs"] = kwargs
        return fake_pool

    monkeypatch.setattr(hc.shared.db, "pool", fake_pool_factory)
    monkeypatch.setattr(hc, "RespawnController", _FakeController)

    hc._standin_dispatch()

    assert seen["role"] == "agent-runner"
    assert seen["pool"] is fake_pool
    # Bounded wait: the stand-in runs inside the watchdog's sequential tick, so it
    # must not sit on psycopg_pool's 30s default while other checks queue behind it.
    assert seen["kwargs"] == {"timeout": hc._STANDIN_POOL_TIMEOUT_S}
    assert fake_pool.closed == 1


def test_standin_dispatch_never_raises_on_a_dead_pool(dead_db: None) -> None:
    """Total by contract — a pool that cannot open is logged, not raised. Raising
    here would unwind `main()` before `sys.exit(1)` and lose the failure signal."""
    hc._standin_dispatch()  # no exception


def test_standin_dispatch_never_raises_on_a_failing_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract for a controller that raises mid-pass (a query timing out on a
    half-dead DB), and the pool is still closed."""
    fake_pool = _FakePool()

    class _ExplodingController:
        def __init__(self, pool: object) -> None:
            pass

        def reconcile(self, role: str) -> ReconcileResult:
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(hc.shared.db, "pool", lambda **_kw: fake_pool)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "RespawnController", _ExplodingController)

    hc._standin_dispatch()  # no exception

    assert fake_pool.closed == 1


def test_hosted_runner_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted mode gates the whole round: the roster disables the restarter
    there, so the healthcheck must neither probe, nor respawn, nor stand in —
    a stand-in reconcile would reap healthy hosted-agent rows (2026-09-02,
    agent 2986 harvested mid-turn)."""
    monkeypatch.setattr(hc, "runner_mode", lambda: "hosted")
    monkeypatch.setattr(hc, "_probe", lambda: pytest.fail("must not probe on hosted"))
    monkeypatch.setattr(hc, "_restart_daemon", lambda: pytest.fail("must not respawn on hosted"))
    monkeypatch.setattr(hc, "_standin_dispatch", lambda: pytest.fail("must not stand in on hosted"))

    hc.main()  # returns after the guard, no keepalive round


def test_process_runner_still_runs_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is mode-specific: on a process runner the round must run
    exactly as before."""
    monkeypatch.setattr(hc, "runner_mode", lambda: "process")
    rounds: list[int] = []

    def _record_round(*a: object, **k: object) -> None:
        rounds.append(1)

    monkeypatch.setattr(hc, "run_keepalive", _record_round)

    hc.main()

    assert rounds == [1]
