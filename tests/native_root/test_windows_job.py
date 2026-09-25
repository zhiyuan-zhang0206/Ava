"""Observe the original native Job rather than inferring containment from flags."""

import json
import subprocess
import sys

import psutil
import pytest

from tests.native_root.test_windows_root import ended, wait_for

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job proof")


async def test_breakaway_request_cannot_escape_original_application_job(tmp_path, native_env):
    from services.ava_root.custody import ServiceCustody
    from services.ava_root.windows.process import spawn

    attempt, child_birth = tmp_path / "attempt", tmp_path / "child-birth"
    child_code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(child_birth)!r}).write_text(str(os.getpid())); time.sleep(120)"
    )
    code = f"""import json,subprocess,sys,pathlib,time
try:
 p=subprocess.Popen([sys.executable,'-c',{child_code!r}],creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)
 result={{'pid':p.pid}}
except OSError as error:
 result={{'winerror':error.winerror}}
pathlib.Path({str(attempt)!r}).write_text(json.dumps(result))
time.sleep(120)
"""
    custody = ServiceCustody(tmp_path / "run", "application")
    with (tmp_path / "application.log").open("wb") as log:
        process = spawn([sys.executable, "-u", "-c", code], native_env, log.fileno())
    child = None
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], env=native_env
    )
    try:
        wait_for(attempt.exists, "breakaway request did not complete")
        result = json.loads(attempt.read_text())
        if "winerror" in result:
            assert result["winerror"] == 5  # Native access denial is also containment.
        else:
            # A nested Python redirector Job can permit CreateProcess to succeed.
            # Query native membership: both launcher and actual interpreter must
            # still belong to the original retained application Job.
            wait_for(child_birth.exists, "created child did not execute")
            child = psutil.Process(int(child_birth.read_text()))
            members = process.job.member_pids()
            assert {result["pid"], child.pid} <= members
        captured = process.members()
        (tmp_path / "native-members.json").write_text(
            json.dumps(
                [
                    {"pid": item.pid, "birth": item.birth, "exe": psutil.Process(item.pid).exe()}
                    for item in sorted(captured, key=lambda item: item.pid)
                ]
            )
        )
        await process.close(custody, timeout=5, force=True)
        assert process.job.active_processes() == 0
        assert all(not member.live() for member in captured)
        assert unrelated.poll() is None
        custody.clear()
    finally:
        process.job.close()
        await process.wait()
        if child is not None and not ended(child):
            child.kill()  # Exact disposable child: cleanup must not hide an escape.
            child.wait(timeout=5)
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_exited_process_is_dead_while_its_original_handle_remains_open(tmp_path, native_env):
    from shared.proc_tree import OwnedProcess

    release = tmp_path / "release"
    code = f"""import os,pathlib,time
while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)
os._exit(259)
"""
    child = subprocess.Popen(  # noqa: S603 -- fixed fixture code, private native home.
        [sys.executable, "-c", code], env=native_env
    )
    try:
        identity = OwnedProcess.capture(psutil.Process(child.pid))
        assert identity.live()
        release.write_text("exit")
        # Popen waits on its original native handle and retains it afterward.
        # Exit 259 is also STILL_ACTIVE: exit-code comparison is not liveness.
        assert child.wait(timeout=5) == 259
        assert int(child._handle) > 0
        assert not identity.live()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
