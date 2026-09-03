"""Raw stdout transcript size rotation — `services.agent_host.daemon`.

The launcher points the hosted daemon's fd 1/2 straight at
`$AVA_HOME/logs/ava-agent-host.out.log`, which carries none of the caps the
rest of the logging stack has (pty transcripts cap at 64 MB, JSONL sinks
rotate at 100 MB) — a crash storm can balloon it to a disk-filling size
(2026-09-03: 13.5 GB in ~6 minutes, task #2356). The daemon re-points its own
fds once the file crosses a ceiling. These tests pin the roll-over mechanics
and the no-op safety of that rotation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from services.agent_host import daemon

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_stdout_log_path_points_into_logs_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The transcript path derives from logs_dir() and the session name."""
    monkeypatch.setattr(daemon.paths, "logs_dir", lambda: tmp_path)
    assert daemon._stdout_log_path() == tmp_path / "ava-agent-host.out.log"


def test_rotate_files_keeps_one_generation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The current chunk replaces `.1` and any stray `.2` is dropped — the
    replacement (not a shift) is what keeps a storm bounded on disk."""
    log = tmp_path / "ava-agent-host.out.log"
    log.write_bytes(b"current-chunk")
    one = tmp_path / "ava-agent-host.out.log.1"
    one.write_bytes(b"old-chunk")
    two = tmp_path / "ava-agent-host.out.log.2"
    two.write_bytes(b"ancient-chunk")

    daemon._rotate_stdout_log_files(log)

    assert not two.exists()
    assert one.read_bytes() == b"current-chunk"
    # The live path is gone until the caller re-creates it (open + dup2).
    assert not log.exists()


def test_rotate_files_without_prior_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first rotation with no `.1`/`.2` present is a plain rename."""
    log = tmp_path / "ava-agent-host.out.log"
    log.write_bytes(b"first-chunk")

    daemon._rotate_stdout_log_files(log)

    assert (tmp_path / "ava-agent-host.out.log.1").read_bytes() == b"first-chunk"
    assert not (tmp_path / "ava-agent-host.out.log.2").exists()


def _child_script(log_path: str) -> str:
    """A real-fd rotation: write past the ceiling, rotate, keep writing.

    Runs in a subprocess whose stdout IS the transcript file, so `fstat(1)`
    and the dup2 re-point are exercised against a genuine fd 1 rather than a
    pytest capture wrapper.
    """
    return f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(_REPO_ROOT)!r})
from services.agent_host import daemon

log = Path(sys.argv[1])
daemon._stdout_log_path = lambda: log
daemon._STDOUT_LOG_ROTATE_BYTES = 1 << 20  # 1 MiB ceiling for the test
os.write(1, b"x" * (1 << 20))
rotated = daemon._rotate_stdout_log_if_needed()
assert rotated == (1 << 20), f"expected rotation at ceiling, got {{rotated}}"
os.write(1, b"after-rotation")
# Stream objects (the loguru console sink writes through the same path) must
# also land in the re-pointed file, not the renamed chunk.
sys.stdout.write("via-stdout")
sys.stdout.flush()
sys.stderr.write("via-stderr")
sys.stderr.flush()
"""


def test_subprocess_rotates_and_continues_writing(tmp_path: Path) -> None:
    """Past the ceiling: the chunk moves to `.1`, fd 1/2 re-point to a fresh
    file at the original path, and subsequent writes land in the new file."""
    log = tmp_path / "ava-agent-host.out.log"
    with log.open("wb") as transcript:
        proc = subprocess.run(  # noqa: S603 -- fixed argv: sys.executable + our own script
            [sys.executable, "-c", _child_script(str(log)), str(log)],
            stdout=transcript,
            stderr=subprocess.PIPE,
            cwd=_REPO_ROOT,
            timeout=120,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    one = tmp_path / "ava-agent-host.out.log.1"
    assert one.exists() and one.stat().st_size == (1 << 20)
    assert not (tmp_path / "ava-agent-host.out.log.2").exists()
    # The live file was re-created and received the post-rotation writes,
    # direct and through the stream objects alike.
    assert log.read_bytes() == b"after-rotation" + b"via-stdout" + b"via-stderr"


def test_rotation_noop_below_ceiling(tmp_path: Path) -> None:
    """A file under the ceiling is left untouched — no rename, no new fd."""
    log = tmp_path / "ava-agent-host.out.log"
    log.write_bytes(b"small")
    with log.open("ab") as transcript:
        proc = subprocess.run(  # noqa: S603 -- fixed argv: sys.executable + our own script
            [sys.executable, "-c", _child_script_noop(str(log)), str(log)],
            stdout=transcript,
            stderr=subprocess.PIPE,
            cwd=_REPO_ROOT,
            timeout=120,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert log.read_bytes() == b"small" + b"tail"
    assert not (tmp_path / "ava-agent-host.out.log.1").exists()


def _child_script_noop(log_path: str) -> str:
    return f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(_REPO_ROOT)!r})
from services.agent_host import daemon

log = Path(sys.argv[1])
daemon._stdout_log_path = lambda: log
daemon._STDOUT_LOG_ROTATE_BYTES = 1 << 30  # far above the few bytes written
rotated = daemon._rotate_stdout_log_if_needed()
assert rotated is None, f"expected no rotation, got {{rotated}}"
os.write(1, b"tail")
"""


def _child_script_open_failure(log_path: str) -> str:
    """First rotation attempt fails at the open (disk full / EMFILE): the
    transcript must be restored under the original name, and the next attempt
    must succeed — a stranded `.1` would grow past every bound."""
    return f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(_REPO_ROOT)!r})
from services.agent_host import daemon

log = Path(sys.argv[1])
daemon._stdout_log_path = lambda: log
daemon._STDOUT_LOG_ROTATE_BYTES = 1 << 20  # 1 MiB ceiling for the test
os.write(1, b"x" * (1 << 20))

real_open = os.open
def boom(_path, _flags, _mode=0o777):
    raise OSError(28, "No space left on device")
os.open = boom
rotated = daemon._rotate_stdout_log_if_needed()
assert rotated is None, f"expected failure, got {{rotated}}"
os.open = real_open
assert log.exists(), "transcript must be restored under its original name"
assert not Path(str(log) + ".1").exists(), "no chunk may be stranded"
os.write(1, b"after-open-failure")
rotated = daemon._rotate_stdout_log_if_needed()
assert rotated == (1 << 20) + len(b"after-open-failure"), f"self-heal failed: {{rotated}}"
os.write(1, b"after-self-heal")
"""


def test_subprocess_open_failure_restores_and_self_heals(tmp_path: Path) -> None:
    """A failed post-rename open restores the name (no unbounded `.1`), and
    the next pass rotates normally."""
    log = tmp_path / "ava-agent-host.out.log"
    with log.open("wb") as transcript:
        proc = subprocess.run(  # noqa: S603 -- fixed argv: sys.executable + our own script
            [sys.executable, "-c", _child_script_open_failure(str(log)), str(log)],
            stdout=transcript,
            stderr=subprocess.PIPE,
            cwd=_REPO_ROOT,
            timeout=120,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    one = tmp_path / "ava-agent-host.out.log.1"
    assert one.exists() and one.stat().st_size == (1 << 20) + len(b"after-open-failure")
    assert not (tmp_path / "ava-agent-host.out.log.2").exists()
    assert log.read_bytes() == b"after-self-heal"
