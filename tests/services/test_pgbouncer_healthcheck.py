"""pgbouncer healthcheck: the pooled front door gets a probe, an alert and a self-heal.

On 2026-08-06 a per-cluster PgBouncer was killed by mistake. `AVA_DB_URL` points at
the pooler whenever it is enabled, so every gateway daemon and every agent lost its
database — for 3.5 minutes, with no probe, no event and no revive. The gateway, the
frontend and the agent-runners all had healthchecks; the pooler was the one
data-plane process without one.

What is asserted here — the decision logic, not pgbouncer itself (a real pooler in
transaction pooling is `tests/cli/test_pgbouncer_wire.py`):

- a live pooler is a no-op (no restart, no event) — the watchdog must not bounce a
  healthy pooler every 60s;
- a dead pooler is restarted through the SAME idempotent bring-up `ava start` uses,
  with the registry-derived ports, and the repair is VERIFIED before it is claimed;
- a repair that does not take raises, so the watchdog reports a failing healthcheck
  instead of logging a success it did not get;
- the probe is the pooler's ADMIN console, never the end-to-end `SELECT 1` — a
  Postgres outage must not be answered by restarting a healthy pooler;
- the event is emitted only after the verified repair (the emitter writes THROUGH
  this pooler);
- a cluster with the pooler disabled is skipped, and so is one whose port or
  identity cannot be resolved — probing a guessed port would restart a live pooler
  every round.
"""

from __future__ import annotations

import inspect

import pytest

from services.healthchecks import pgbouncer as hc

_SECRET = "s3cr3t"  # noqa: S105 — test fixture, not a real credential


class _Calls:
    """Records what the check decided to do."""

    def __init__(self, *, reachable: list[bool], rc: int = 0) -> None:
        self.reachable = list(reachable)
        self.rc = rc
        self.probes: list[tuple[int, str]] = []
        self.ensured: list[dict[str, object]] = []
        self.events: list[tuple[str, dict[str, object]]] = []

    def probe(self, listen_port: int, role: str, _secret: str) -> bool:
        self.probes.append((listen_port, role))
        return self.reachable.pop(0) if self.reachable else False

    def ensure(self, **kwargs: object) -> int:
        self.ensured.append(kwargs)
        return self.rc

    def emit(self, _category: str, kind: str, **kwargs: object) -> None:
        self.events.append((kind, kwargs))


def _wire(monkeypatch: pytest.MonkeyPatch, calls: _Calls) -> None:
    """Stub the three seams the check reaches through (probe / repair / emit).

    They are imported inside `check` and `_emit_repaired`, so the patch goes on the
    defining modules rather than on this one.
    """
    monkeypatch.setattr(
        "cli.commands._pgbouncer.pgbouncer_listener_reachable", calls.probe, raising=True
    )
    monkeypatch.setattr("cli.commands._pgbouncer.ensure_pgbouncer", calls.ensure, raising=True)
    monkeypatch.setattr("shared.telemetry.emit", calls.emit, raising=True)


def _check(calls: _Calls) -> None:
    hc.check(listen_port=16433, pg_port=15433, identity="ava_probe", cluster_secret=_SECRET)


def test_live_pooler_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy pooler must not be touched — a restart every round would bounce the
    connections this check exists to protect."""
    calls = _Calls(reachable=[True])
    _wire(monkeypatch, calls)

    _check(calls)

    assert calls.ensured == []
    assert calls.events == []


def test_dead_pooler_is_restarted_with_the_registry_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repair is the idempotent bring-up, handed the ports and identity the
    caller resolved — not a second implementation and not a hardcoded 6433."""
    calls = _Calls(reachable=[False, True])
    _wire(monkeypatch, calls)

    _check(calls)

    assert calls.ensured == [
        {
            "pg_port": 15433,
            "listen_port": 16433,
            "db_name": "ava_probe",
            "role": "ava_probe",
            "cluster_secret": "s3cr3t",
        }
    ]


def test_repair_is_verified_before_it_is_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe, restart, probe again. `ensure_pgbouncer` returning 0 means the process
    was started, not that the listener answers."""
    calls = _Calls(reachable=[False, True])
    _wire(monkeypatch, calls)

    _check(calls)

    assert calls.probes == [(16433, "ava_probe"), (16433, "ava_probe")]


def test_repair_that_did_not_take_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Still dead after the restart: the watchdog has to log a failing healthcheck.
    Swallowing it is how the pooler stayed dead for 3.5 minutes in the first place."""
    calls = _Calls(reachable=[False, False])
    _wire(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="pooled database front door"):
        _check(calls)

    # The loguru->telemetry bridge (shared/log.py _StdlibInterceptHandler)
    # forwards log records as 'log' events when it is active, so the warning
    # above is captured too. What must be empty is the *repair* events: a
    # failed repair must not announce itself as one.
    assert [e for e in calls.events if e[0] != "log"] == [], (
        "a failed repair must not announce itself as one"
    )


def test_nonzero_bring_up_raises_without_a_second_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure_pgbouncer` already said it failed (e.g. pgbouncer not installed);
    the raise names its rc so the operator gets the real cause."""
    calls = _Calls(reachable=[False, True], rc=1)
    _wire(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="rc=1"):
        _check(calls)


def test_event_is_emitted_only_after_the_verified_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The emitter writes to Postgres THROUGH this pooler, so an event announcing the
    outage would be enqueued into the thing that is down. Only the repair is
    reportable, and it carries the port so a pooler that dies repeatedly is
    distinguishable from a one-off."""
    calls = _Calls(reachable=[False, True])
    _wire(monkeypatch, calls)

    _check(calls)

    # Same bridge caveat as above: filter the forwarded log records out; the
    # assertion counts repair events only.
    repair_events = [e for e in calls.events if e[0] != "log"]
    assert len(repair_events) == 1
    kind, kwargs = repair_events[0]
    assert kind == "pgbouncer_repaired"
    assert kwargs["attributes"] == {"listen_port": 16433}
    assert kwargs["level"] == "warning"


# ── the probe choice ─────────────────────────────────────────────────────────


def test_check_probes_the_admin_console_not_the_end_to_end_path() -> None:
    """The load-bearing distinction, asserted against the source: an end-to-end
    `SELECT 1` also fails when Postgres is down, and the answer to a Postgres outage
    is not to restart a healthy pooler every round."""
    src = inspect.getsource(hc.check)
    assert "pgbouncer_listener_reachable" in src
    assert "pgbouncer_reachable" not in src.replace("pgbouncer_listener_reachable", "")


# ── the skips ────────────────────────────────────────────────────────────────


def test_disabled_pooler_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_PGBOUNCER_ENABLED false is the documented kill-switch: consumers dial
    Postgres directly and there is no pooler to keep alive."""
    calls = _Calls(reachable=[False])
    _wire(monkeypatch, calls)
    monkeypatch.setattr(hc.settings.data_plane, "pgbouncer_enabled", False)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    hc.main()

    assert calls.probes == []
    assert calls.ensured == []


def test_missing_registry_record_is_skipped_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listen port is a registry fact only (not materialized in .env). Guessing
    it would probe the wrong port, read a live pooler as dead, and restart it every
    round."""
    calls = _Calls(reachable=[False])
    _wire(monkeypatch, calls)
    monkeypatch.setattr(hc.settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    hc.main()

    assert calls.probes == []
    assert calls.ensured == []


def test_identity_less_db_url_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """No scram identity in the db_url means the probe cannot authenticate against
    the pooled front door — the same shape as the redis-acl check's username-less
    skip, and `ava converge` is what backfills it."""
    calls = _Calls(reachable=[False])
    _wire(monkeypatch, calls)
    monkeypatch.setattr(hc.settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "get_record", lambda _home: object())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    def _no_identity() -> str:
        raise ValueError("db_url carries no username")

    monkeypatch.setattr(hc, "db_identity", _no_identity)

    hc.main()

    assert calls.probes == []
    assert calls.ensured == []


# ── the watchdog wiring ──────────────────────────────────────────────────────


def test_pgbouncer_runs_before_every_service_and_is_not_db_scoped() -> None:
    """Order and block-scope, both load-bearing.

    Ahead of the services because when the pooler is enabled it IS their
    `AVA_DB_URL` — reviving a daemon first only produces one that cannot reach the
    database. `requires_db=False` because a DB-scoped block exists precisely when
    the database is unreachable, and holding back the check that repairs the front
    door to it would be a deadlock."""
    import services.watchdog.daemon as wd

    names = [c.name for c in wd._checks_for_capability("gateway")]
    assert "pgbouncer" in names
    assert names.index("pgbouncer") < min(
        i for i, n in enumerate(names) if n not in ("redis-acl", "pgbouncer")
    )
    pgb = next(c for c in wd._checks_for_capability("gateway") if c.name == "pgbouncer")
    assert pgb.requires_db is False


def test_agent_runner_watchdog_does_not_probe_the_pooler() -> None:
    """The pooler is this cluster's own process beside the Postgres it fronts; a
    runner-only host has neither."""
    import services.watchdog.daemon as wd

    assert "pgbouncer" not in [c.name for c in wd._checks_for_capability("agent-runner")]
