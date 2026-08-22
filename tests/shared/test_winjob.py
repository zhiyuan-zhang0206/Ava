"""Off-box contract tests for the Windows Job Object wrapper."""

from __future__ import annotations

import ctypes
import stat
from pathlib import Path
from typing import Any

import pytest

from shared import winjob


class _FakeKernel32:
    def __init__(self, *, assign_ok: bool = True, set_ok: bool = True) -> None:
        self.set_flags: list[int] = []
        self.assigned: list[tuple[int, int]] = []
        self.closed: list[int] = []
        self.assign_ok = assign_ok
        self.set_ok = set_ok

    def CreateJobObjectW(self, _security: object, _name: object) -> int:  # noqa: N802
        return 91

    def SetInformationJobObject(  # noqa: N802
        self, handle: object, _kind: int, info: Any, _size: int
    ) -> int:
        limits = ctypes.cast(info, ctypes.POINTER(winjob._ExtendedLimitInformation)).contents
        self.set_flags.append(int(limits.BasicLimitInformation.LimitFlags))
        return int(self.set_ok)

    def AssignProcessToJobObject(  # noqa: N802
        self, job_handle: Any, process_handle: Any
    ) -> int:
        self.assigned.append((int(job_handle.value), int(process_handle.value)))  # type: ignore[attr-defined]
        return int(self.assign_ok)

    def CloseHandle(self, handle: Any) -> int:  # noqa: N802
        self.closed.append(int(handle.value))  # type: ignore[attr-defined]
        return 1


class _FakePopen:
    _handle = 123


def test_job_sets_owned_tree_limits_assigns_by_handle_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    job = winjob.WindowsJob.create()
    job.assign(_FakePopen())  # type: ignore[arg-type]
    job.close()
    job.close()

    assert api.set_flags == [
        winjob._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | winjob._JOB_OBJECT_LIMIT_BREAKAWAY_OK
    ]
    assert api.set_flags[0] & 0x00001000 == 0  # never SILENT_BREAKAWAY_OK
    assert api.assigned == [(91, 123)]
    assert api.closed == [91]


def test_job_limit_structures_match_windows_x64_abi() -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(winjob._BasicLimitInformation) == 64
        assert ctypes.sizeof(winjob._ExtendedLimitInformation) == 144


def test_set_limits_failure_preserves_error_before_closing_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32(set_ok=False)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)
    monkeypatch.setattr(winjob, "_get_last_error", lambda: 1234)

    with pytest.raises(OSError) as caught:
        winjob.WindowsJob.create()

    assert caught.value.errno == 1234
    assert api.closed == [91]


def test_job_assign_failure_is_loud_and_handle_remains_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32(assign_ok=False)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    def _error(action: str, _code: int | None = None) -> OSError:
        return OSError(action)

    monkeypatch.setattr(winjob, "_last_error", _error)
    job = winjob.WindowsJob.create()

    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        job.assign(_FakePopen())  # type: ignore[arg-type]

    assert not job.closed
    job.close()
    assert api.closed == [91]


def test_attach_gate_is_exclusive_0600_and_sets_process_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = tmp_path / "attach.job-ready"
    monkeypatch.setattr(winjob._exec_job_state, "attached", False)

    winjob.publish_parent_job_gate(gate)
    assert stat.S_IMODE(gate.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        winjob.publish_parent_job_gate(gate)

    assert not winjob.in_attached_exec_job()
    winjob.await_parent_job_gate(str(gate))
    assert winjob.in_attached_exec_job()
