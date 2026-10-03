"""What a terminal closure leaves: survivors keep their native identity and diagnostics.

A normal stop SIGKILLs what outlives its grace, so a survivor here is a process the
closure may not signal (another user's); `stub_closure` stands in for the service's
closure reporting one. The stop turns the survivors of an outcome into the failure
report and the journal's structured inventory.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from base.deploy.lifecycle import status_journal
from base.native_process import ownership
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from cli.commands.lifecycle import _maintenance_stop_report as report
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import service_stop as stop
from cli.commands.lifecycle.tests.stop_support import (
    Launcher,
    closed_session,
    identity_of,
    stub_closure,
)
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop_support import written as written
from tests.agent.test_maintenance import WHEN

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
        "import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
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


def test_report_keeps_owned_job_of_a_closed_session(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's shell is gone but a job outlived the SIGKILL: the report names the
    job, its role and its session."""
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
    with pytest.raises(report.StopIncompleteError) as caught:
        stop.close_terminals(time.monotonic() + 0.35, "private-stop", WHEN, direct_db=False)
    assert child.live()
    assert len(caught.value.survivors) == 1
    survivor = caught.value.survivors[0]
    assert survivor["pid"] == child.pid
    assert survivor["role"] == "job" and survivor["service"] == "private-terminal"
    assert str(armed) in str(survivor["cmdline"])


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
