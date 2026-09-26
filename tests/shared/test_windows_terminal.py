"""Resource contract tests; native Job/pipe evidence runs separately on Windows CI."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psutil
import pytest
from pydantic import ValidationError

from services.ava_root.wiring import WiringContext
from services.ava_root_glue.windows_terminal import TerminalBroker
from shared import paths, session_backend
from shared.native_process.ownership import OwnedProcess
from shared.root_control.ipc import ResponsePayload, encode, ok_response
from shared.windows_terminal import backend, record


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_native_transport_rejects_unbounded_deadlines_before_os_calls(
    tmp_path: Path, timeout: float
) -> None:
    from shared.root_control.windows.transport import roundtrip

    with pytest.raises(ValueError, match="finite and positive"):
        roundtrip(tmp_path / "root", b'{"verb":"status"}\n', timeout)


@pytest.fixture
def pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> record.TerminalRecord:
    monkeypatch.setattr(paths, "run_dir", lambda: tmp_path)
    birth = record.NativeBirth.capture(OwnedProcess.capture(psutil.Process()))
    value = record.TerminalRecord(
        name="test-terminal",
        domain="a" * 32,
        generation=None,
        state="pending",
        root=birth,
        launcher=birth,
        started_at=time.time(),
        command="test",
        cwd=str(tmp_path),
    )
    path = record.record_path(value.name)
    path.parent.mkdir()
    path.write_text(value.model_dump_json())
    return value


@pytest.mark.parametrize("force", [False, True])
def test_owner_loss_retains_terminal_custody_and_refuses_both_stop_modes(
    pending: record.TerminalRecord, force: bool
) -> None:
    before = record.record_path(pending.name).read_bytes()
    terminal = backend.WindowsTerminalBackend()
    assert terminal.list_sessions() == [pending.name]
    with pytest.raises(RuntimeError, match="custody requires reconciliation"):
        terminal.kill_session(pending.name, graceful=not force)
    assert record.record_path(pending.name).read_bytes() == before


def test_closed_record_requires_a_complete_job_receipt(pending: record.TerminalRecord) -> None:
    with pytest.raises(ValidationError, match="empty Job receipt"):
        record.TerminalRecord.model_validate(
            pending.model_dump()
            | {
                "owner": pending.root,
                "state": "closed",
                "closed_at": time.time(),
            }
        )


def test_pipe_peer_must_be_the_exact_recorded_owner(
    pending: record.TerminalRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = record.TerminalRecord.model_validate(
        pending.model_dump()
        | {
            "state": "running",
            "owner": pending.root,
            "target": pending.root,
        }
    )
    record.record_path(running.name).write_text(running.model_dump_json())
    assert running.owner is not None
    owner_pid = running.owner.pid

    def _fake_roundtrip(*_args: object) -> tuple[bytes, int]:
        return encode(ok_response(running.model_dump(mode="json"))), owner_pid + 1

    monkeypatch.setattr(backend, "roundtrip", _fake_roundtrip)
    with pytest.raises(RuntimeError, match="recorded native owner"):
        backend.query(running)


def test_terminal_birth_has_no_local_spawn_fallback(
    pending: record.TerminalRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> ResponsePayload:
        raise RuntimeError("root unavailable")

    monkeypatch.setattr(backend.RootClient, "resource", unavailable)
    with pytest.raises(RuntimeError, match="root unavailable"):
        backend.WindowsTerminalBackend().new_session(
            "another", "echo test", Path(pending.cwd), env={}
        )
    assert record.read("another") is None


def test_windows_terminal_dispatch_does_not_change_agent_process_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_backend, "IS_WINDOWS", True)
    monkeypatch.setattr(session_backend, "_shell_backend", None)
    monkeypatch.setattr(session_backend, "_backend", None)
    assert isinstance(session_backend.get_shell_backend(), backend.WindowsTerminalBackend)
    assert isinstance(session_backend.get_backend(), session_backend.WinprocSessionBackend)


async def test_root_shutdown_waits_for_earlier_terminal_admission(
    pending: record.TerminalRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.ava_root_glue import windows_terminal as broker_module

    started, release = asyncio.Event(), asyncio.Event()

    async def controlled_thread(_function: object, _request: object) -> record.TerminalRecord:
        started.set()
        await release.wait()
        return pending

    monkeypatch.setattr(broker_module.asyncio, "to_thread", controlled_thread)
    context = cast("WiringContext", SimpleNamespace(resource_handlers={}))
    broker = TerminalBroker(context)
    broker.start()
    birth = asyncio.create_task(
        broker.request(
            {
                "name": pending.name,
                "command": pending.command,
                "cwd": pending.cwd,
                "env": {},
            }
        )
    )
    await started.wait()
    stop = asyncio.create_task(broker.stop())
    await asyncio.sleep(0)
    assert not stop.done()
    release.set()
    await birth
    await stop
    with pytest.raises(RuntimeError, match="admission is closed"):
        await broker.request({"name": "next", "command": "x", "cwd": pending.cwd, "env": {}})
