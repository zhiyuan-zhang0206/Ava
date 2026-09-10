"""The deadline report names the exact surviving service and process identity.

Issue #2162: when a held stop cannot close an owned tree, the failure must
carry the operator to the exact resource — owning session, leader/descendant
role, birth pair, cmdline, occupied recorded groups, and the stage — instead
of a bare pid list that leaves only a blind rerun. These tests drive real
private process boundaries through the same entrypoints `ava pause` / the
update's stop leg call.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cli.commands import _maintenance_stop as stop
from cli.commands import stop as entry
from shared.session_backend import PosixProcSessionBackend
from shared.session_record import SessionRecord, pid_starttime_ticks
from tests.cli.test_maintenance_stop import Launcher
from tests.cli.test_maintenance_stop import home as home
from tests.cli.test_maintenance_stop import launch as launch
from tests.cli.test_pause_stop import dependencies

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="real POSIX signal contract")

_IGNORE = (
    "import signal,time; "
    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    "print('ready',flush=True); "
    "time.sleep(60)"
)


def test_refusing_service_failure_names_its_full_identity(home: Path, launch: Launcher) -> None:
    service = launch("ava-worker", _IGNORE)
    with pytest.raises(stop.StopIncompleteError) as excinfo:
        stop.stop_services(0.25)
    exc = excinfo.value
    message = str(exc)
    # The legacy summary stays, so existing operators keep their grep anchors.
    assert "service stop incomplete" in message
    assert "surviving services: ['ava-worker']" in message
    assert f"surviving tracked descendants: [{service.pid}]" in message
    # ...and the inventory now names the process itself.
    assert "stage=services" in message
    assert f"pid={service.pid}" in message
    assert "role=leader" in message
    assert "service='ava-worker'" in message
    assert "birth=" in message
    assert "SIG_IGN" in message  # the cmdline the operator must judge
    assert service.poll() is None  # the refusal never becomes a force kill
    payload = {item["pid"]: item for item in exc.survivors}
    assert payload[service.pid]["role"] == "leader"
    assert payload[service.pid]["service"] == "ava-worker"
    assert "SIG_IGN" in str(payload[service.pid]["cmdline"])
    assert payload[service.pid]["pgid"] == os.getpgid(service.pid)


def test_leader_exit_with_refusing_descendant_names_the_survivor(
    home: Path, launch: Launcher
) -> None:
    # The frontend chain's shape: the leader exits on TERM without closing its
    # child, and here the child also refuses TERM, so the stop holds until the
    # deadline. The report must name the child as the leader's descendant — the
    # process that actually held the stop.
    code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-u','-c',{_IGNORE!r}]); "
        "print('ready',flush=True); time.sleep(60)"
    )
    parent = launch("ava-frontend", code)
    children = psutil.Process(parent.pid).children()
    assert len(children) == 1
    child = stop.OwnedProcess.capture(children[0])
    try:
        with pytest.raises(stop.StopIncompleteError) as excinfo:
            stop.stop_services(0.6)
        assert parent.wait(timeout=1) == -signal.SIGTERM
        message = str(excinfo.value)
        assert f"pid={child.pid}" in message
        assert "role=descendant" in message
        assert "service='ava-frontend'" in message
        payload = {item["pid"]: item for item in excinfo.value.survivors}
        assert payload[child.pid]["role"] == "descendant"
        assert payload[child.pid]["service"] == "ava-frontend"
        assert "SIG_IGN" in str(payload[child.pid]["cmdline"])
        assert children[0].is_running()  # the refusal never becomes a force kill
    finally:
        children[0].kill()  # Exact child created by this fixture, after the assertions.


def test_late_child_in_recorded_group_is_named_as_group_member(
    home: Path, launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A child created during the leader's own TERM handling escapes the capture
    # and lands in the recorded process group; the stop holds on the occupied
    # group. The report must name that child — pid and cmdline — together with
    # the recorded session whose group it occupies.
    child_file = home / "late-child.pid"
    parent = launch(
        "late-child",
        f"""
import subprocess, signal, sys, time, pathlib, os

def finish(*_):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path({str(child_file)!r}).write_text(str(child.pid))
    os._exit(0)
signal.signal(signal.SIGTERM, finish)
print('ready', flush=True)
time.sleep(60)
""",
    )
    original = PosixProcSessionBackend.graceful_signal

    def deliver(
        backend: PosixProcSessionBackend,
        name: str,
        *,
        expected: SessionRecord | None = None,
    ) -> bool:
        result = original(backend, name, expected=expected)
        parent.wait(timeout=2)  # Make the actual leader exit before the next snapshot.
        return result

    monkeypatch.setattr(PosixProcSessionBackend, "graceful_signal", deliver)
    try:
        with pytest.raises(stop.StopIncompleteError) as excinfo:
            stop.stop_services(0.3)
        child = psutil.Process(int(child_file.read_text()))
        assert child.is_running()  # The refusal must not become a force kill.
        message = str(excinfo.value)
        assert f"pid={child.pid}" in message
        assert "role=group-member" in message
        assert "service='late-child'" in message
        payload = {item["pid"]: item for item in excinfo.value.survivors}
        assert payload[child.pid]["role"] == "group-member"
        assert payload[child.pid]["service"] == "late-child"
        assert "time.sleep(60)" in str(payload[child.pid]["cmdline"])
    finally:
        if child_file.exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(int(child_file.read_text())).kill()  # Exact private fixture.


@pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("bash") is None, reason="real shell/node layering"
)
def test_layered_shell_node_chain_reports_the_surviving_node_process(home: Path) -> None:
    # The frontend's actual launch layering (bash -lc -> node -> node) with a
    # child that refuses TERM: the deadline report must name the surviving node
    # process, cmdline included, so the operator does not have to guess which
    # layer of the chain held the stop.
    leader = home / "leader.js"
    child = home / "child.js"
    leader.write_text(
        "const { spawn } = require('child_process');\n"
        "spawn(process.execPath, [" + repr(str(child)) + "], { stdio: 'inherit' });\n"
        "process.on('SIGTERM', () => process.exit(0));\n"
        "setInterval(() => {}, 1000);\n"
    )
    child.write_text("process.on('SIGTERM', () => {});\nsetInterval(() => {}, 1000);\n")
    proc = subprocess.Popen(  # noqa: S603 — test-owned bash + node, fixed fixture scripts
        ["bash", "-lc", f"node {leader}"],
        cwd=home,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    leader_pid: int | None = None
    grandchild_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            candidates = [proc.pid] + [c.pid for c in psutil.Process(proc.pid).children()]
            for candidate in candidates:
                if psutil.Process(candidate).name() == "node":
                    leader_pid = candidate
                    break
            if leader_pid is not None and psutil.Process(leader_pid).children():
                break
            time.sleep(0.05)
        assert leader_pid is not None
        grandchild = psutil.Process(leader_pid).children()[0]
        grandchild_pid = grandchild.pid
        SessionRecord(
            leader_pid,
            psutil.Process(leader_pid).create_time(),
            "layered-test",
            str(home),
            time.time(),
            pid_starttime_ticks(leader_pid),
            pgid=os.getpgid(leader_pid),
        ).write(home / "run/sessions" / "layered.json")
        with pytest.raises(stop.StopIncompleteError) as excinfo:
            stop.stop_services(1.0)
        assert grandchild.is_running()  # The refusal must not become a force kill.
        message = str(excinfo.value)
        assert f"pid={grandchild_pid}" in message
        assert "role=descendant" in message
        assert "service='layered'" in message
        assert str(child) in message  # the cmdline names the exact node script
        payload = {item["pid"]: item for item in excinfo.value.survivors}
        assert payload[grandchild_pid]["role"] == "descendant"
        assert str(child) in str(payload[grandchild_pid]["cmdline"])
    finally:
        if proc.poll() is None:
            proc.kill()
        for pid in (leader_pid, grandchild_pid):
            if pid is not None:
                with contextlib.suppress(psutil.NoSuchProcess):
                    psutil.Process(pid).kill()  # Exact private fixtures, post-assertion.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def test_failed_pause_persists_survivor_identity_on_the_status_journal(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies(monkeypatch)
    service = launch("ava-worker", _IGNORE)
    assert entry.cmd_pause(timeout=0.25) == 1
    err = capsys.readouterr().err
    assert "Pause/stop incomplete" in err
    assert "service stop incomplete" in err
    assert f"pid={service.pid}" in err
    assert "stage=services" in err
    # The journal — the durable record a later operator reads — carries the
    # same structured inventory, not just the printable string.
    journal = json.loads((home / "run" / "lifecycle-op.json").read_text())
    assert journal["complete"] is True
    result = journal["result"]
    assert result["rc"] == 1
    assert result["stage"] == "services"
    assert {item["pid"] for item in result["survivors"]} == {service.pid}
    ref = result["survivors"][0]
    assert ref["role"] == "leader" and ref["service"] == "ava-worker"
    assert "SIG_IGN" in str(ref["cmdline"])
    assert service.poll() is None


def test_launched_service_deadline_report_names_the_real_daemon(home: Path) -> None:
    # The actual launch chain: `PosixProcSessionBackend.new_session` wraps the
    # command as a login-shell exec, so the recorded pid IS the daemon (the
    # shape every service uses). A daemon that refuses TERM must be named by
    # the deadline report with its recorded identity and script path.
    from shared import posixproc

    started = home / "started"
    script = home / "daemon.py"
    script.write_text(
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(started)!r}).write_text('up')\n"
        "time.sleep(120)\n"
    )
    name = "ava-worker"
    assert PosixProcSessionBackend().new_session(
        name, f"{sys.executable} {script}", home, env=dict(os.environ)
    )
    try:
        deadline = time.monotonic() + 20
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        record = SessionRecord.read(home / "run" / "sessions" / f"{name}.json")
        assert record is not None
        with pytest.raises(stop.StopIncompleteError) as excinfo:
            stop.stop_services(0.5)
        payload = {item["pid"]: item for item in excinfo.value.survivors}
        assert record.pid in payload
        ref = payload[record.pid]
        assert ref["role"] == "leader" and ref["service"] == name
        assert str(script) in str(ref["cmdline"])
        birth = ref["birth"]
        assert isinstance(birth, float) and birth > 0
        assert psutil.Process(record.pid).is_running()  # never force-killed
    finally:
        posixproc.kill_session(name, graceful=False)
