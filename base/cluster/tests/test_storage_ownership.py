"""Native PostgreSQL ownership: a receipt, never a pidfile, a listener or a prior boot, is what authorizes a stop, restart or signal."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from base.cluster import ownership
from base.cluster import postgres as pg
from base.native_process.ownership import OwnedProcess


@pytest.fixture
def pg_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PostgreSQL data directory inside a private home.

    Custody keeps its receipt in `<data>/../run`, as a home's `pgdata` sits beside its `run`.
    A data directory directly under the session's shared base temp directory would hand every
    other test the receipt this one writes.
    """
    data = tmp_path_factory.mktemp("home") / "pgdata"
    data.mkdir()
    return data


def test_unrelated_listener_is_not_owned_by_a_valid_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = SimpleNamespace(pid=123, live=lambda: True)
    monkeypatch.setattr(ownership, "strict_listeners_on", lambda _port: [123, 456])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(ownership, "capture_tree", lambda _owner: [owner])  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    with pytest.raises(RuntimeError, match="does not belong"):
        ownership.require_listener(cast("ownership.OwnedProcess", owner), 15433)


def test_postmaster_pidfile_cannot_supply_missing_native_receipt(pg_data: Path) -> None:
    (pg_data / "postmaster.pid").write_text(f"123\n{pg_data}\n100\n")
    with pytest.raises(RuntimeError, match="no native launch receipt"):
        ownership.postgres(pg_data)


def _pg_receipt(data: Path, *, state: str = "captured") -> pg.Receipt:
    if sys.platform == "win32":
        pytest.skip("owned PostgreSQL is POSIX-only")
    info = data.stat()
    values = {
        "data": str(data.resolve()),
        "directory": [info.st_dev, info.st_ino],
        "port": 15433,
        "platform": sys.platform,
        "boot_id": pg._boot(),
        "state": state,
        "owner": None
        if state in {"pending", "not-started"}
        else {
            "pid": 123,
            "create_time": 100.0,
            "starttime": 456,
        },
    }
    import json

    return pg.Receipt.model_validate_json(json.dumps(values))


def test_pending_postgres_is_never_recovered_from_a_pidfile(pg_data: Path) -> None:
    pg._write(pg_data, _pg_receipt(pg_data, state="pending"))
    with pytest.raises(RuntimeError, match="unresolved"):
        pg.observe(pg_data)


def test_postgres_birth_uses_ticks_despite_wall_clock_drift(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _pg_receipt(pg_data)
    pg._write(pg_data, receipt)
    (pg_data / "postmaster.pid").write_text(f"123\n{pg_data}\n987654321\n15433\n")
    process = SimpleNamespace(
        cmdline=lambda: ["postgres", "-D", str(pg_data)],
        name=lambda: "postgres",
        cwd=lambda: str(pg_data),
    )
    monkeypatch.setattr(pg.psutil, "Process", Mock(return_value=process))
    monkeypatch.setattr(
        pg.OwnedProcess, "capture", Mock(return_value=OwnedProcess(123, 9000.0, 456))
    )
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=True))
    monkeypatch.setattr(pg.os, "getsid", Mock(return_value=123))
    assert pg.observe(pg_data) == receipt.process()
    monkeypatch.setattr(
        pg.OwnedProcess, "capture", Mock(return_value=OwnedProcess(123, 100.0, 457))
    )
    with pytest.raises(RuntimeError, match="captured PostgreSQL"):
        pg.observe(pg_data)


@pytest.mark.parametrize("change", ["directory", "pidfile", "pid", "port"])
def test_postgres_rejects_changed_file_binding(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    receipt = _pg_receipt(pg_data)
    record = pg_data / "postmaster.pid"
    record.write_text(f"123\n{pg_data}\n100\n15433\n")
    inode, header = pg._pidfile(pg_data, receipt)
    ready = receipt.model_copy(update={"state": "ready", "pidfile": inode, "header": header})
    if change == "pidfile":
        record.rename(pg_data / "original.pid")
        record.write_text("\n".join(header) + "\n")
    elif change == "directory":
        record.write_text(f"123\n{pg_data / 'foreign'}\n100\n15433\n")
    elif change == "pid":
        record.write_text(f"456\n{pg_data}\n100\n15433\n")
    else:
        record.write_text(f"123\n{pg_data}\n100\n15434\n")
    with pytest.raises(RuntimeError, match="pidfile"):
        pg._pidfile(pg_data, ready)


def test_prior_boot_never_donates_reused_pid_authority(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _pg_receipt(pg_data).model_copy(update={"boot_id": "prior-boot"})
    pg._write(pg_data, receipt)
    monkeypatch.setattr(
        pg.OwnedProcess, "live", Mock(side_effect=AssertionError("old boot PID observed"))
    )
    monkeypatch.setattr(pg, "_require_closed", Mock(return_value=None))
    assert pg.observe(pg_data) is None


@pytest.mark.parametrize("name", ["postgres", "archive-command"])
def test_dead_postmaster_with_surviving_worker_refuses_restart(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    pg._write(pg_data, _pg_receipt(pg_data))
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=False))
    worker = SimpleNamespace(
        info={"pid": 124},
        name=lambda: name,
        pid=124,
        uids=lambda: SimpleNamespace(real=os.getuid()),
        status=lambda: "running",
        cwd=lambda: str(pg_data),
        cmdline=lambda: ["postgres: checkpoint"],
    )
    monkeypatch.setattr(pg.psutil, "process_iter", Mock(return_value=[worker]))
    monkeypatch.setattr(pg.os, "getsid", Mock(return_value=123))
    with pytest.raises(RuntimeError, match="descendants"):
        pg.observe(pg_data)


def test_closed_postgres_with_unknown_scan_refuses_restart(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg._write(pg_data, _pg_receipt(pg_data))
    monkeypatch.setattr(pg.OwnedProcess, "live", Mock(return_value=False))

    def unknown(_data: Path, _receipt: pg.Receipt | None) -> None:
        raise PermissionError("native metadata unavailable")

    monkeypatch.setattr(pg, "_require_closed", unknown)
    with pytest.raises(PermissionError):
        pg.observe(pg_data)


def test_failed_pg_exec_is_not_started_but_ambiguous_spawn_stays_pending(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _pg_receipt(pg_data, state="pending")
    with pytest.raises(FileNotFoundError):
        pg._spawn(pg_data, pending, [str(pg_data / "missing-postgres")], {})
    receipt = pg._read(pg_data)
    assert receipt is not None and receipt.state == "not-started"

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pg.subprocess, "Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pg._spawn(pg_data, pending, ["postgres"], {})
    receipt = pg._read(pg_data)
    assert receipt is not None and receipt.state == "pending"


def test_real_cancellation_after_pg_spawn_retains_native_receipt(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", actual(argv, **kwargs))
        children.append(child)
        os.kill(os.getpid(), signal.SIGINT)
        return child

    monkeypatch.setattr(pg.subprocess, "Popen", spawn)
    try:
        with pytest.raises(KeyboardInterrupt, match="admission interrupted"):
            pg._spawn(
                pg_data,
                _pg_receipt(pg_data, state="pending"),
                [sys.executable, "-c", "import time; time.sleep(30)"],
                {},
            )
        receipt = pg._read(pg_data)
        assert receipt is not None and receipt.state == "captured"
        assert receipt.process().pid == children[0].pid and receipt.process().live()
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_retained_postgres_cannot_signal_replacement(
    pg_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (pg_data.parent / "run").mkdir(exist_ok=True)
    monkeypatch.setattr(pg, "observe", Mock(return_value=OwnedProcess(123, 100.0, 457)))
    monkeypatch.setattr(
        pg.OwnedProcess, "send_signal", Mock(side_effect=AssertionError("foreign signal"))
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        pg.start(pg_data, 15433, [], {}, ready=lambda: True, expected=OwnedProcess(123, 100.0, 456))
