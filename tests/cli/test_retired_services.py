"""Retired restarter cleanup uses real private session identities and signals."""

from dataclasses import replace
from pathlib import Path

import pytest

from cli.commands._retired_services import stop_retired_services
from shared.session_backend import PosixProcSessionBackend
from shared.session_record import SessionRecord
from tests.cli.test_maintenance_stop import Launcher, forbidden
from tests.cli.test_maintenance_stop import home as home
from tests.cli.test_maintenance_stop import launch as launch
from tests.cli.test_maintenance_stop import pytestmark as pytestmark

_IGNORE = (
    "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    "print('ready',flush=True); time.sleep(60)"
)


def test_retired_restarter_exits_normally_without_touching_other_sessions_or_home(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = home / "restarter-exited"
    retired = launch(
        "ava-restarter",
        "import signal,time,pathlib; "
        f"signal.signal(signal.SIGTERM,lambda *_: (pathlib.Path({str(completed)!r})"
        ".write_text('normal'), exit(0))); print('ready',flush=True); time.sleep(60)",
    )
    native = launch("ava-agent-host", _IGNORE)
    terminal = launch("ava-agent-23-shell-1", _IGNORE)
    sibling = launch("sibling-restarter", _IGNORE)
    record_path = home / "run/sessions/sibling-restarter.json"
    record = SessionRecord.read(record_path)
    assert record is not None
    sibling_home = home.with_name(home.name + "-other-unit")
    record.write(sibling_home / "run/sessions/ava-restarter.json")
    record_path.unlink()
    monkeypatch.setattr(PosixProcSessionBackend, "kill_session", forbidden)

    stop_retired_services(3)

    assert retired.wait(timeout=1) == 0
    assert completed.read_text() == "normal"
    assert native.poll() is terminal.poll() is sibling.poll() is None
    stop_retired_services(1)  # A completed cleanup remains an idempotent no-op.


def test_retired_restarter_timeout_preserves_the_survivor(
    launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    retired = launch("ava-restarter", _IGNORE)
    monkeypatch.setattr(PosixProcSessionBackend, "kill_session", forbidden)

    with pytest.raises(TimeoutError, match="kept its hold"):
        stop_retired_services(0.15)

    assert retired.poll() is None


def test_changed_retired_identity_refuses_without_signalling(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    retired = launch("ava-restarter", _IGNORE)
    path = home / "run/sessions/ava-restarter.json"
    record = SessionRecord.read(path)
    assert record is not None
    replace(record, create_time=record.create_time + 1, starttime=None).write(path)
    monkeypatch.setattr(PosixProcSessionBackend, "graceful_signal", forbidden)
    monkeypatch.setattr(PosixProcSessionBackend, "kill_session", forbidden)

    with pytest.raises(RuntimeError, match="identity changed"):
        stop_retired_services(1)

    assert retired.poll() is None


def test_dead_retired_record_does_not_block_start(home: Path, launch: Launcher) -> None:
    retired = launch("ava-restarter", "print('ready',flush=True)")
    retired.wait(timeout=2)
    assert (home / "run/sessions/ava-restarter.json").is_file()

    stop_retired_services(1)
