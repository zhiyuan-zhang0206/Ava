"""A zombie cannot execute; uncertain or live shells retain their records."""

import os
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from shared.pty_sessions import cli
from shared.pty_sessions._paths import record_path, socket_path, transcript_path
from shared.session_record import SessionRecord


def _mock_process(create_time: float) -> Mock:
    """A psutil.Process stand-in whose stable identity reading is `create_time`."""
    proc = Mock()
    proc.create_time.return_value = create_time
    proc._proc.create_time.return_value = create_time
    return proc


@pytest.mark.parametrize("starttime", [None, 123])
@pytest.mark.parametrize("state", ["zombie", "running", "unreadable"])
def test_lazy_sweep_requires_proven_shell_exit(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, starttime: int | None, state: str
) -> None:
    """List hides stale observations but deletes artifacts only with exit proof."""
    name = "ava-test-zombie-sweep"
    record = SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=1.0,
        starttime=starttime,
    )
    record.write(record_path(name))
    socket_path(name).touch()
    proc = _mock_process(1.0)
    proc.is_running.return_value = True
    if state == "unreadable":
        proc.status.side_effect = psutil.AccessDenied(record.pid)
    else:
        proc.status.return_value = (
            psutil.STATUS_ZOMBIE if state == "zombie" else psutil.STATUS_RUNNING
        )

    def inspect_process(_pid: int) -> Mock:
        return proc

    def matching_identity(_self: SessionRecord, _pid: int) -> bool:
        return True

    monkeypatch.setattr(cli.psutil, "Process", inspect_process)
    monkeypatch.setattr(SessionRecord, "identifies", matching_identity)

    listed = cli.live_sessions(prefix=name)
    assert bool(listed) is (state == "running")
    assert record_path(name).exists() is (state != "zombie"), "zombie record must be swept"
    assert socket_path(name).exists() is (state != "zombie")


def test_sweep_defers_while_record_lock_is_held(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep side of the issue #2063 mutex: a sweep never unlinks while
    another holder owns the pty record lock — it skips (the dead record
    survives one scan), and the next scan sweeps it once the lock is free."""
    from shared.platform import file_lock
    from shared.pty_sessions import records as records_module
    from shared.pty_sessions._paths import records_lock_path

    name = "ava-test-locked-sweep"
    record = SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=1.0,
        starttime=123,
    )
    record.write(record_path(name))
    socket_path(name).touch()
    proc = _mock_process(1.0)
    proc.is_running.return_value = True
    proc.status.return_value = psutil.STATUS_ZOMBIE

    def inspect_process(_pid: int) -> Mock:
        return proc

    monkeypatch.setattr(cli.psutil, "Process", inspect_process)
    monkeypatch.setattr(records_module, "_SWEEP_LOCK_TIMEOUT_S", 0.1)

    with file_lock(records_lock_path(), timeout_s=1):
        listed = cli.live_sessions(prefix=name)
        assert listed == {}
        assert record_path(name).exists(), "sweep must skip while the lock is held"
        assert socket_path(name).exists()

    listed = cli.live_sessions(prefix=name)
    assert listed == {}
    assert not record_path(name).exists(), "a later scan sweeps once the lock is free"
    assert not socket_path(name).exists()


def test_sweep_cannot_unlink_a_record_written_under_the_lock(  # noqa: PLR0915 -- one synchronized sweep-vs-write race proof; each step is one setup or timing statement
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue #2063 TOCTOU: a sweep that read the previous incarnation's
    dead record must not unlink the record a concurrent ``new`` wrote
    meanwhile. Record creation takes the same pty record lock, so the fresh
    record always survives — the sweep either finishes before the write, or
    re-reads the fresh live record and retains it."""
    import threading

    from shared.platform import file_lock
    from shared.pty_sessions import records as records_module
    from shared.pty_sessions._paths import records_lock_path, write_record

    name = "ava-test-sweep-race"
    old_pid, fresh_pid = 1001, 1002
    record = SessionRecord(
        pid=old_pid,
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=1.0,
        starttime=123,
    )
    record.write(record_path(name))
    socket_path(name).touch()

    def inspect_process(pid: int) -> Mock:
        proc = _mock_process(2.0)
        if pid == old_pid:
            proc.is_running.return_value = True
            proc.status.return_value = psutil.STATUS_ZOMBIE
        else:
            proc.is_running.return_value = True
            proc.status.return_value = psutil.STATUS_RUNNING
        return proc

    def matching_identity(_self: SessionRecord, _pid: int) -> bool:
        return True

    monkeypatch.setattr(cli.psutil, "Process", inspect_process)
    monkeypatch.setattr(SessionRecord, "identifies", matching_identity)
    monkeypatch.setattr(records_module, "_SWEEP_LOCK_TIMEOUT_S", 10.0)

    fresh = SessionRecord(
        pid=fresh_pid,
        create_time=2.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=2.0,
        starttime=456,
    )
    entered = threading.Event()  # the sweep read the old record, about to unlink
    written = threading.Event()  # the concurrent writer has written the fresh record

    def write_fresh() -> None:
        # What the new session host does under the lock (its bind + record
        # write); the write is the file-level mutation the sweep must not race.
        with file_lock(records_lock_path(), timeout_s=10):
            write_record(
                record_path(name),
                fresh,
                host_pid=os.getpid(),
                host_create_time=2.0,
            )
            written.set()

    orig_reapable = records_module._record_reapable

    def racing_reapable(path: Path, rec: SessionRecord) -> tuple[bool, str]:
        result = orig_reapable(path, rec)
        if rec.started_at == 1.0:  # the old incarnation's record
            entered.set()
            # Let the concurrent writer attempt its write before the unlink.
            # With the lock, the writer cannot land mid-sweep; without it, the
            # sweep would unlink the fresh record right after this wait.
            written.wait(1.0)
        return result

    monkeypatch.setattr(records_module, "_record_reapable", racing_reapable)

    listed: dict[str, SessionRecord] = {}

    def run_sweep() -> None:
        listed.update(cli.live_sessions(prefix=name))

    sweeper = threading.Thread(target=run_sweep)
    sweeper.start()
    assert entered.wait(5), "the sweep never reached the unlink decision"
    writer = threading.Thread(target=write_fresh)
    writer.start()
    sweeper.join(10)
    writer.join(10)
    assert not sweeper.is_alive() and not writer.is_alive()

    # Pre-fix, the writer's fresh record landed mid-sweep and the sweep
    # unlinked it — the fresh session would vanish. Post-fix, sweep and write
    # serialize on the record lock, so the fresh record survives and reads
    # live on the next scan.
    again = cli.live_sessions(prefix=name)
    assert set(again) == {name}
    surviving = SessionRecord.read(record_path(name))
    assert surviving is not None
    assert surviving.started_at == 2.0


def test_bring_up_defers_while_record_lock_is_held(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The creation side of the issue #2063 mutex: the host's bring-up takes
    the same record lock as the sweep, so while another holder owns the lock
    it refuses with the failure exit code and never runs any bind/write work
    outside the lock."""
    from shared.platform import file_lock
    from shared.pty_sessions import launch as launch_module
    from shared.pty_sessions._paths import records_lock_path

    entered: list[object] = []

    def fake_bring_up_locked(*_args: object, **_kw: object) -> object:
        entered.append(True)
        raise AssertionError("bring-up body must not run while the lock is held")

    monkeypatch.setattr(launch_module, "_bring_up_locked", fake_bring_up_locked)
    monkeypatch.setattr(launch_module, "_BRING_UP_LOCK_TIMEOUT_S", 0.1)
    name = "ava-test-locked-bringup"
    with file_lock(records_lock_path(), timeout_s=1):
        result = launch_module._bring_up(
            name,
            str(unit_home),
            {},
            None,
            record_path(name),
            socket_path(name),
            transcript_path(name),
            None,
        )
    assert result == 1
    assert not entered
