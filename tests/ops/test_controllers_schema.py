"""Schema controller reconcile behavior — ported from the watchdog daemon's
`_schema_reconcile` tests when the gate moved to `ops.controllers.schema`.

Covers: code-behind-DB spawns `ava cluster update`; a migration layout error self-heals
the same way (not misread as DB-unreachable); the shared cooldown prevents a
double spawn; DB-behind-code skips without spawning; a DB-unreachable blip skips
safely; aligned returns non-blocking; and a gateway that rejects the trigger
falls back to spawning the updater locally.

Also the SCOPE each arm reports (`BlockScope`): an arm that gets an updater running
owns the whole host (`ALL`), while every arm that is purely a statement about the DB
— unreachable, DB-behind-code, or code-behind-DB with nothing spawned — blocks only
the services that use the DB (`DB_DEPENDENT`), so a DB-free service like the headed
browser keeps being revived.

And the persistent heal backoff (`TestHealBackoff`): which arm arms it, that a
FAILING trigger arms it too, what clears it, and that a backed-off round is still a
blocked round the `ControllerManager` escalates.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import shared.db
from ops import manager
from ops.controllers import schema, update_trigger
from ops.controllers.base import BlockScope
from ops.manager import ControllerManager
from shared.cluster_lock import DeployLease as _DeployLease
from shared.migrations import CodeBehindSchema, MigrationLayoutError, SchemaVersionMismatch

# Captured before the autouse fixture below stubs it, so the predicate's own test can
# exercise the real implementation (same pattern as tests/cli/test_commands.py).
_real_pin_is_the_blocker = schema.pin_is_the_blocker
_real_deploy_already_running = schema._deploy_already_running


def _ask_the_real_deploy_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the autouse stub for the tests that are ABOUT the deferral."""
    monkeypatch.setattr(schema, "_deploy_already_running", _real_deploy_already_running)


def _scope() -> BlockScope:
    """`schema_reconcile`'s block scope. It returns `(scope, detail)` since #1074 —
    the detail rides out on `ReconcileResult.detail`, which the manager renders on its
    escalating blocked-round line; these assertions are about the scope."""
    return schema.schema_reconcile()[0]


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    """The shared process cooldown is a module global; isolate it between tests."""
    update_trigger.reset_cooldown()


@pytest.fixture(autouse=True)
def _no_deploy_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no lease and no orchestration session, so the code-behind-DB arm
    reaches its spawn. The deferral has its own section at the bottom of this file."""
    monkeypatch.setattr(schema, "_deploy_already_running", lambda: None)


@pytest.fixture(autouse=True)
def _pin_is_not_the_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: HEAD is not the cluster pin, so the code-behind-DB arm takes its
    ordinary self-heal path. The escalation that fires when it IS the pin has its own
    section at the bottom of this file."""
    monkeypatch.setattr(schema, "pin_is_the_blocker", lambda: None)


@pytest.fixture(autouse=True)
def heal_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the persistent heal record at a tmp file. Autouse: every arm can now
    read or write it, so a test that never mentions the backoff must still not read
    the developer's real `$AVA_HOME/schema_heal_attempt` (nor leave one behind)."""
    path = tmp_path / "schema_heal_attempt"
    monkeypatch.setattr(schema, "_schema_heal_attempt_path", lambda: path)
    return path


class _FakeConn:
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


def _fake_connect(*_a: object, **_kw: object) -> _FakeConn:
    """`with shared.db.connect(...)` returns a noop conn; the patched
    check_schema_version does not touch it."""
    return _FakeConn()


def _gateway_accepts(calls: list[bool]) -> Callable[[], bool]:
    """Stub for the gateway update trigger: record the call and report acceptance,
    which is what keeps the local-spawn fallback out of the way."""

    def _spawn() -> bool:
        calls.append(True)
        return True

    return _spawn


def test_reconcile_spawns_update_on_code_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_schema_version raises CodeBehindSchema → trigger_update, and the round is
    blocked for EVERYTHING: that updater is about to replace every process here."""

    def _raise(_conn: object) -> None:
        raise CodeBehindSchema("DB 17 > code 16")

    monkeypatch.setattr(schema, "check_schema_version", _raise)
    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    spawn_calls: list[bool] = []
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

    assert _scope() is BlockScope.ALL
    assert spawn_calls == [True]


def test_reconcile_spawns_update_on_migration_layout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MigrationLayoutError means the local parser choked on a file newer code
    understands — local code is behind. It must self-heal via `ava cluster update`, NOT
    fall through to the generic "DB unreachable" handler that never reconciles."""

    def _raise(_conn: object) -> None:
        raise MigrationLayoutError("0023_x.down.sql")

    monkeypatch.setattr(schema, "check_schema_version", _raise)
    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    spawn_calls: list[bool] = []
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

    assert _scope() is BlockScope.ALL
    assert spawn_calls == [True]


def test_reconcile_cooldown_prevents_double_spawn(
    monkeypatch: pytest.MonkeyPatch, heal_record: Path
) -> None:
    """Within the cooldown the second CodeBehindSchema does not spawn again (avoids
    stepping on the still-running `ava cluster update`)."""

    def _raise(_conn: object) -> None:
        raise CodeBehindSchema("stale")

    monkeypatch.setattr(schema, "check_schema_version", _raise)
    monkeypatch.setattr(shared.db, "connect", _fake_connect)

    spawn_calls: list[bool] = []

    def _fake_spawn() -> bool:
        spawn_calls.append(True)
        # Reproduce the real trigger_update side effect — arm the shared cooldown.
        update_trigger._last_update_spawn = time.monotonic()
        return True  # the gateway accepted the trigger; no local fallback

    monkeypatch.setattr(schema, "trigger_update", _fake_spawn)

    assert _scope() is BlockScope.ALL
    assert spawn_calls == [True]
    # Drop the persistent record so the COOLDOWN is what this test measures. Both
    # limiters now stand in front of a second spawn and the backoff is checked first,
    # so leaving it armed would let this test pass without the cooldown existing.
    heal_record.unlink()
    # Second call: still in cooldown → no second spawn, and the scope narrows to the
    # DB's users: nothing was spawned this round, so no host-wide transition is owed.
    assert _scope() is BlockScope.DB_DEPENDENT
    assert spawn_calls == [True]


def test_reconcile_db_behind_no_spawn_but_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB older than code blocks the DB's users without spawning: on an agent-runner
    only the gateway can migrate, so self-update is useless here. The wait lasts as
    long as the gateway takes, which is precisely why it must not hold down services
    that never read the schema."""

    def _raise(_conn: object) -> None:
        raise SchemaVersionMismatch("DB 16 < code 17 (pending: 1)")

    monkeypatch.setattr(schema, "check_schema_version", _raise)
    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    spawn_calls: list[bool] = []
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

    assert _scope() is BlockScope.DB_DEPENDENT
    assert spawn_calls == []


def test_reconcile_db_unreachable_blocks_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect raising any exception (DB down / misconfig) → block the DB's users
    only, do NOT spawn (state unknown, no assumption). This is the arm that used to
    take the whole roster down with the database."""

    def _connect_fails(*_a: object, **_kw: object) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(shared.db, "connect", _connect_fails)
    spawn_calls: list[bool] = []
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

    assert _scope() is BlockScope.DB_DEPENDENT
    assert spawn_calls == []


def test_reconcile_aligned_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema aligned → blocks nothing, the tick proceeds to its full roster."""
    monkeypatch.setattr(schema, "check_schema_version", lambda _conn: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    spawn_calls: list[bool] = []
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

    assert _scope() is BlockScope.NONE
    assert spawn_calls == []


class TestLocalSpawnFallback:
    """The gateway round-trip needs a gateway->runner leg a misregistered or
    unreachable runner cannot repair from here, so retrying it every tick is a
    closed loop. This controller runs ON the drifting host — spawn the updater
    locally instead, the same escape the pin controller already has."""

    @staticmethod
    def _code_behind(monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(_conn: object) -> None:
            raise CodeBehindSchema("DB ahead of code")

        monkeypatch.setattr(schema, "check_schema_version", _raise)
        monkeypatch.setattr(shared.db, "connect", _fake_connect)

    def test_rejected_gateway_trigger_spawns_locally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._code_behind(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", lambda: False)  # gateway rejected
        local: list[dict[str, str]] = []
        monkeypatch.setattr(
            "ops.cluster.spawn_update",
            lambda: local.append({"session": "ava-updater"}) or {"session": "ava-updater"},
        )

        assert _scope() is BlockScope.ALL
        assert len(local) == 1, "a rejected gateway trigger must fall back to a local spawn"

    def test_accepted_gateway_trigger_does_not_spawn_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._code_behind(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", lambda: True)
        local: list[bool] = []
        monkeypatch.setattr("ops.cluster.spawn_update", lambda: local.append(True))

        assert _scope() is BlockScope.ALL
        assert local == [], "the gateway accepted; a second local updater would race it"

    def test_in_flight_local_update_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An updater already running owns the host's pause — leave it alone and
        retry next tick rather than surfacing an exception out of the reconcile. It is
        still a host-wide transition (someone else's updater), so the scope stays
        ALL."""
        from ops.cluster import ClusterUpdateInProgress

        self._code_behind(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", lambda: False)

        def _busy() -> dict[str, str]:
            raise ClusterUpdateInProgress("ava-updater already exists")

        monkeypatch.setattr("ops.cluster.spawn_update", _busy)

        assert _scope() is BlockScope.ALL

    def test_local_spawn_failure_still_blocks_the_dbs_users(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both trigger paths failing must not let DB-dependent healthchecks run under
        a schema mismatch — that spawns daemons against a schema they cannot read. But
        with no updater running, nothing is transitioning this host, so the scope is
        the narrow DB one rather than ALL."""
        self._code_behind(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", lambda: False)

        def _boom() -> dict[str, str]:
            raise RuntimeError("session spawn not available")

        monkeypatch.setattr("ops.cluster.spawn_update", _boom)

        assert _scope() is BlockScope.DB_DEPENDENT


class TestHealBackoff:
    """The persistent per-drift backoff on the ONE arm that acts.

    Field shape being pinned: a Windows runner fired ~85 failed `ava cluster update` triggers
    in a 3h07m `Schema ahead of code` window, one per round, because the only limiter
    in front of the heal was a 120s *process* cooldown that the heal's own restart
    resets. The record under `$AVA_HOME` is what survives that restart.
    """

    @staticmethod
    def _drifted(monkeypatch: pytest.MonkeyPatch, message: str = "DB ahead of code") -> None:
        def _raise(_conn: object) -> None:
            raise CodeBehindSchema(message)

        monkeypatch.setattr(schema, "check_schema_version", _raise)
        monkeypatch.setattr(shared.db, "connect", _fake_connect)

    @staticmethod
    def _gateway_rejects_and_local_fails(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Both trigger paths dead — the win host's situation. Returns the attempt log."""
        attempts: list[str] = []

        def _gateway() -> bool:
            attempts.append("gateway")
            return False

        def _local() -> dict[str, str]:
            attempts.append("local")
            raise RuntimeError("session spawn not available")

        monkeypatch.setattr(schema, "trigger_update", _gateway)
        monkeypatch.setattr("ops.cluster.spawn_update", _local)
        return attempts

    def test_failed_trigger_arms_the_backoff(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """The bug PR #879 fixed in the pin controller, pinned here: a heal whose
        trigger FAILS must still arm its own backoff. Recorded only on success, the
        heal that never succeeds never rate-limits itself and retries forever — which
        is the loop that actually ran on win."""
        self._drifted(monkeypatch)
        attempts = self._gateway_rejects_and_local_fails(monkeypatch)

        assert _scope() is BlockScope.DB_DEPENDENT
        assert attempts == ["gateway", "local"]
        rec = json.loads(heal_record.read_text())
        assert rec["ok"] is False
        assert rec["consecutive_failures"] == 1
        assert "session spawn not available" in str(rec["last_error"])

        # Next round, inside the window: not one trigger of either kind.
        update_trigger.reset_cooldown()  # prove the BACKOFF is what holds, not the cooldown
        assert _scope() is BlockScope.DB_DEPENDENT
        assert attempts == ["gateway", "local"], "a failing heal must not retry every round"

    def test_accepted_trigger_also_arms_the_backoff(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """A trigger the gateway ACCEPTS arms it too. The gateway accepting is not the
        drift clearing: if the next round still reads the same drift, the accepted
        update did not land and a second one would only thrash."""
        self._drifted(monkeypatch)
        spawn_calls: list[bool] = []
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))

        assert _scope() is BlockScope.ALL
        assert json.loads(heal_record.read_text())["ok"] is True

        update_trigger.reset_cooldown()
        assert _scope() is BlockScope.DB_DEPENDENT
        assert spawn_calls == [True]

    def test_convergence_clears_the_backoff(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """What resets the window (1): the drift is gone. A backoff that outlived the
        drift would mean the NEXT drift — a different one, hours later — inherits a
        stale window it never earned."""
        self._drifted(monkeypatch)
        spawn_calls: list[bool] = []
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))
        assert _scope() is BlockScope.ALL
        assert heal_record.exists()

        monkeypatch.setattr(schema, "check_schema_version", lambda _conn: None)  # pyright: ignore[reportUnknownArgumentType]
        assert _scope() is BlockScope.NONE
        assert not heal_record.exists()

        # And the very same drift returning is healed at once, not waited out.
        self._drifted(monkeypatch)
        update_trigger.reset_cooldown()
        assert _scope() is BlockScope.ALL
        assert spawn_calls == [True, True]

    def test_a_changed_drift_is_not_in_backoff(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """What resets the window (2): the drift itself changed — the gateway applied
        another migration, so this is a new destination and gets its own attempt. The
        record is keyed on the check's report, not on "schema", precisely so a worse
        drift is not silently covered by the previous one's window."""
        self._drifted(monkeypatch, "DB has 1 migration(s) this checkout lacks")
        spawn_calls: list[bool] = []
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))
        assert _scope() is BlockScope.ALL

        update_trigger.reset_cooldown()
        self._drifted(monkeypatch, "DB has 2 migration(s) this checkout lacks")
        assert _scope() is BlockScope.ALL
        assert spawn_calls == [True, True]
        assert "2 migration(s)" in str(json.loads(heal_record.read_text())["target"])

    def test_expired_window_retries(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """What resets the window (3): it simply runs out. A host behind the schema must
        not be abandoned — the backoff slows the heal to ticks-per-hour, it does not
        stop it."""
        self._drifted(monkeypatch)
        spawn_calls: list[bool] = []
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))
        assert _scope() is BlockScope.ALL

        rec = json.loads(heal_record.read_text())
        rec["ts"] = time.time() - schema._SCHEMA_HEAL_BACKOFF_S - 1
        heal_record.write_text(json.dumps(rec))
        update_trigger.reset_cooldown()
        assert _scope() is BlockScope.ALL
        assert spawn_calls == [True, True]

    def test_in_flight_foreign_update_records_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """An updater that was ALREADY running is not our heal firing: it still owns the
        host (scope stays ALL) but our attempt did not happen, so it records ok=False.
        If that update clears the drift the converged round drops the record anyway."""
        from ops.cluster import ClusterUpdateInProgress

        self._drifted(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", lambda: False)

        def _busy() -> dict[str, str]:
            raise ClusterUpdateInProgress("ava-updater already exists")

        monkeypatch.setattr("ops.cluster.spawn_update", _busy)
        assert _scope() is BlockScope.ALL
        rec = json.loads(heal_record.read_text())
        assert rec["ok"] is False
        assert rec["last_error"] == "local: ClusterUpdateInProgress"

    @pytest.mark.parametrize(
        ("name", "setup"),
        [
            ("db-unreachable", "unreachable"),
            ("db-behind-code", "behind"),
            ("in-cooldown", "cooldown"),
        ],
    )
    def test_declining_arms_never_touch_the_record(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path, name: str, setup: str
    ) -> None:
        """Only the arm that RETRIES AN ACTION gets a backoff. These three merely decline
        to act — DB unreachable (state unknown, nothing spawned), DB behind code (only
        the gateway can migrate), and already inside the process cooldown — so a record
        here would rate-limit nothing and would mislead the next reader of the file."""
        if setup == "unreachable":

            def _connect_fails(*_a: object, **_kw: object) -> object:
                raise RuntimeError("connection refused")

            monkeypatch.setattr(shared.db, "connect", _connect_fails)
        elif setup == "behind":

            def _raise(_conn: object) -> None:
                raise SchemaVersionMismatch("DB 16 < code 17")

            monkeypatch.setattr(schema, "check_schema_version", _raise)
            monkeypatch.setattr(shared.db, "connect", _fake_connect)
        else:
            self._drifted(monkeypatch)
            update_trigger.arm_cooldown()

        spawn_calls: list[bool] = []
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))
        assert _scope() is BlockScope.DB_DEPENDENT
        assert spawn_calls == []
        assert not heal_record.exists(), f"the {name} arm spawned nothing to rate-limit"

    def test_db_unreachable_does_not_clear_an_armed_backoff(
        self, monkeypatch: pytest.MonkeyPatch, heal_record: Path
    ) -> None:
        """Only convergence clears the record — a DB that cannot be read must not.
        Otherwise a flapping DB resets the window every other round and hands the hot
        loop straight back, through the arm that knows the least about the drift."""
        self._drifted(monkeypatch)
        monkeypatch.setattr(schema, "trigger_update", _gateway_accepts([]))
        assert _scope() is BlockScope.ALL
        armed = heal_record.read_text()

        def _connect_fails(*_a: object, **_kw: object) -> object:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(shared.db, "connect", _connect_fails)
        assert _scope() is BlockScope.DB_DEPENDENT
        assert heal_record.read_text() == armed

    async def test_backed_off_round_is_still_a_blocked_round_that_escalates(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The backoff must not silence the alarm PR #941 just installed.

        Drives the real `ControllerManager` over the real `SchemaController` for the
        full escalation distance: the heal fires ONCE and then stays backed off, but
        every round still reports `DB_DEPENDENT`, so the streak keeps counting and
        reaches ERROR. A backoff that returned `NONE` instead would have hidden the
        3h07m gap all over again — quieter attempts, same signal.
        """
        self._drifted(monkeypatch)
        attempts = self._gateway_rejects_and_local_fails(monkeypatch)
        mgr = ControllerManager([schema.SchemaController()])

        with caplog.at_level("WARNING", logger="ops.manager"):
            for _ in range(manager._BLOCKED_ROUND_ALARM_ROUNDS):
                update_trigger.reset_cooldown()  # only the backoff may hold the heal back
                assert await mgr.reconcile("agent-runner") is BlockScope.DB_DEPENDENT

        assert attempts == ["gateway", "local"], "one heal attempt across the whole streak"
        blocked = [r for r in caplog.records if "round blocked by schema" in r.message]
        assert len(blocked) == manager._BLOCKED_ROUND_ALARM_ROUNDS, "every round must say so"
        assert blocked[-1].levelno == logging.ERROR, "the streak must still escalate"


def test_controller_wraps_reconcile_into_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """SchemaController.reconcile passes the scope through onto the ReconcileResult
    unchanged — it must not widen a DB-scoped finding into a host-wide one — and
    carries the reason out on `detail`, which the manager renders on its escalating
    blocked-round line."""
    monkeypatch.setattr(schema, "schema_reconcile", lambda: (BlockScope.DB_DEPENDENT, "why"))
    res = schema.SchemaController().reconcile("gateway")
    assert res.dimension == "schema" and res.blocks is BlockScope.DB_DEPENDENT
    assert res.detail == "why"
    monkeypatch.setattr(schema, "schema_reconcile", lambda: (BlockScope.ALL, "spawned"))
    assert schema.SchemaController().reconcile("gateway").blocks is BlockScope.ALL
    monkeypatch.setattr(schema, "schema_reconcile", lambda: (BlockScope.NONE, None))
    aligned = schema.SchemaController().reconcile("gateway")
    assert aligned.blocks is BlockScope.NONE and aligned.detail is None


# ─── the pin is the blocker: escalate, do not retry (issue #1074) ────────────
#
# Once prod's DB was ahead of the pinned commit, this controller's heal and the pin
# controller's heal fought for 111 minutes: `ava cluster update` moved HEAD to origin/main,
# the pin controller force-checked it back to a commit that lacks those migrations,
# and the next round produced the same `CodeBehindSchema`. Nothing advances the pin,
# because advancing it is a step of a SUCCESSFUL update. Six identical rc=1 rounds
# produced no signal a human would see.


def _code_behind(monkeypatch: pytest.MonkeyPatch, spawn_calls: list[bool]) -> None:
    """The code-behind-DB arm, with the gateway trigger and the local fallback both
    recording rather than acting."""

    def _raise(_conn: object) -> None:
        raise CodeBehindSchema("DB has 5 migration(s) this checkout lacks")

    monkeypatch.setattr(schema, "check_schema_version", _raise)
    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    monkeypatch.setattr(schema, "trigger_update", _gateway_accepts(spawn_calls))
    monkeypatch.setattr(
        schema,
        "_spawn_update_locally",
        lambda _d: spawn_calls.append(True) or True,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_escalates_instead_of_spawning_when_the_pin_is_behind_the_schema(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HEAD is the cluster pin AND this checkout lacks the DB's migrations, so the PIN
    lacks them. Spawning an update here is what the pin controller undoes; refuse it,
    log ERROR (which `shared.log` routes to agent_events) and name both remedies."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    monkeypatch.setattr(schema, "pin_is_the_blocker", lambda: "1a90f95d33a145d1")

    with caplog.at_level(logging.ERROR):
        blocks, detail = schema.schema_reconcile()

    assert spawn_calls == [], "an update the pin will undo must not be spawned"
    assert blocks is BlockScope.DB_DEPENDENT
    assert detail is not None and "1a90f95" in detail
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a livelock a human must break has to reach ERROR"
    assert "advance_pin" in errors[0] and "rollback_to" in errors[0]


def test_the_escalation_is_not_merely_a_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """It refuses ahead of the backoff, so it cannot degrade into "one checkout flap
    per half hour" — which is what the incident's 30-minute cadence actually was."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    monkeypatch.setattr(schema, "pin_is_the_blocker", lambda: "1a90f95d33a145d1")
    for _ in range(5):
        update_trigger.reset_cooldown()
        assert schema.schema_reconcile()[0] is BlockScope.DB_DEPENDENT
    assert spawn_calls == []


def test_still_heals_when_head_is_not_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrowing must not disable the ordinary heal: a host that is merely behind
    the DB — the 2026-07-28 wsl case this arm exists for — still spawns its update."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    monkeypatch.setattr(schema, "pin_is_the_blocker", lambda: None)
    assert schema.schema_reconcile()[0] is BlockScope.ALL
    assert spawn_calls == [True]


def test_pin_is_the_blocker_needs_head_to_equal_the_pin_in_the_prod_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predicate itself. HEAD == pin is the whole test — the exception in hand
    already established that this checkout lacks the migrations — and it is licensed
    only inside the prod source tree, because a dev worktree's watchdog reads PROD's
    HEAD while the schema check compares the DB against its OWN migrations/."""
    monkeypatch.setattr("shared.cluster_drift.running_from_prod_source", lambda: True)
    monkeypatch.setattr("ops.controllers.pin.read_pin_and_head", lambda: ("abc1234", "abc1234"))
    assert _real_pin_is_the_blocker() == "abc1234"

    monkeypatch.setattr("ops.controllers.pin.read_pin_and_head", lambda: ("abc1234", "def5678"))
    assert _real_pin_is_the_blocker() is None

    monkeypatch.setattr("ops.controllers.pin.read_pin_and_head", lambda: None)
    assert _real_pin_is_the_blocker() is None

    monkeypatch.setattr("shared.cluster_drift.running_from_prod_source", lambda: False)
    monkeypatch.setattr("ops.controllers.pin.read_pin_and_head", lambda: ("abc1234", "abc1234"))
    assert _real_pin_is_the_blocker() is None


# ─── this controller asks whether a deploy is already running (issue #1074) ──


def test_defers_the_heal_while_a_rollout_is_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    """This was the only acting controller that never asked, so it could fire an
    `ava cluster update` into a rollout's Phase B — or into its own previous updater, which is
    the spawn half of the 2026-07-31 flap. An executing lease blocks only the DB's
    users: nothing was spawned HERE, so no host-wide transition is owed."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    _ask_the_real_deploy_question(monkeypatch)
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: _DeployLease(
            holder="gateway-host:pid1", held_for_s=1.0, expires_in_s=900.0, note=None
        ),
    )
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)

    blocks, detail = schema.schema_reconcile()

    assert spawn_calls == []
    assert blocks is BlockScope.DB_DEPENDENT
    assert detail is not None and "gateway-host:pid1" in detail


def test_defers_the_heal_while_a_local_updater_runs_and_owns_the_whole_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog-spawned updater takes no lease, so the lease check alone would miss
    it. Scope `ALL` here, unlike the lease case: that updater is about to replace every
    process on this host."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    _ask_the_real_deploy_question(monkeypatch)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")

    blocks, _detail = schema.schema_reconcile()

    assert spawn_calls == []
    assert blocks is BlockScope.ALL


def test_a_settle_hold_does_not_block_the_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody executes under a settle hold, and the convergence it waits for is exactly
    what this heal produces — issue #1020's argument, in its narrowest form here:
    `note IS NULL` defers, every settle hold passes through. Chosen deliberately over
    `DeployLease.awaits` so the two PRs cannot deadlock in either merge order."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    _ask_the_real_deploy_question(monkeypatch)
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: _DeployLease(
            holder="gateway-host:pid1",
            held_for_s=1.0,
            expires_in_s=900.0,
            note="settling, waiting for: wsl",
        ),
    )
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)

    assert schema.schema_reconcile()[0] is BlockScope.ALL
    assert spawn_calls == [True]


def test_an_unreadable_signal_defers_rather_than_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawning an update on missing evidence is the expensive direction."""
    spawn_calls: list[bool] = []
    _code_behind(monkeypatch, spawn_calls)
    _ask_the_real_deploy_question(monkeypatch)

    def _boom():
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _boom)
    assert schema.schema_reconcile()[0] is BlockScope.DB_DEPENDENT
    assert spawn_calls == []
