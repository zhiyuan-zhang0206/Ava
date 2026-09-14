"""services.ava_root.handoff: the exec-generation handoff file + child adoption.

Unit-level: the wire format round-trip and its fail-fast validation, the
non-blocking child probe, and the thread-based reaper that re-attaches to a
child inherited across the root's exec replacement.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from services.ava_root.handoff import (
    ChildState,
    HandoffFile,
    HandoffUnit,
    adopt_child,
    handoff_path,
    load_handoff,
    probe_child,
    write_handoff,
)
from services.ava_root.manifest import DesiredState

_SLEEPER = [sys.executable, "-u", "-c", "import time; time.sleep(60)"]


def _sample() -> HandoffFile:
    return HandoffFile.stamp(
        os.getpid(),
        (
            HandoffUnit(
                "svc",
                DesiredState.RUNNING,
                pid=os.getpid(),
                pgid=os.getpgid(0),
                started_at=time.monotonic(),
            ),
            HandoffUnit("held", DesiredState.STOPPED),
        ),
    )


def _wait_zombie(pid: int, *, timeout: float = 5.0) -> None:
    """Wait until a killed child is an unreaped zombie (its exit is pending)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(  # noqa: S603 — fixed system tool, literal argv, no shell
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stdout.strip().startswith("Z"):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not become an unreaped zombie")


def test_roundtrip_preserves_every_field(tmp_path: Path) -> None:
    handoff = _sample()
    path = write_handoff(tmp_path, handoff)
    assert path == handoff_path(tmp_path)
    assert not path.with_name(path.name + ".tmp").exists()

    loaded = load_handoff(tmp_path, expected_writer_pid=os.getpid(), takeover_marker=True)
    assert loaded == handoff  # frozen dataclasses: structural equality
    assert path.exists()  # a live handoff is kept until the tree is up


def test_absent_file_is_a_cold_start(tmp_path: Path) -> None:
    assert load_handoff(tmp_path, expected_writer_pid=os.getpid(), takeover_marker=False) is None
    # A takeover marker with no file is anomalous, but still just a cold start.
    assert load_handoff(tmp_path, expected_writer_pid=os.getpid(), takeover_marker=True) is None


def _units(doc: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", doc["units"])


_MUTATORS: list[Callable[[dict[str, object]], object]] = [
    lambda _doc: None,
    lambda _doc: [1, 2],
    lambda doc: {**doc, "extra": 1},
    lambda doc: {k: v for k, v in doc.items() if k != "version"},
    lambda doc: {**doc, "version": 99},
    lambda doc: {**doc, "units": [{**_units(doc)[0], "desired": "sideways"}]},
    lambda doc: {**doc, "units": [_units(doc)[0], _units(doc)[0]]},
    lambda doc: {**doc, "units": [{**_units(doc)[1], "pgid": 7}]},
    lambda doc: {**doc, "units": [{**_units(doc)[1], "pid": 5}]},
]
_MALFORMED_IDS = [
    "json-null",
    "top-level-list",
    "unknown-field",
    "missing-field",
    "unsupported-version",
    "unknown-desired",
    "duplicate-unit",
    "pgid-without-pid",
    "pid-without-started-at",
]


@pytest.mark.parametrize("mutate", _MUTATORS, ids=_MALFORMED_IDS)
def test_malformed_files_are_discarded(
    tmp_path: Path, mutate: Callable[[dict[str, object]], object]
) -> None:
    path = write_handoff(tmp_path, _sample())
    raw = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    broken = mutate(raw)
    path.write_text(json.dumps(broken), encoding="utf-8")

    assert load_handoff(tmp_path, expected_writer_pid=os.getpid(), takeover_marker=True) is None
    assert not path.exists()  # stale/malformed files are purged, never re-read


def test_foreign_writer_pid_is_stale(tmp_path: Path) -> None:
    path = write_handoff(tmp_path, _sample())
    assert (
        load_handoff(tmp_path, expected_writer_pid=os.getpid() + 1, takeover_marker=False) is None
    )
    assert not path.exists()


def test_probe_child_sees_live_zombie_and_stranger() -> None:
    live = subprocess.Popen(_SLEEPER)  # noqa: S603 — test's own child
    try:
        assert probe_child(live.pid).state is ChildState.LIVE
        live.kill()
        _wait_zombie(live.pid)
        probed = probe_child(live.pid)
        assert probed.state is ChildState.EXITED
        assert probed.returncode == -signal.SIGKILL
        assert probe_child(live.pid).state is ChildState.NOT_A_CHILD  # already reaped
    finally:
        if live.poll() is None:
            live.kill()
    assert probe_child(os.getpid()).state is ChildState.NOT_A_CHILD  # self is not a child


async def test_adopted_child_reaps_the_inherited_child() -> None:
    child = subprocess.Popen(_SLEEPER)  # noqa: S603 — test's own child
    adopted = adopt_child(child.pid)
    try:
        assert adopted.pid == child.pid
        assert adopted.returncode is None
        os.kill(child.pid, signal.SIGTERM)
        assert await adopted.wait() == -signal.SIGTERM
        assert adopted.returncode == -signal.SIGTERM
        assert await adopted.wait() == -signal.SIGTERM  # stable on re-await
        adopted.terminate()  # no-ops after exit
        adopted.kill()
    finally:
        if child.poll() is None:
            child.kill()


async def test_adopted_child_kill_reaps_the_child() -> None:
    child = subprocess.Popen(_SLEEPER)  # noqa: S603 — test's own child
    adopted = adopt_child(child.pid)
    adopted.kill()
    assert await asyncio.wait_for(adopted.wait(), 5.0) == -signal.SIGKILL
