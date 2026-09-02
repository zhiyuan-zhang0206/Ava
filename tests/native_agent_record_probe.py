"""Native OS record ordering probe, run with unittest outside PG/Redis fixtures.

This exercises actual POSIX/Windows launch and record consumers, not admission
authority. The separate PostgreSQL suite proves that only admission calls the
publisher. All children and paths belong to this disposable CI test.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import psutil

from ops.agent_launch import _require_released_agent_session
from shared.cluster import session_name
from shared.config import settings
from shared.session_backend import native_proc
from shared.session_record import SessionRecord


class NativeAgentRecordOrdering(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows bounded non-cooperative stop")
    def test_noncooperative_handler_requires_bounded_force_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original_home = settings.general.ava_home
            settings.general.ava_home = home
            backend = native_proc()
            name = session_name("noncooperative-control-probe")
            ready, marker = home / "ready", home / "handled"
            child = (
                "import os,signal,time;from pathlib import Path;"
                "signal.signal(signal.SIGBREAK,lambda *_: "
                f"Path({str(marker)!r}).write_text('handled'));"
                f"Path({str(ready)!r}).write_text(str(os.getpid()));time.sleep(30)"
            )
            try:
                self.assertTrue(
                    backend.new_session(
                        name,
                        [sys.executable, "-c", child],
                        Path.cwd(),
                        env=dict(os.environ),
                    )
                )
                deadline = time.monotonic() + 15
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                actual = psutil.Process(int(ready.read_text()))
                started = time.monotonic()
                ok, mode = backend.kill_session(name, graceful=True, timeout=0.2)
                self.assertEqual((ok, mode), (True, "forced"))
                self.assertLess(time.monotonic() - started, 8)
                self.assertFalse(marker.exists(), "blocked main thread unexpectedly ran handler")
                self.assert_process_exited(actual)
            finally:
                backend.kill_session(name, graceful=False)
                settings.general.ava_home = original_home

    def test_helper_failure_and_timeout_are_not_delivery_success(self) -> None:
        import subprocess

        from shared import winproc

        record = SessionRecord(123, 1.0, "test", "/", 1.0, control_mode="private-console-v1")
        with (
            patch.object(winproc, "_read_record", return_value=record),
            patch.object(winproc, "_process_for_record", return_value=object()),
            patch.object(winproc, "_record_path", return_value=Path("/unused/test.json")),
        ):
            with (
                patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("helper", 5)),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                winproc.graceful_signal("test")
            result = subprocess.CompletedProcess([], 1, "", "identity changed")
            with (
                patch.object(subprocess, "run", return_value=result),
                self.assertRaisesRegex(RuntimeError, "identity changed"),
            ):
                winproc.graceful_signal("test")

    def assert_control_refusals(self, home: Path, child_pid: int) -> None:
        from shared import winproc

        name = session_name("agent-124")
        path = home / "run" / "sessions" / f"{name}.json"
        record = SessionRecord.read(path)
        if record is None:
            self.fail("missing admitted record")
        replace(record, control_mode=None).write(path)
        with self.assertRaisesRegex(RuntimeError, "private-console"):
            winproc.graceful_signal(name)
        replace(record, create_time=record.create_time + 100).write(path)
        self.assertFalse(winproc.graceful_signal(name))
        record.write(path)
        if child_pid != record.pid:
            foreign = path.with_name("other-recorded-session.json")
            replace(
                record, pid=child_pid, create_time=psutil.Process(child_pid).create_time()
            ).write(foreign)
            try:
                with self.assertRaisesRegex(RuntimeError, "another recorded session"):
                    winproc.graceful_signal(name)
            finally:
                foreign.unlink()

    def assert_redirector_parent(self, child: psutil.Process, record: SessionRecord) -> None:
        self.assertEqual(os.name, "nt")
        parent = child.parent()
        self.assertIsNotNone(parent)
        if parent is None:
            self.fail("redirected Python lost its verified session parent")
        self.assertEqual(parent.pid, record.pid)
        self.assertEqual(parent.create_time(), record.create_time)
        self.assertEqual(Path(parent.exe()).resolve(), Path(sys.executable).resolve())

    def assert_process_exited(self, process: psutil.Process) -> None:
        deadline = time.monotonic() + 5
        while process.is_running() and time.monotonic() < deadline:
            if process.status() == psutil.STATUS_ZOMBIE:
                return
            time.sleep(0.05)
        self.assertTrue(
            not process.is_running() or process.status() == psutil.STATUS_ZOMBIE,
            "canonical alias must not spare the attempt's actual Python child",
        )

    @unittest.skipUnless(os.name == "nt", "Windows console control contract")
    def test_ctrl_break_reaches_child_without_stopping_other_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original_home = settings.general.ava_home
            settings.general.ava_home = home
            backend = native_proc()
            attempt = session_name("boot-124-" + "b" * 32)
            bystander = session_name("native-control-bystander")
            ready = home / "ready"
            received = home / "sigbreak-received"
            child = (
                "import os,signal,time;from pathlib import Path;from uuid import UUID;"
                "from shared.config import settings;"
                f"settings.general.ava_home=Path({str(home)!r});"
                "from agent.session_admission import publish_admitted_session,wait_for_launch_record;"
                "from shared.runtime_incarnation import RuntimeIncarnation;"
                "signal.signal(signal.SIGBREAK,lambda *_: "
                f"Path({str(received)!r}).write_text(str(os.getpid())));"
                "wait_for_launch_record(124);"
                "publish_admitted_session(RuntimeIncarnation(124,UUID(int=3),UUID(int=4)));"
                f"Path({str(ready)!r}).write_text(str(os.getpid()));"
                "[time.sleep(0.05) for _ in range(600)]"
            )
            try:
                self.assertTrue(
                    backend.new_session(
                        bystander,
                        [sys.executable, "-c", "import time;time.sleep(30)"],
                        Path.cwd(),
                        env=dict(os.environ),
                    )
                )
                self.assertTrue(
                    backend.new_session(
                        attempt,
                        [sys.executable, "-c", child],
                        Path.cwd(),
                        env=dict(os.environ),
                    )
                )
                deadline = time.monotonic() + 15
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "signal handler readiness was never observed")
                self.assert_control_refusals(home, int(ready.read_text()))
                reported = backend.graceful_signal(session_name("agent-124"))
                deadline = time.monotonic() + 5
                while not received.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                sys.stdout.write(
                    json.dumps(
                        {
                            "graceful_signal_reported": reported,
                            "child_handler_received": received.exists(),
                            "child_pid": int(ready.read_text()),
                            "bystander_alive": backend.has_session(bystander),
                            "attempt_alive": backend.has_session(attempt),
                        }
                    )
                    + "\n"
                )
                self.assertTrue(backend.has_session(bystander))
                self.assertTrue(
                    received.exists(), "Ctrl-Break did not reach the actual child handler"
                )
                self.assertEqual(received.read_text(), ready.read_text())
            finally:
                backend.kill_session(attempt, graceful=False)
                backend.kill_session(bystander, graceful=False)
                settings.general.ava_home = original_home

    def test_parent_record_cannot_overwrite_child_canonical(self) -> None:
        for child_delay in (0.0, 0.3):
            with self.subTest(child_delay=child_delay), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                original_home = settings.general.ava_home
                settings.general.ava_home = home
                backend = native_proc()
                attempt = session_name("boot-123-" + "a" * 32)
                canonical = home / "run" / "sessions" / f"{session_name('agent-123')}.json"
                attempt_path = canonical.with_name(f"{attempt}.json")
                child = (
                    "import time;from pathlib import Path;from uuid import UUID;"
                    "from shared.config import settings;"
                    f"settings.general.ava_home=Path({str(home)!r});"
                    "from agent.session_admission import publish_admitted_session,wait_for_launch_record;"
                    "from shared.runtime_incarnation import RuntimeIncarnation;"
                    f"time.sleep({child_delay});"
                    "wait_for_launch_record(123);"
                    "publish_admitted_session(RuntimeIncarnation(123,UUID(int=1),UUID(int=2)));"
                    "import os;"
                    f"Path({str(home / 'actual-agent-pid')!r}).write_text(str(os.getpid()));"
                    "time.sleep(30)"
                )
                try:
                    self.assertTrue(
                        backend.new_session(
                            attempt, [sys.executable, "-c", child], Path.cwd(), env=dict(os.environ)
                        )
                    )
                    deadline = time.monotonic() + 15
                    while not (home / "actual-agent-pid").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    admitted = SessionRecord.read(canonical)
                    launched = SessionRecord.read(attempt_path)
                    self.assertIsNotNone(admitted)
                    self.assertIsNotNone(launched)
                    if admitted is None or launched is None:
                        self.fail("native child did not publish both record identities")
                    self.assertEqual(admitted.pid, launched.pid)
                    self.assertEqual(admitted.create_time, launched.create_time)
                    actual_agent_pid = int((home / "actual-agent-pid").read_text())
                    actual_agent = psutil.Process(actual_agent_pid)
                    if actual_agent_pid != admitted.pid:
                        self.assert_redirector_parent(actual_agent, admitted)
                    self.assertEqual(launched.cwd, str(Path.cwd()))
                    self.assertIn("publish_admitted_session", launched.cmd)
                    self.assertEqual(admitted.generation, "00000000-0000-0000-0000-000000000001")
                    process_group = None
                    if os.name == "posix":
                        process_group = os.getpgid(admitted.pid)
                        self.assertNotEqual(process_group, os.getpgrp())
                    sys.stdout.write(
                        json.dumps(
                            {
                                "platform": sys.platform,
                                "pid": admitted.pid,
                                "actual_agent_pid": actual_agent_pid,
                                "birth": admitted.create_time,
                                "pgid": process_group,
                                "starttime": admitted.starttime,
                                "attempt": attempt,
                                "generation": admitted.generation,
                                "child_delay": child_delay,
                            }
                        )
                        + "\n"
                    )
                    before = canonical.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, "still live"):
                        _require_released_agent_session(123)
                    self.assertTrue(backend.has_session(attempt))
                    self.assertEqual(canonical.read_bytes(), before)
                    backend.kill_session(attempt, graceful=False)
                    self.assert_process_exited(actual_agent)
                finally:
                    backend.kill_session(attempt, graceful=False)
                    settings.general.ava_home = original_home


if __name__ == "__main__":
    unittest.main()
