"""Platform-independent regressions; native ownership proof lives beside these."""

import asyncio
import contextlib
import ctypes
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psutil
import pytest

from services.ava_root.windows import console, process
from services.ava_root_glue.windows_terminal_owner import TerminalOwner
from shared import winjob
from shared.native_process import ownership as proc_tree
from shared.native_process.ownership import OwnedProcess
from shared.root_control import client
from shared.root_control.ipc import encode, ok_response
from shared.root_control.windows import transport


def test_resource_root_field_is_not_interpreted_as_status(tmp_path, monkeypatch):
    record = {"root": {"pid": 100, "birth": 42.0}, "state": "running"}
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(transport, "roundtrip", lambda *_: (encode(ok_response(record)), 100))
    assert client.RootClient(tmp_path / "root").resource("terminal.start", {}) == ok_response(
        record
    )


@pytest.mark.parametrize(
    "root",
    [None, {"pid": 101, "create_time": 42.0, "starttime": None}],
)
def test_status_still_requires_native_pipe_peer_identity(tmp_path, monkeypatch, root):
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        transport, "roundtrip", lambda *_: (encode(ok_response({"root": root})), 100)
    )
    with pytest.raises(client.RootClientError):
        client.RootClient(tmp_path / "root").status()


async def test_terminal_monitor_rechecks_completion_after_waiting_for_close_lock():
    from services.ava_root.custody import ServiceCustody
    from shared.windows_terminal.record import TerminalRecord

    class Job:
        closed = False

        def active_processes(self):
            assert not self.closed, "monitor queried a closed native Job"
            return 0

    async def waited():
        return 0

    job = Job()
    fake_process = cast(
        "process.ApplicationProcess",
        SimpleNamespace(job=cast("winjob.WindowsJob", job), wait=waited),
    )
    owner = TerminalOwner(cast("TerminalRecord", None), fake_process, cast("ServiceCustody", None))
    await owner._lock.acquire()
    observer = asyncio.create_task(owner.observe())
    await asyncio.sleep(0)  # Observer has entered its loop and is waiting for close.
    job.closed = True
    owner.done.set()
    owner._lock.release()
    await observer
    await owner._waiter


@pytest.mark.parametrize("error", [5, 6, 87])
def test_only_native_no_console_observation_is_deferred(tmp_path, monkeypatch, error):
    api = SimpleNamespace(FreeConsole=lambda: True, AttachConsole=lambda _pid: False)
    monkeypatch.setattr(console, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(console, "_custody", lambda *_: (b"captured", {100: 42.0}))
    monkeypatch.setattr(
        console,
        "ctypes",
        SimpleNamespace(WinDLL=lambda *_a, **_kw: api, get_last_error=lambda: error),
    )
    if error == 6:
        assert console.deliver(tmp_path / "custody", "digest", 100, 1.0) == {
            "delivered_pids": [],
            "consoleless_pid": 100,
        }
    else:
        with pytest.raises(OSError, match=f"Win32 error {error}"):
            console.deliver(tmp_path / "custody", "digest", 100, 1.0)


async def test_consoleless_live_member_never_becomes_a_closure_receipt(tmp_path, monkeypatch):
    from services.ava_root.custody import ServiceCustody

    member = OwnedProcess.capture(psutil.Process())
    job = cast("winjob.WindowsJob", SimpleNamespace(active_processes=lambda: 1))
    application = process.ApplicationProcess(member.pid, 123, job, contextlib.ExitStack())
    monkeypatch.setattr(application, "members", lambda: {member})
    monkeypatch.setattr(
        process,
        "run_job_process",
        lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, json.dumps({"delivered_pids": [], "consoleless_pid": member.pid}), ""
        ),
    )
    custody = ServiceCustody(tmp_path / "run", "application")
    with pytest.raises(TimeoutError, match="Job still has members"):
        await application.close(custody, timeout=0.1, force=False)
    assert json.loads(custody.path.read_text())["processes"][0]["pid"] == member.pid


@pytest.mark.parametrize("wait_result,expected", [(0, False), (258, True)])
def test_windows_liveness_observes_exit_signal_with_retained_pid(
    monkeypatch, wait_result, expected
):
    closed = []
    api = SimpleNamespace(
        OpenProcess=lambda *_: 123,
        WaitForSingleObject=lambda *_: wait_result,
        CloseHandle=lambda handle: closed.append(handle.value) or 1,
    )
    retained = SimpleNamespace(
        pid=100, create_time=lambda: 42.0, status=lambda: psutil.STATUS_RUNNING
    )
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="win32"))
    # The Linux start-ticks path must not leak into this Windows simulation.
    monkeypatch.setattr(proc_tree, "_start_ticks", lambda _pid: None)
    monkeypatch.setattr(proc_tree.psutil, "Process", lambda _pid: retained)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)
    assert OwnedProcess(100, 42.0, None).live() is expected
    assert closed == [123]


def test_windows_liveness_binds_birth_while_native_handle_is_open(monkeypatch):
    closed = []
    api = SimpleNamespace(
        OpenProcess=lambda *_: 123,
        WaitForSingleObject=lambda *_: 258,
        CloseHandle=lambda handle: closed.append(handle.value) or 1,
    )

    def birth():
        assert not closed
        return 43.0  # PID was reused before OpenProcess acquired this new object.

    retained = SimpleNamespace(pid=100, create_time=birth, status=lambda: psutil.STATUS_RUNNING)
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="win32"))
    # The Linux start-ticks path must not leak into this Windows simulation.
    monkeypatch.setattr(proc_tree, "_start_ticks", lambda _pid: None)
    monkeypatch.setattr(proc_tree.psutil, "Process", lambda _pid: retained)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)
    assert not OwnedProcess(100, 42.0, None).live()
    assert closed == [123]


@pytest.mark.parametrize("open_result,error", [(0, 5), (123, 6)])
def test_windows_liveness_keeps_native_observation_errors_unknown(monkeypatch, open_result, error):
    closed = []
    api = SimpleNamespace(
        OpenProcess=lambda *_: open_result,
        WaitForSingleObject=lambda *_: 0xFFFFFFFF,
        CloseHandle=lambda handle: closed.append(handle.value) or 1,
    )
    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)
    monkeypatch.setattr(winjob, "_get_last_error", lambda: error)
    with pytest.raises(OSError, match=f"Win32 error {error}"):
        OwnedProcess(100, 42.0, None).live()
    assert closed == ([123] if open_result else [])


async def test_job_zero_count_still_waits_for_every_observed_native_exit(tmp_path, monkeypatch):
    from services.ava_root.custody import ServiceCustody

    native_exited = False
    observed = OwnedProcess(100, 42.0, None)
    job = cast(
        "winjob.WindowsJob", SimpleNamespace(active_processes=lambda: 0, terminate=lambda: None)
    )
    application = process.ApplicationProcess(100, 123, job, contextlib.ExitStack())
    snapshots = iter([{observed}, set()])
    monkeypatch.setattr(application, "members", lambda: next(snapshots))
    monkeypatch.setattr(OwnedProcess, "live", lambda _: not native_exited)

    async def finish_exit(_delay):
        nonlocal native_exited
        native_exited = True

    monkeypatch.setattr(process, "asyncio", SimpleNamespace(sleep=finish_exit))
    custody = ServiceCustody(tmp_path / "run", "application")
    # Sleep is stubbed, so the deadline only has to outlast two fsynced custody
    # writes; 0.1 s did not on a loaded CI runner.
    await application.close(custody, timeout=5.0, force=True)
    assert native_exited
    assert json.loads(custody.path.read_text())["processes"] == [
        {"pid": 100, "birth": 42.0, "starttime": None}
    ]


def _membership_api(monkeypatch, snapshots: list[tuple[int, list[int]]]) -> list[int]:
    """Serve (assigned, listed PIDs) snapshots; the last one repeats."""
    queries: list[int] = []

    def query(_handle, info_class, buffer, _length, _returned):
        assert info_class == 3
        assigned, pids = snapshots[min(len(queries), len(snapshots) - 1)]
        queries.append(assigned)
        header = (ctypes.c_uint32 * 2)(assigned, len(pids))
        ctypes.memmove(buffer, header, ctypes.sizeof(header))
        listed = (winjob._SIZE_T * len(pids))(*pids)
        ctypes.memmove(ctypes.addressof(buffer) + 8, listed, ctypes.sizeof(listed))
        return 1

    monkeypatch.setattr(
        winjob, "_kernel32", lambda: SimpleNamespace(QueryInformationJobObject=query)
    )
    return queries


def test_job_membership_requeries_a_member_in_transition(monkeypatch):
    # Native CI observed the kernel counting one more assigned process than it
    # listed while a member was being torn down; the next query agreed.
    queries = _membership_api(monkeypatch, [(3, [11, 12]), (2, [11, 12])])
    assert winjob.WindowsJob(7).member_pids() == {11, 12}
    assert queries == [3, 2]


def test_job_membership_that_never_settles_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(winjob, "_MEMBERSHIP_SETTLE_S", 0.05)
    queries = _membership_api(monkeypatch, [(3, [11, 12])])
    with pytest.raises(RuntimeError, match="3 assigned, 2 listed"):
        winjob.WindowsJob(7).member_pids()
    assert len(queries) > 1


def _move_api(monkeypatch, transient: list[int], lasting: int | None = None) -> list[int]:
    """MoveFileExW fails with each transient Win32 code, then with ``lasting`` or renames."""
    from shared.root_control.windows import storage

    attempts: list[int] = []
    error = [0]

    def move(source, target, flags):
        attempts.append(flags)
        if transient or lasting is not None:
            error[0] = transient.pop(0) if transient else cast(int, lasting)
            return 0
        Path(source).replace(target)
        return 1

    monkeypatch.setattr(storage, "_kernel32", lambda: SimpleNamespace(MoveFileExW=move))
    monkeypatch.setattr(storage, "_get_last_error", lambda: error[0])
    return attempts


def test_custody_publication_rides_out_a_reader_holding_the_record(tmp_path, monkeypatch):
    from shared.root_control.windows import storage

    # Native CI: a root readiness poll holding the record open made the owner's
    # replacing rename fail with ERROR_ACCESS_DENIED, and the owner died.
    attempts = _move_api(monkeypatch, [5, 32])
    target = tmp_path / "record.json"
    target.write_text("old")
    storage.publish(target, "new")
    assert target.read_text() == "new"
    assert attempts == [9, 9, 9]  # write-through | replace-existing, every attempt
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("code,retried", [(2, False), (183, False), (5, True)])
def test_custody_publication_refuses_other_errors_and_a_lasting_conflict(
    tmp_path, monkeypatch, code, retried
):
    from shared.root_control.windows import storage

    monkeypatch.setattr(storage, "_SHARING_WAIT_S", 0.05)
    attempts = _move_api(monkeypatch, [], lasting=code)
    target = tmp_path / "record.json"
    target.write_text("old")
    with pytest.raises(OSError, match=f"Win32 error {code}"):
        storage.publish(target, "new")
    assert target.read_text() == "old"
    assert (len(attempts) > 1) is retried
    assert list(tmp_path.iterdir()) == [target]


def test_record_read_rides_out_a_replacement_in_progress(tmp_path, monkeypatch):
    from shared.root_control.windows import storage

    # Native CI: the root read the record while the owner replaced it and the
    # terminal.start handler failed with PermissionError ("internal error").
    record = tmp_path / "record.json"
    record.write_bytes(b"current")
    denials = [PermissionError(13, "Access is denied")]
    read_bytes = Path.read_bytes

    def contended(path: Path) -> bytes:
        if denials:
            raise denials.pop()
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", contended)
    assert storage.read_published(record) == b"current"
    assert not denials


def test_record_read_propagates_absence_links_and_a_lasting_denial(tmp_path, monkeypatch):
    from shared.root_control.windows import storage

    monkeypatch.setattr(storage, "_SHARING_WAIT_S", 0.05)
    with pytest.raises(FileNotFoundError):
        storage.read_published(tmp_path / "absent.json")
    record = tmp_path / "record.json"
    record.write_bytes(b"current")
    link = tmp_path / "link.json"
    link.symlink_to(record)
    with pytest.raises(RuntimeError, match="must not be a link"):
        storage.read_published(link)

    def denied(_path: Path) -> bytes:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        storage.read_published(record)
