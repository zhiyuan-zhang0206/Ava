"""Native POSIX acquisition ownership, including already-reparented children."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cli.release_prepare.acquisition_process import Commands
from shared import posix_command
from shared.exec_process_domain import ExecProcessDomain
from shared.runtime_prepare import _run, tree_inventory

pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"}, reason="POSIX acquisition execution domain"
)


def _live(pid: int, birth: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == birth and process.status() not in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }
    except psutil.NoSuchProcess:
        return False


def _cleanup_receipt(path: Path) -> None:
    if path.exists():
        pid, birth = json.loads(path.read_text())
        if _live(pid, birth):
            # Negative controls must clean only this captured native birth.
            psutil.Process(pid).kill()


def _programs(root: Path, *, parent_wait: bool, child_delay: float) -> tuple[Path, Path]:
    child = root / "child.py"
    child.write_text(
        f"import time; time.sleep({child_delay}); print('child completed', flush=True)\n"
    )
    parent = root / "parent.py"
    parent.write_text(
        "import json, pathlib, subprocess, sys, time, psutil\n"
        "child = subprocess.Popen([sys.executable, '-I', '-B', sys.argv[1]])\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps([child.pid, psutil.Process(child.pid).create_time()]))\n"
        "print('parent stdout', flush=True)\n"
        "print('parent stderr', file=sys.stderr, flush=True)\n"
        + ("time.sleep(60)\n" if parent_wait else "")
    )
    return parent, child


@pytest.mark.parametrize("parent_wait", [False, True])
def test_timeout_closes_exact_group_even_after_parent_exit(
    tmp_path: Path, parent_wait: bool
) -> None:
    root = tmp_path.resolve()
    parent, child = _programs(root, parent_wait=parent_wait, child_delay=60)
    receipt = root / "child.json"
    work = root / "work"
    work.mkdir()
    commands = Commands(work, Path(sys.executable))
    unrelated = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            commands.run(
                [sys.executable, "-I", "-B", str(parent), str(child), str(receipt)], root, timeout=1
            )
        assert time.monotonic() - started < 8
        pid, birth = json.loads(receipt.read_text())
        assert not _live(pid, birth), "timed-out acquisition left its captured child alive"
        assert unrelated.poll() is None
        (record,) = commands.evidence
        assert record.timed_out and record.returncode is None
        assert record.elapsed_seconds >= 0.9
        assert "parent stdout" in record.output.path.read_text()
        assert "parent stderr" in record.output.path.read_text()
    finally:
        _cleanup_receipt(receipt)
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_parent_success_waits_for_natural_child_completion(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    parent, child = _programs(root, parent_wait=False, child_delay=0.3)
    receipt = root / "child.json"
    work = root / "work"
    work.mkdir()
    commands = Commands(work, Path(sys.executable))
    try:
        output = commands.run(
            [sys.executable, "-I", "-B", str(parent), str(child), str(receipt)], root, timeout=5
        )
        assert "child completed" in output
        pid, birth = json.loads(receipt.read_text())
        assert not _live(pid, birth)
        assert commands.evidence[0].returncode == 0
    finally:
        _cleanup_receipt(receipt)


def test_interruption_closes_spawned_group_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    parent, child = _programs(root, parent_wait=True, child_delay=60)
    receipt = root / "child.json"

    def interrupted(_process: subprocess.Popen[bytes], _timeout: float) -> None:
        deadline = time.monotonic() + 5
        while not receipt.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("test child did not publish its native birth")
            time.sleep(0.01)
        raise KeyboardInterrupt("controlled interruption")

    monkeypatch.setattr(posix_command, "_wait_for_completion", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt, match="controlled interruption"):
            posix_command.run_owned_command(
                [sys.executable, "-I", "-B", str(parent), str(child), str(receipt)],
                cwd=root,
                env={"PATH": "/usr/bin:/bin"},
                timeout=5,
            )
        pid, birth = json.loads(receipt.read_text())
        assert not _live(pid, birth)
    finally:
        _cleanup_receipt(receipt)


def test_cleanup_error_keeps_original_timeout_in_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    parent, child = _programs(root, parent_wait=False, child_delay=60)
    receipt = root / "child.json"
    original = posix_command._close_and_reap

    def fail_after_cleanup(domain: ExecProcessDomain) -> int:
        original(domain)
        raise RuntimeError("controlled cleanup uncertainty")

    monkeypatch.setattr(posix_command, "_close_and_reap", fail_after_cleanup)
    try:
        with pytest.raises(RuntimeError, match=r"TimeoutExpired.*cleanup uncertainty"):
            posix_command.run_owned_command(
                [sys.executable, "-I", "-B", str(parent), str(child), str(receipt)],
                cwd=root,
                env={"PATH": "/usr/bin:/bin"},
                timeout=1,
            )
        pid, birth = json.loads(receipt.read_text())
        assert not _live(pid, birth)
    finally:
        _cleanup_receipt(receipt)


def test_offline_probe_uses_no_output_files_in_sealed_image(tmp_path: Path) -> None:
    image = tmp_path.resolve() / "sealed-image"
    image.mkdir()
    (image / "manifest.json").write_text('{"sealed": true}\n')
    before = tree_inventory(image)
    image.chmod(0o500)
    try:
        assert _run([sys.executable, "-I", "-B", "-c", "print('native probe')"], image) == (
            "native probe\n"
        )
        assert tree_inventory(image) == before
    finally:
        image.chmod(0o700)


def test_owned_tool_group_remains_inside_callers_session(tmp_path: Path) -> None:
    result = posix_command.run_owned_command(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import os,json;print(json.dumps([os.getpid(),os.getpgid(0),os.getsid(0)]))",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        timeout=5,
    )
    pid, group, session = json.loads(result.stdout)
    assert pid == group
    assert session == os.getsid(0)
