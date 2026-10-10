"""What a terminal closure leaves: survivors keep their native identity and diagnostics.

A surviving known shell retains the failure inventory. Known job leftovers are
reported without blocking best-effort terminal closure; stubbed outcomes lock
the consumer distinction without claiming complete descendant ownership.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import psutil
import pytest

from base.deploy.lifecycle import status_journal
from base.native_process import ownership
from base.native_process.ownership import OwnedProcess
from base.sessions.posixproc import process_group_has_live_members
from base.sessions.pty import closure
from base.sessions.record import SessionRecord
from cli.commands.lifecycle import _maintenance_stop_report as report
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import service_stop as stop
from cli.commands.lifecycle.tests.stop_support import (
    Launcher,
    closed_session,
    identity_of,
    launched_processes,
    stub_closure,
)
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop_support import written as written
from tests.components.agent.test_maintenance import WHEN

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="real POSIX signal contract"),
    pytest.mark.usefixtures("written"),
]


def test_terminal_survivor_names_itself_and_persists_exact_inventory(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = launch(
        "private-terminal",
        term="ignore",
        ignore_hangup=True,
    )
    identity = identity_of(process)
    stub_closure(
        monkeypatch,
        closure.Outcome(survivors=(closure.Survivor("private-terminal", identity, "terminal"),)),
    )
    assert status_journal.begin("stop")
    with pytest.raises(report.StopIncompleteError) as caught:
        stop.close_terminals(time.monotonic() + 0.25, "private-stop", WHEN, direct_db=False)
    failure = caught.value
    assert identity.live(), "the survivor outlived its SIGKILL"
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


def test_report_keeps_known_job_diagnostic_of_a_closed_session(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The session's shell is gone but a job outlived the SIGKILL: the report names the
    known job and its session without failing stop."""
    armed = home / "job-armed"
    parent = launch("private-terminal", child_armed=armed)
    deadline = time.monotonic() + 5
    while not armed.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    children = psutil.Process(parent.pid).children()
    assert len(children) == 1
    child = OwnedProcess.capture(children[0])
    stub_closure(
        monkeypatch,
        closure.Outcome(
            closed=(closed_session("private-terminal", ((child.pid, "python"),)),),
            survivors=(closure.Survivor("private-terminal", child, "job"),),
        ),
    )
    stop.close_terminals(time.monotonic() + 0.35, "private-stop", WHEN, direct_db=False)
    assert child.live()
    diagnostic = capsys.readouterr().err
    assert f"pid={child.pid}" in diagnostic
    assert "private-terminal" in diagnostic
    assert "inspect the process" in diagnostic


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


@pytest.mark.parametrize(
    ("term", "ignore_hangup", "sent", "result"),
    [
        ("default", False, signal.SIGTERM, -signal.SIGTERM),
        ("default", False, signal.SIGHUP, -signal.SIGHUP),
        ("exit", False, signal.SIGTERM, 0),
        ("ignore", False, signal.SIGTERM, None),
        ("ignore", True, signal.SIGHUP, None),
    ],
)
def test_literal_session_installs_requested_signal_behavior(
    home: Path,
    term: Literal["default", "exit", "ignore"],
    ignore_hangup: bool,
    sent: int,
    result: int | None,
) -> None:
    with launched_processes(home) as create:
        process = create("signal-behavior", term=term, ignore_hangup=ignore_hangup)
        record = SessionRecord.read(home / "run/sessions/signal-behavior.json")
        assert record is not None and record.pid == record.pgid == process.pid
        assert record.create_time == psutil.Process(process.pid).create_time()
        assert os.getsid(process.pid) == process.pid
        process.send_signal(sent)
        if result is None:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
        else:
            assert process.wait(timeout=5) == result
    assert not process_group_has_live_members(process.pid)
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_literal_child_is_armed_in_the_owned_group_and_cleanup_preserves_failure(
    home: Path,
) -> None:
    armed = home / "child-armed"
    original = ValueError("test body failed after child readiness")
    parent: subprocess.Popen[str] | None = None
    with pytest.raises(ValueError) as raised, launched_processes(home) as create:
        parent = create("child-session", child_armed=armed)
        deadline = time.monotonic() + 5
        while not armed.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        children = psutil.Process(parent.pid).children()
        assert len(children) == 1
        child = children[0]
        assert os.getpgid(child.pid) == os.getsid(child.pid) == parent.pid
        child.send_signal(signal.SIGTERM)
        with pytest.raises(psutil.TimeoutExpired):
            child.wait(timeout=0.2)
        raise original
    assert raised.value is original
    assert parent is not None
    assert parent.returncode == -signal.SIGKILL
    assert not process_group_has_live_members(parent.pid)
    assert parent.stdout is not None and parent.stdout.closed
    assert parent.stderr is not None and parent.stderr.closed


def test_literal_launcher_rejects_executable_text_as_behavior(home: Path) -> None:
    executable_text: Any = "__import__('os')._exit(97)"
    with (
        launched_processes(home) as create,
        pytest.raises(ValueError, match="invalid fixture SIGTERM behavior"),
    ):
        create("invalid-behavior", term=executable_text)
    assert not (home / "run/sessions/invalid-behavior.json").exists()
