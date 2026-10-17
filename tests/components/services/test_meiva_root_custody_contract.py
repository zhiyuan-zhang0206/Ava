"""Contract: the permissions helper's Swift source, compiled with tests/components/services/helper_child_custody.swift, cannot race owned signal delivery."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.desktop.permissions_helper import lifecycle


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="native Swift child ownership contract",
)
def test_helper_native_reaping_cannot_race_owned_signal_delivery(tmp_path: Path) -> None:
    source = lifecycle.SOURCE.read_text()
    # Compile the actual spawn/session and root-keeper implementations without
    # the GUI server. Test barriers widen the old race, without runtime hooks.
    children = source.split("/// Resolve an allowed path", 1)[0]
    # The keeper's production backoff spans a minute; the fixture runs it scaled down.
    base, cap = 20.0, 40.0
    assert f"static let baseBackoffS = {base}" in source
    assert f"static let maxBackoffS = {cap}" in source
    assert base + min(cap, base * 2) == 60.0  # waits before the 2nd and 3rd attempts
    scaled = source.replace(f"baseBackoffS = {base}", "baseBackoffS = 0.5").replace(
        f"maxBackoffS = {cap}", "maxBackoffS = 1.0"
    )
    keeper = (
        scaled.split("// MARK: - Root keeper", 1)[1]
        .split("// MARK: - Capture indicator", 1)[0]
        .split("// MARK: - Dispatch", 1)[0]
    )
    implementation = (children + keeper).replace("waitpid(", "observedWaitpid(")
    implementation = implementation.replace(
        "kill(child, SIGTERM)", "pausedRootSignal(child, SIGTERM)"
    ).replace("kill(child.pid, signalValue)", "pausedSessionSignal(child.pid, signalValue)")
    implementation = implementation.replace(
        "try StopIntent.store(runDir, owner: .helper)", "try pausedHelperStopIntent(runDir)"
    )
    fixture = Path(__file__).with_name("helper_child_custody.swift").read_text()
    program = tmp_path / "custody.swift"
    program.write_text(implementation + fixture)
    result = subprocess.run(  # noqa: S603 — private native fixture, no daemon socket, signing or scheduler
        ["swift", str(program), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "foreign-child isolation passed" in result.stdout
    assert "helper shutdown admission passed" in result.stdout
    # A separate process loses its controller immediately after durable intent;
    # the next process must observe it before any root or session birth.
    crash_home = tmp_path / "crash-home"
    crash_home.mkdir()
    for mode, expected in (("crash-after-intent", 73), ("restart-after-intent", 0)):
        crash = subprocess.run(  # noqa: S603 — same private native fixture
            ["swift", str(program), str(crash_home), mode],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert crash.returncode == expected, crash.stdout + crash.stderr
    # A root that keeps exiting is restarted a bounded number of times.
    bounded_home = tmp_path / "bounded-home"
    bounded_home.mkdir()
    bounded = subprocess.run(  # noqa: S603 — same private native fixture
        ["swift", str(program), str(bounded_home), "bounded-restart"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert bounded.returncode == 0, bounded.stdout + bounded.stderr
    assert "bounded root restart passed" in bounded.stdout
