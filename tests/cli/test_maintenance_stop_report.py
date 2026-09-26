"""Terminal deadlines preserve native survivors and their operator diagnostics."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from cli.commands import _maintenance_stop_report as report
from cli.commands import _temporary_stop as command
from shared import lifecycle_status
from shared.native_process import ownership
from shared.native_process.ownership import OwnedProcess
from shared.session_record import SessionRecord
from tests.agent.test_maintenance import WHEN
from tests.cli.test_maintenance_stop import Launcher
from tests.cli.test_maintenance_stop import home as home
from tests.cli.test_maintenance_stop import launch as launch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="real POSIX signal contract")


def _terminal(
    home: Path, process: subprocess.Popen[str], monkeypatch: pytest.MonkeyPatch
) -> OwnedProcess:
    identity = OwnedProcess.capture(psutil.Process(process.pid))
    SessionRecord(
        identity.pid, identity.birth, "private-terminal", str(home), time.time(), identity.starttime
    ).write(home / "run/pty/private-terminal.json")
    monkeypatch.setattr(
        command,
        "get_shell_backend",
        lambda: SimpleNamespace(list_sessions=lambda: ["private-terminal"]),
    )
    return identity


def test_terminal_deadline_names_survivor_and_persists_exact_inventory(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = launch(
        "private-terminal",
        "import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
    )
    identity = _terminal(home, process, monkeypatch)
    assert lifecycle_status.begin("stop")
    with pytest.raises(report.StopIncompleteError) as caught:
        command._stop_terminals(time.monotonic() + 0.25, "private-stop", WHEN)
    failure = caught.value
    assert identity.live(), "a reporting deadline must not force-kill the survivor"
    assert failure.stage == "terminals"
    assert f"pid={identity.pid}" in str(failure) and "SIG_IGN" in str(failure)
    assert len(failure.survivors) == 1
    survivor = failure.survivors[0]
    assert (survivor["pid"], survivor["birth"], survivor["starttime"]) == (
        identity.pid,
        identity.birth,
        identity.starttime,
    )
    assert survivor["role"] == "terminal" and survivor["service"] == "private-terminal"
    command._report_incomplete(failure, [("terminals", 0.25)], owns_journal=True)
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == 1
    assert journal["result"]["stage"] == "terminals"
    assert journal["result"]["survivors"] == failure.survivors
    assert f"pid={identity.pid}" in capsys.readouterr().err


def test_report_keeps_owned_job_after_shell_exits(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    armed = home / "job-armed"
    child_code = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(armed)!r}).touch(); time.sleep(60)"
    )
    parent = launch(
        "private-terminal",
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print('ready',flush=True); time.sleep(60)",
    )
    _terminal(home, parent, monkeypatch)
    deadline = time.monotonic() + 5
    while not armed.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    children = psutil.Process(parent.pid).children()
    assert len(children) == 1
    child = OwnedProcess.capture(children[0])
    try:
        with pytest.raises(report.StopIncompleteError) as caught:
            command._stop_terminals(time.monotonic() + 0.35, "private-stop", WHEN)
        assert parent.wait(timeout=5) == -signal.SIGHUP
        assert child.live()
        assert len(caught.value.survivors) == 1
        survivor = caught.value.survivors[0]
        assert survivor["pid"] == child.pid
        assert survivor["role"] == "job" and survivor["service"] == "private-terminal"
        assert str(armed) in str(survivor["cmdline"])
    finally:
        child.send_signal(signal.SIGKILL)


def test_report_reuses_the_native_birth_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = OwnedProcess.capture(psutil.Process())
    if sys.platform == "linux":
        shifted = OwnedProcess(identity.pid, identity.birth + 3600, identity.starttime)
        assert report._identity_matches(shifted)
    else:
        shifted = OwnedProcess(identity.pid, identity.birth + 0.0001, None)
        assert not report._identity_matches(shifted)
    assert report._identity_matches(identity)


def test_unknown_identity_is_retained_without_unrelated_live_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = OwnedProcess(os.getpid(), 1.0, None)
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    assert report.live_identities([identity]) == [identity]
    survivor = report.capture_survivor(identity, service="private-terminal", role="job")
    assert survivor.pid == identity.pid and survivor.birth == identity.birth
    assert survivor.cmdline is None and survivor.ppid is None and survivor.status is None
