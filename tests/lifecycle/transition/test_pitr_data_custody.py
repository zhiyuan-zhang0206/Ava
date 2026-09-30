"""A dead data leader cannot hide a retained, still-live native descendant."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.native_process.evidence import ExpectedProcess
from base.native_process.ownership import OwnedProcess
from cli.commands.data_plane import maintenance_stop as data
from cli.commands.data_plane.maintenance_stop import DataOwner, DataStop
from tests.lifecycle.transition.test_pitr_execution import _constant


def _evidence(identity: OwnedProcess) -> ExpectedProcess:
    return ExpectedProcess(
        pid=identity.pid, create_time=identity.birth, starttime=identity.starttime
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX data-plane execution domain")
def test_dead_leader_does_not_clear_a_live_captured_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "parent.py"
    release = tmp_path / "exit-parent"
    script.write_text("""import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
while not Path(sys.argv[1]).exists():
    time.sleep(.01)
""")
    with subprocess.Popen(  # noqa: S603 — private fixture source and exact interpreter
        [sys.executable, "-I", "-B", str(script), str(release)], stdout=subprocess.PIPE, text=True
    ) as parent:
        assert parent.stdout is not None
        # Held from capture on: once killed, the orphan's new parent may reap it
        # before a fresh psutil.Process(pid) could be built for the cleanup wait.
        child_process = psutil.Process(int(parent.stdout.readline()))
        child = OwnedProcess.capture(child_process)
        leader = OwnedProcess.capture(psutil.Process(parent.pid))
        assert psutil.Process(child.pid).ppid() == leader.pid
        owner = DataOwner(
            process=_evidence(leader),
            tree=(_evidence(leader), _evidence(child)),
            directory=str(tmp_path),
            port=18000,
        )
        receipt = DataStop(postgres=owner, redis=owner, pgbouncer=None)
        release.touch()
        parent.wait(timeout=5)
        assert not leader.live() and child.live()

        class NoRedis:
            async def capture(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        async def close(_client: object) -> None:
            pass

        monkeypatch.setattr(data, "_validate_receipt", _constant(None))
        monkeypatch.setattr(data, "_receipt_client", _constant(object()))
        monkeypatch.setattr(data, "_close_receipt_client", close)
        monkeypatch.setattr(data.ownership, "RedisConnectionCustody", NoRedis)
        monkeypatch.setattr(data.ownership, "require_listener", _constant(None))
        monkeypatch.setattr(data, "_require_no_unrecorded", _constant(None))
        try:
            with pytest.raises(TimeoutError):
                data.stop_captured(receipt, 0.05)
            assert child.live(), "ordinary closure must not silently escalate to kill"
        finally:
            if child.live():
                os.kill(child.pid, 9)  # exact native birth captured by this isolated test
            psutil.wait_procs([child_process], timeout=5)
        data.stop_captured(receipt, 0.5)
