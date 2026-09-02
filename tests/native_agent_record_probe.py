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
from pathlib import Path

import psutil

from ops.agent_launch import _require_released_agent_session
from shared.cluster import session_name
from shared.config import settings
from shared.session_backend import native_proc
from shared.session_record import SessionRecord


class NativeAgentRecordOrdering(unittest.TestCase):
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
                    "from agent.session_admission import publish_admitted_session;"
                    "from shared.runtime_incarnation import RuntimeIncarnation;"
                    f"time.sleep({child_delay});"
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
                        self.assertEqual(os.name, "nt")
                        parent = actual_agent.parent()
                        self.assertIsNotNone(parent)
                        if parent is None:
                            self.fail("redirected Python lost its verified session parent")
                        self.assertEqual(parent.pid, admitted.pid)
                        self.assertEqual(parent.create_time(), admitted.create_time)
                        self.assertEqual(
                            Path(parent.exe()).resolve(), Path(sys.executable).resolve()
                        )
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
                finally:
                    backend.kill_session(attempt, graceful=False)
                    settings.general.ava_home = original_home


if __name__ == "__main__":
    unittest.main()
