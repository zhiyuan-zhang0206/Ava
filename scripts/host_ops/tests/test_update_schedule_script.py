"""External maintenance edits use the installed writer and preserve desired state."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from functools import partial
from pathlib import Path

import psycopg
import pytest

from base.db import Database
from base.deploy.lifecycle import home_lifecycle_locks
from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.native_process.os_platform import LockTimeoutError
from gateway.schedules import router, session_control
from scripts.host_ops import update_schedule_script as repair


@pytest.fixture(autouse=True)
def private_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def publish(phase: MaintenancePhase, *, failed: bool = False) -> None:
    stamp = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
    before = pause_owner.begin_maintenance("test:script-repair", stamp).snapshot
    assert before.maintenance is not None
    assert before.holder is not None
    hold = MaintenanceHold(phase, {}, failures={1: "RuntimeError"} if failed else {})
    pause_owner.change_maintenance(before.holder, stamp, before.maintenance, hold)


@pytest.mark.parametrize("phase", [p for p in MaintenancePhase if p != MaintenancePhase.STOPPED])
def test_only_certified_stopped_phase_is_accepted(phase: MaintenancePhase) -> None:
    publish(phase)
    with pytest.raises(RuntimeError, match="failure-free stopped"):
        repair.require_stopped_hold()


def test_stopped_failure_receipt_is_refused() -> None:
    publish(MaintenancePhase.STOPPED, failed=True)
    with pytest.raises(RuntimeError, match="failure-free stopped"):
        repair.require_stopped_hold()


def test_retained_root_is_not_a_refusal() -> None:
    publish(MaintenancePhase.STOPPED)
    before = pause_owner.state_path().read_bytes()
    assert repair.require_stopped_hold().maintenance.phase == MaintenancePhase.STOPPED
    assert pause_owner.state_path().read_bytes() == before


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_journal_refuses_before_database(
    corrupt: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if corrupt:
        pause_owner.state_path().write_text("broken")

    def installed(_home: Path, _source: Path) -> None:
        pass

    monkeypatch.setattr(repair, "load_installed_runtime", installed)

    def unexpected_database() -> None:
        pytest.fail("a refused hold must not build a database handle")

    monkeypatch.setattr(Database, "from_settings", unexpected_database)
    with pytest.raises(RuntimeError):
        repair.repair_script(
            home=tmp_path,
            source=tmp_path,
            schedule_id=1,
            script="pass\n",
            expected_sha256="0" * 64,
        )


def test_wrong_interpreter_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"own \.venv"):
        repair.load_installed_runtime(tmp_path, tmp_path / "wrong")


def test_wrong_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must match"):
        repair.load_installed_runtime(tmp_path / "different-home", Path(sys.prefix).parent)


def test_lifecycle_lock_prevents_a_competing_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def installed(_home: Path, _source: Path) -> None:
        pass

    monkeypatch.setattr(repair, "load_installed_runtime", installed)
    owner_lock = home_lifecycle_locks.resource_lock
    monkeypatch.setattr(home_lifecycle_locks, "resource_lock", partial(owner_lock, timeout_s=0.01))
    with (
        owner_lock(purpose="test:other-lifecycle"),
        pytest.raises(LockTimeoutError, match="last holder"),
    ):
        repair.repair_script(
            home=tmp_path,
            source=tmp_path,
            schedule_id=1,
            script="pass\n",
            expected_sha256="0" * 64,
        )


def test_wrong_home_source_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(sys.prefix).parent
    (tmp_path / "start-intent.json").write_text(
        json.dumps({"home": str(tmp_path), "checkout": str(tmp_path / "another-source")})
    )
    monkeypatch.delenv("AVA_DB_GENERATION", raising=False)
    monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="source checkout"):
        repair.load_installed_runtime(tmp_path, source)


@pytest.mark.parametrize("key", ["AVA_PROCESS_PROFILE", "AVA_LAUNCHER_PROFILE"])
def test_launcher_delivery_cannot_be_transplanted(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key, "untrusted-delivery")
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="without a launcher"):
        repair.load_installed_runtime(tmp_path, Path(sys.prefix).parent)


@pytest.mark.parametrize("enabled", [False, True])
def test_versioned_edit_preserves_fields_and_defers_session_work(
    enabled: bool, db_conn: psycopg.Connection, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    with database.pool() as pool:
        row = router._create_blocking(
            pool,
            router.ScheduleCreate(
                name="maintenance-edit",
                description="keep description",
                script="print(1)\n",
                command="python custom.py",
                enabled=enabled,
            ),
        )
        sid = row[0]

        def unexpected_wait(*args: object) -> None:
            pytest.fail("maintenance edit must not wait for or launch session work")

        monkeypatch.setattr(session_control, "wait_consumed", unexpected_wait)
        monkeypatch.setattr(session_control, "request_sync", unexpected_wait)
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[9]))
            == "updated"
        )
        after = router._fetch_full_blocking(pool, sid)
        assert after[:8] == row[:8]
        assert after[9] == "print(2)\n"
        assert (
            repair.replace_script(pool, sid, "print(2)\n", repair.script_sha256(row[9]))
            == "unchanged"
        )
    assert db_conn.execute(
        "SELECT script, command, note FROM schedule_versions WHERE schedule_id=%s ORDER BY id",
        (sid,),
    ).fetchall() == [
        ("print(1)\n", "python custom.py", "initial"),
        ("print(2)\n", "python custom.py", "edit"),
    ]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == (
        [(sid,)] if enabled else []
    )
    assert db_conn.execute(
        "SELECT desired_revision, launch_count FROM schedules WHERE id=%s", (sid,)
    ).fetchone() == (1, 0)


def test_stale_hash_and_syntax_errors_do_not_write(
    db_conn: psycopg.Connection, database: Database
) -> None:
    with database.pool() as pool:
        row = router._create_blocking(pool, router.ScheduleCreate(name="stale", script="pass\n"))
        with pytest.raises(RuntimeError, match="expected old script"):
            repair.replace_script(pool, row[0], "print(2)\n", "0" * 64)
        with pytest.raises(Exception, match="syntax error"):
            repair.replace_script(pool, row[0], "def broken(\n", repair.script_sha256(row[9]))
        assert router._fetch_full_blocking(pool, row[0]) == row
    assert db_conn.execute("SELECT note FROM schedule_versions").fetchall() == [("initial",)]


def test_cli_does_not_accept_state_mutations(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        repair.main(
            [
                "1",
                "--home",
                str(tmp_path / "home"),
                "--source",
                str(tmp_path / "source"),
                "--script-file",
                "x.py",
                "--expected-sha256",
                "0" * 64,
                "--enable",
            ]
        )
    assert exc.value.code == 2


def test_utf8_digest_preserves_newlines() -> None:
    assert repair.script_sha256("pass\r\n") != repair.script_sha256("pass\n")


def test_external_file_uses_installed_runtime_and_real_writer(
    db_conn: psycopg.Connection,
    database: Database,
    db_url: str,
    tmp_path: Path,
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set AVA_TEST_INSTALLED_SOURCE to repeat this contract on an old checkout."""
    source = Path(os.environ.get("AVA_TEST_INSTALLED_SOURCE", Path(sys.prefix).parent)).resolve()
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    (home / "start-intent.json").write_text(
        json.dumps({"home": str(home), "checkout": str(source)})
    )
    publish(MaintenancePhase.STOPPED)
    journal = pause_owner.state_path().read_bytes()
    (home / "run" / "deploy-pause-owner.json").write_bytes(journal)
    (home / ".env").write_text(
        f"AVA_DB_URL={db_url}\nAVA_SERVE_GATEWAY=true\n"
        "AVA_SERVE_AGENT_RUNNER=false\nAVA_MACHINE_NAME=external-script-test\n"
    )
    with database.pool() as pool:
        row = router._create_blocking(
            pool, router.ScheduleCreate(name="external", script="pass\n", enabled=True)
        )
    external = tmp_path / "operator-tool.py"
    external.write_bytes(Path(repair.__file__).read_bytes())
    script_file = tmp_path / "prepared.py"
    script_file.write_bytes(b"raise AssertionError('the body must never run')\r\n")
    with monkeypatch.context() as child_environment:
        for key in list(os.environ):
            if key.startswith("AVA_"):
                child_environment.delenv(key)
        child_environment.setenv("AVA_HOME", str(home))
        child_environment.chdir(tmp_path)
        result = pytester.run(
            str(source / ".venv" / "bin" / "python"),
            str(external),
            str(row[0]),
            "--home",
            str(home),
            "--source",
            str(source),
            "--script-file",
            str(script_file),
            "--expected-sha256",
            repair.script_sha256(row[9]),
            timeout=30,
        )
    assert result.ret == 0, result.stderr.str()
    assert "updated script_sha256=" in result.stdout.str()
    assert (home / "run" / "deploy-pause-owner.json").read_bytes() == journal
    assert db_conn.execute(
        "SELECT enabled,status,script FROM schedules WHERE id=%s", (row[0],)
    ).fetchone() == (True, "stopped", script_file.read_bytes().decode())
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id=%s ORDER BY id", (row[0],)
    ).fetchall() == [("initial",), ("edit",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
        (row[0],)
    ]


def test_main_preserves_unknown_failure_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_file = tmp_path / "prepared.py"
    script_file.write_text("pass\n")

    def fail(**_arguments: object) -> str:
        raise RuntimeError("unexpected writer failure")

    monkeypatch.setattr(repair, "repair_script", fail)
    with pytest.raises(RuntimeError, match="unexpected writer failure"):
        repair.main(
            [
                "1",
                "--home",
                str(tmp_path),
                "--source",
                str(tmp_path),
                "--script-file",
                str(script_file),
                "--expected-sha256",
                "0" * 64,
            ]
        )
