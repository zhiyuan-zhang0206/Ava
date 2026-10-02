"""The daily tick against a scripted wal-g: step order, first failure stops, skips are not failures.

The fake wal-g (`fake_walg.sh`) answers from files in the sandbox: a backup list that
grows when `backup-push` runs, a `wal-verify` report, and the log `delete` prints.
The gates that need a live cluster (deploy window, Postgres socket) are replaced by
two attributes of `tick`; everything else (config, key pin, lock, state file, argv and
environment handed to wal-g) is the production code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.db import pg_admin
from base.native_process.os_platform import file_lock
from services.gateway_side.walg import state, tick
from services.gateway_side.walg.state import RunRecord
from services.gateway_side.walg.tests.support import SECRETS, Sandbox, fixture_text, make_sandbox

FULL_4 = "base_0000000100000006000000C0"
T0 = datetime(2026, 10, 2, 6, 25, 0, tzinfo=UTC)

PG_DATA = Path("/pg/data")
DATABASE = "ava_main"
ADMIN_URL = "postgresql://tester@/postgres?host=/sockets/ava-pg-home&port=5433"


class Clock:
    """A clock that advances one minute per reading, so records order and differ."""

    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        value, self._now = self._now, self._now + timedelta(minutes=1)
        return value


def _accepts(_target: tick.PgTarget) -> bool:
    return True


def _refuses(_target: tick.PgTarget) -> bool:
    return False


def _never_due(*_args: Any) -> bool:
    return False


def _always_due(*_args: Any) -> bool:
    return True


def _backup_lists() -> tuple[str, str]:
    before = json.loads(fixture_text("backup-list.json"))
    newest = dict(before[-1])
    newest.update(backup_name=FULL_4, start_time="2026-10-08T06:25:00.5Z")
    return json.dumps(before), json.dumps([*before, newest])


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = make_sandbox(tmp_path, monkeypatch)
    before, after = _backup_lists()
    box.put("backups.json", before)
    box.put("backups-after.json", after)
    box.put("wal-verify.json", fixture_text("wal-verify-warning.json"))
    box.put("delete-dry.log", fixture_text("retention-dry-run.log"))
    box.put("delete-confirm.log", _confirm_log())
    monkeypatch.setattr(tick, "pg_target", lambda: tick.PgTarget(ADMIN_URL, PG_DATA, DATABASE))
    monkeypatch.setattr(tick, "postgres_accepts_connections", _accepts)
    monkeypatch.setattr(tick, "deploy_window_reason", lambda: None)
    monkeypatch.setattr(tick.drill, "drill_due", _never_due)  # the drill has its own tests below
    return box


def _confirm_log() -> str:
    dry = fixture_text("retention-dry-run.log")
    lines = [line for line in dry.splitlines() if "Dry run:" not in line]
    return "\n".join([*lines, "INFO: Objects deleted successfully: count=21"]) + "\n"


def _run(clock: Clock | None = None) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = tick.run_tick(lines.append, now=clock or Clock())
    return code, lines


def _verify_report(*, integrity: str) -> str:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = integrity
    return json.dumps(report)


def _assert_no_secret_anywhere(lines: list[str]) -> None:
    haystack = state.state_path().read_text() + "\n".join(lines)
    for secret in SECRETS:
        assert secret not in haystack


# ── the happy path ───────────────────────────────────────────────────────────


def test_a_run_goes_preflight_backup_verify_retention_in_that_order(sandbox: Sandbox) -> None:
    code, lines = _run()

    assert code == 0
    assert sandbox.calls() == [
        "backup-list --detail --json",
        f"backup-push {PG_DATA}",
        "backup-list --detail --json",
        "wal-verify integrity timeline --json",
        "delete retain FULL 3 --use-sentinel-time",
        "delete retain FULL 3 --use-sentinel-time --confirm",
    ]
    assert lines[0] == "preflight: ok, 4 backups listed"
    stored = json.loads(fixture_text("backup-list.json"))[-1]["compressed_size"]
    assert lines[1] == f"backup: {FULL_4} (full, {stored} bytes stored)"
    assert lines[2] == "verify: integrity WARNING, timeline OK"
    assert lines[-1] == f"ok: {FULL_4}; chain integrity WARNING, timeline OK; retention deleted 21"


def test_backup_push_gets_the_chain_limit_and_the_owner_only_socket(sandbox: Sandbox) -> None:
    _run()

    assert sandbox.env_log() == [
        "PGHOST=/sockets/ava-pg-home PGPORT=5433 PGUSER=tester WALG_DELTA_MAX_STEPS=6"
    ]


def test_wal_verify_gets_the_owner_only_socket_like_backup_push(sandbox: Sandbox) -> None:
    """Without the connection variables WAL-G dials libpq's default socket and fails."""
    _run()

    assert sandbox.verify_env_log() == ["PGHOST=/sockets/ava-pg-home PGPORT=5433 PGUSER=tester"]


def test_every_step_leaves_its_record_in_the_state_file(sandbox: Sandbox) -> None:
    code, lines = _run()

    recorded = state.read_state()
    assert code == 0
    assert recorded.tick == state.TickRecord(started_at=T0, skipped=None)
    run = recorded.run
    assert run is not None
    assert (run.status, run.step, run.started_at) == ("ok", None, T0)
    assert run.detail == f"{FULL_4}; chain integrity WARNING, timeline OK; retention deleted 21"
    assert recorded.backup is not None
    assert (recorded.backup.name, recorded.backup.kind) == (FULL_4, "full")
    assert recorded.verify is not None
    assert (recorded.verify.integrity, recorded.verify.timeline) == ("WARNING", "OK")
    assert recorded.retention is not None
    assert (recorded.retention.marked, recorded.retention.deleted) == (21, 21)
    assert recorded.drill is None
    _assert_no_secret_anywhere(lines)


def test_an_increment_is_recorded_as_a_delta(sandbox: Sandbox) -> None:
    before = json.loads(fixture_text("backup-list.json"))
    delta = dict(before[-1])
    delta.update(
        backup_name="base_0000000100000006000000C0_D_0000000100000004000000B6",
        start_time="2026-10-08T06:25:00Z",
    )
    sandbox.put("backups-after.json", json.dumps([*before, delta]))

    code, _ = _run()

    assert code == 0
    backup = state.read_state().backup
    assert backup is not None and backup.kind == "delta"


def test_with_few_full_backups_retention_does_not_ask_wal_g_to_delete(sandbox: Sandbox) -> None:
    listed = json.loads(fixture_text("backup-list.json"))[:2]  # one full backup and its increment
    newest = dict(listed[-1])
    newest.update(
        backup_name="base_0000000100000006000000C0_D_0000000100000000000000A6",
        start_time="2026-10-08T06:25:00Z",
    )
    sandbox.put("backups.json", json.dumps(listed))
    sandbox.put("backups-after.json", json.dumps([*listed, newest]))

    code, lines = _run()

    assert code == 0
    assert not [call for call in sandbox.calls() if call.startswith("delete")]
    assert "retention: 3 or fewer full backups, nothing expires yet" in lines
    retention = state.read_state().retention
    assert retention is not None and (retention.marked, retention.deleted) == (0, 0)


def test_a_verify_warning_does_not_stop_the_run(sandbox: Sandbox) -> None:
    code, _ = _run()

    assert code == 0
    assert any(call.endswith("--confirm") for call in sandbox.calls())


# ── failures: the first failing step ends the run, named ─────────────────────


def _assert_failed_at(step: str) -> RunRecord:
    run = state.read_state().run
    assert run is not None
    assert (run.status, run.step) == ("failed", step)
    return run


def test_a_failing_preflight_stops_before_any_backup(sandbox: Sandbox) -> None:
    sandbox.fail("backup-list")

    code, lines = _run()

    assert code == 1
    _assert_failed_at("preflight")
    assert not [call for call in sandbox.calls() if call.startswith("backup-push")]
    assert lines[-1].startswith("failed at preflight: wal-g backup-list failed (exit 1)")
    _assert_no_secret_anywhere(lines)


def test_a_key_that_is_not_the_pinned_one_fails_preflight(sandbox: Sandbox) -> None:
    from services.gateway_side.walg import config as walg_config

    walg_config.load_walg_config()  # pins the key
    sandbox.key_file.write_text("cd" * 32 + "\n")

    code, _ = _run()

    assert code == 1
    assert "not the key this home pinned" in _assert_failed_at("preflight").detail
    assert sandbox.calls() == []


def test_a_failing_backup_push_stops_before_verify(sandbox: Sandbox) -> None:
    sandbox.fail("backup-push")

    code, _ = _run()

    assert code == 1
    _assert_failed_at("backup")
    assert not [call for call in sandbox.calls() if call.startswith("wal-verify")]
    assert state.read_state().backup is None


def test_a_backup_that_never_appears_in_the_list_is_a_failure(sandbox: Sandbox) -> None:
    (sandbox.store_dir / "backups-after.json").unlink()

    code, _ = _run()

    assert code == 1
    assert "no new backup is listed" in _assert_failed_at("backup").detail


def test_the_first_ever_backup_is_a_backup_even_with_an_empty_before_list(
    sandbox: Sandbox,
) -> None:
    sandbox.put("backups.json", "[]")
    sandbox.put("backups-after.json", json.dumps(json.loads(fixture_text("backup-list.json"))[:1]))

    code, _ = _run()

    assert code == 0
    assert state.read_state().backup is not None


def test_a_chain_failure_with_exit_code_zero_stops_before_retention(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _verify_report(integrity="FAILURE"))
    sandbox.put("wal-verify.rc", "0")

    code, lines = _run()

    assert code == 1
    _assert_failed_at("verify")
    assert not [call for call in sandbox.calls() if call.startswith("delete")]
    verify = state.read_state().verify
    assert verify is not None and verify.integrity == "FAILURE"
    assert "the archived WAL chain is broken" in lines[-1]
    assert state.read_state().backup is not None, "the backup that happened is still recorded"


def test_a_failing_verify_command_stops_before_retention(sandbox: Sandbox) -> None:
    sandbox.fail("wal-verify")

    code, _ = _run()

    assert code == 1
    _assert_failed_at("verify")
    assert not [call for call in sandbox.calls() if call.startswith("delete")]


def test_a_retention_invariant_violation_never_confirms(sandbox: Sandbox) -> None:
    bad = fixture_text("retention-dry-run.log").replace("count=21", "count=22") + (
        f"INFO: Object marked for deletion: basebackups_005/{FULL_4}/metadata.json storage=default\n"
    )
    sandbox.put("delete-dry.log", bad)

    code, lines = _run()

    assert code == 1
    run = _assert_failed_at("retention")
    assert "newest backup depends on" in run.detail
    assert sandbox.calls()[-1] == "delete retain FULL 3 --use-sentinel-time"
    assert state.read_state().retention is None
    assert any(line.startswith("  basebackups_005/") for line in lines), "the plan was audited"


def test_an_unexpected_error_is_still_recorded_against_its_step(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(pg_admin_url: str) -> Any:
        raise ValueError("surprise")

    monkeypatch.setattr(tick, "verify_chain", explode)

    code, _ = _run()

    assert code == 1
    assert _assert_failed_at("verify").detail == "ValueError: surprise"


def test_a_failed_run_is_replaced_by_the_next_successful_one(sandbox: Sandbox) -> None:
    sandbox.fail("backup-push")
    _run()
    sandbox.fail()

    code, _ = _run()

    assert code == 0
    run = state.read_state().run
    assert run is not None and run.status == "ok"


# ── skips and no-ops are not failures ────────────────────────────────────────


def test_off_does_nothing_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = make_sandbox(tmp_path, monkeypatch, enabled=False)

    code, lines = _run()

    assert (code, box.calls()) == (0, [])
    assert not state.state_path().exists()
    assert "nothing to do" in lines[0]


def test_an_open_deploy_window_skips_without_calling_wal_g(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tick, "deploy_window_reason", lambda: "machine 'a' is mid-deploy")

    code, lines = _run()

    assert (code, sandbox.calls()) == (0, [])
    recorded = state.read_state()
    assert recorded.tick == state.TickRecord(
        started_at=T0, skipped="a deploy window is open (machine 'a' is mid-deploy)"
    )
    assert recorded.run is None
    assert lines == ["skipped: a deploy window is open (machine 'a' is mid-deploy)"]


def test_postgres_not_accepting_connections_skips(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tick, "postgres_accepts_connections", _refuses)

    code, _ = _run()

    assert (code, sandbox.calls()) == (0, [])
    assert state.read_state().tick == state.TickRecord(
        started_at=T0, skipped="postgres is not accepting connections"
    )


def test_a_skip_leaves_the_last_real_runs_failure_visible(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.fail("backup-push")
    _run()
    failed = state.read_state().run
    monkeypatch.setattr(tick, "deploy_window_reason", lambda: "a window")

    code, _ = _run()

    assert code == 0
    after = state.read_state()
    assert after.run == failed
    assert after.tick is not None and after.tick.skipped is not None


def test_a_tick_that_holds_the_lock_makes_a_second_one_stand_down(sandbox: Sandbox) -> None:
    state.walg_dir().mkdir(parents=True, exist_ok=True)

    with file_lock(state.lock_path()):
        code, lines = _run()

    assert (code, sandbox.calls()) == (0, [])
    assert lines == ["another WAL-G tick is still running; nothing to do"]
    assert not state.state_path().exists()


def test_the_lock_is_released_after_a_run(sandbox: Sandbox) -> None:
    _run()
    sandbox.put("backups.json", _backup_lists()[0])

    code, _ = _run()

    assert code == 0


# ── setup errors are recorded, not crashes ───────────────────────────────────


def test_a_corrupt_state_file_fails_the_tick_without_touching_the_bucket(sandbox: Sandbox) -> None:
    state.walg_dir().mkdir(parents=True, exist_ok=True)
    state.state_path().write_text("{broken")

    code, lines = _run()

    assert (code, sandbox.calls()) == (1, [])
    assert "remove" in lines[0] and str(state.state_path()) in lines[0]


def test_no_locally_owned_postgres_is_a_recorded_setup_failure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_postgres() -> tick.PgTarget:
        raise RuntimeError("a remote-managed data plane has no local owner authority")

    monkeypatch.setattr(tick, "pg_target", no_postgres)

    code, _ = _run()

    assert (code, sandbox.calls()) == (1, [])
    assert "remote-managed" in _assert_failed_at("preflight").detail


def test_a_socket_that_is_not_this_homes_postgres_is_a_recorded_failure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreign(_target: tick.PgTarget) -> bool:
        raise RuntimeError("PostgreSQL admin connection used a foreign Unix socket")

    monkeypatch.setattr(tick, "postgres_accepts_connections", foreign)

    code, _ = _run()

    assert (code, sandbox.calls()) == (1, [])
    assert "foreign Unix socket" in _assert_failed_at("preflight").detail


# ── the Postgres gate ────────────────────────────────────────────────────────


def test_postgres_reachability_is_a_custody_checked_dial_of_the_admin_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialed: list[tuple[str, Path | None]] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def connect(url: str, *, expected_data_dir: Path | None = None, **_kw: Any) -> _Conn:
        dialed.append((url, expected_data_dir))
        return _Conn()

    monkeypatch.setattr(pg_admin, "connect", connect)

    assert tick.postgres_accepts_connections(tick.PgTarget(ADMIN_URL, PG_DATA, DATABASE)) is True
    assert dialed == [(ADMIN_URL, PG_DATA)]


def test_a_refused_connection_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_kw: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(pg_admin, "connect", refuse)

    assert tick.postgres_accepts_connections(tick.PgTarget(ADMIN_URL, PG_DATA, DATABASE)) is False


def test_the_clock_reaches_the_records(sandbox: Sandbox) -> None:
    _run(Clock())

    recorded = state.read_state()
    assert recorded.run is not None and recorded.backup is not None
    assert recorded.run.started_at == T0
    assert recorded.run.finished_at > recorded.backup.finished_at > T0


# ── the weekly recovery drill ────────────────────────────────────────────────


class FakeDrill:
    """Stands in for `drill.run_drill`; records when it ran relative to wal-g's calls."""

    def __init__(self, sandbox: Sandbox, *, ok: bool = True) -> None:
        self.sandbox = sandbox
        self.ok = ok
        self.wal_g_calls_before: list[list[str]] = []
        self.backups: list[str] = []

    def __call__(
        self,
        _target: tick.PgTarget,
        backup: Any,
        previous: state.DrillRecord | None,
        _report: Any,
        now: Any,
    ) -> state.DrillRecord:
        self.wal_g_calls_before.append(self.sandbox.calls())
        self.backups.append(backup.name)
        finished = now()
        return state.DrillRecord(
            finished_at=finished,
            ok=self.ok,
            backup=backup.name,
            target_lsn="0/A3000000",
            seconds=12.0,
            detail="restored" if self.ok else "recovery failed: no segment",
            last_ok_at=finished if self.ok else (previous.last_ok_at if previous else None),
        )


@pytest.fixture
def due_drill(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> FakeDrill:
    fake = FakeDrill(sandbox)
    monkeypatch.setattr(tick.drill, "drill_due", _always_due)
    monkeypatch.setattr(tick.drill, "run_drill", fake)
    return fake


def test_a_due_drill_restores_yesterdays_backup_before_todays_backup_is_taken(
    sandbox: Sandbox, due_drill: FakeDrill
) -> None:
    code, lines = _run()

    assert code == 0
    # only the preflight listing had run when the drill started, and it drilled the newest
    # backup that existed then (not the one this run is about to make)
    assert due_drill.wal_g_calls_before == [["backup-list --detail --json"]]
    assert due_drill.backups == [json.loads(fixture_text("backup-list.json"))[-1]["backup_name"]]
    assert any(line.startswith("drill: ok in 12s (restored)") for line in lines)
    recorded = state.read_state()
    assert recorded.drill is not None and recorded.drill.ok
    assert recorded.run is not None and recorded.run.status == "ok"


def test_a_failed_drill_is_recorded_and_the_backup_still_runs(
    sandbox: Sandbox, due_drill: FakeDrill
) -> None:
    due_drill.ok = False

    code, lines = _run()

    assert code == 0  # the drill's failure is its own alert, not the run's
    assert any(f"backup-push {PG_DATA}" == call for call in sandbox.calls())
    assert any(line == "drill: FAILED after 12s: recovery failed: no segment" for line in lines)
    recorded = state.read_state()
    assert recorded.drill is not None and not recorded.drill.ok
    assert recorded.run is not None and recorded.run.status == "ok"


def test_a_drill_that_is_not_due_does_not_run(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDrill(sandbox)
    monkeypatch.setattr(tick.drill, "run_drill", fake)

    code, _ = _run()

    assert code == 0
    assert fake.backups == []
    assert state.read_state().drill is None


def test_the_drill_is_due_by_the_state_the_tick_read(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []

    def due(previous: Any, backups: list[Any], now: datetime) -> bool:
        seen.append((previous, [b.name for b in backups], now))
        return False

    monkeypatch.setattr(tick.drill, "drill_due", due)

    _run()

    ((previous, names, now),) = seen
    assert previous is None
    assert len(names) == 4 and now == T0 + timedelta(minutes=1)


def test_drill_now_runs_the_drill_and_records_it(sandbox: Sandbox, due_drill: FakeDrill) -> None:
    lines: list[str] = []

    code = tick.run_drill_now(lines.append, now=Clock())

    assert code == 0
    assert state.read_state().drill is not None
    assert not [call for call in sandbox.calls() if call.startswith("backup-push")]


def test_drill_now_exits_non_zero_when_the_drill_fails(
    sandbox: Sandbox, due_drill: FakeDrill
) -> None:
    due_drill.ok = False

    assert tick.run_drill_now(lambda _line: None, now=Clock()) == 1
    drill_record = state.read_state().drill
    assert drill_record is not None and not drill_record.ok


def test_drill_now_refuses_without_a_backup(sandbox: Sandbox, due_drill: FakeDrill) -> None:
    sandbox.put("backups.json", "[]")
    lines: list[str] = []

    assert tick.run_drill_now(lines.append, now=Clock()) == 1
    assert lines == ["failed: no backup exists yet; run `ava backup walg run` first"]
    assert due_drill.backups == []


def test_drill_now_needs_postgres_and_the_run_lock(
    sandbox: Sandbox, due_drill: FakeDrill, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(tick, "postgres_accepts_connections", _refuses)
    assert tick.run_drill_now(lines.append, now=Clock()) == 1
    assert lines == ["failed: postgres is not accepting connections"]

    monkeypatch.setattr(tick, "postgres_accepts_connections", _accepts)
    tick.ensure_private_dir(state.walg_dir())
    with file_lock(state.lock_path(), timeout_s=0):
        assert tick.run_drill_now(lines.append, now=Clock()) == 1
    assert lines[-1] == "failed: a WAL-G tick or drill is still running"
    assert due_drill.backups == []


def test_drill_now_while_wal_g_is_off_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)
    lines: list[str] = []

    assert tick.run_drill_now(lines.append) == 1
    assert "WAL-G is off" in lines[0]
