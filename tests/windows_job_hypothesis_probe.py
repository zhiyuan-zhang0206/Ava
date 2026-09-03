"""Disposable Windows counterexample to post-spawn named-job enrollment."""

import ctypes
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import psutil

from shared import winjob


@unittest.skipUnless(sys.platform == "win32", "Windows named job semantics")
class NamedJobEnrollmentHypothesis(unittest.TestCase):
    def test_open_unassigned_child_survives_parent_handle_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            name = "Local\\AvaSignalProbe-" + uuid4().hex
            api = winjob._kernel32()
            handle = api.CreateJobObjectW(None, name)
            self.assertTrue(handle)
            job = winjob.WindowsJob(int(handle))
            limits = winjob._ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            self.assertTrue(
                api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            )
            child = (
                "import ctypes,os,time;from pathlib import Path;"
                "api=ctypes.WinDLL('kernel32',use_last_error=True);"
                "api.OpenJobObjectW.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_wchar_p];"
                "api.OpenJobObjectW.restype=ctypes.c_void_p;"
                f"job=api.OpenJobObjectW(1,0,{name!r});assert job;"
                f"Path({str(ready)!r}).write_text(str(os.getpid()));"
                "time.sleep(30)"
            )
            actual = None
            proc = subprocess.Popen(  # noqa: S603 — disposable literal probe, no user command
                [sys.executable, "-I", "-c", child],
                creationflags=0x08000000,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                actual = psutil.Process(int(ready.read_text()))
                self.assertNotEqual(actual.pid, proc.pid, "probe requires actual venv redirector")
                job.assign(proc)
                job.close()  # parent timeout closes its only handle
                time.sleep(0.2)
                self.assertTrue(actual.is_running(), "counterexample did not reproduce")
                sys.stdout.write(
                    json.dumps(
                        {
                            "named_job_prejoin_leak_reproduced": True,
                            "redirector_pid": proc.pid,
                            "helper_pid": actual.pid,
                            "helper_birth": actual.create_time(),
                            "parent_handle_closed": job.closed,
                        }
                    )
                    + "\n"
                )
            finally:
                if actual is not None and actual.is_running():
                    actual.kill()
                    actual.wait(timeout=5)
                job.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
