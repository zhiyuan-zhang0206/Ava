"""Drain exact home-owned OS jobs and verify their resource exits."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from pathlib import Path

import psutil

from cli.commands._maintenance_stop import (
    OwnedProcess,
    capture_tree,
    deadline_after,
    remaining,
    wait_for_exit,
)


def _capture(pid: int) -> set[OwnedProcess]:
    if pid <= 0:
        return set()
    try:
        return capture_tree(OwnedProcess.capture(psutil.Process(pid)))
    except psutil.NoSuchProcess:
        return set()


def _force(tracked: set[OwnedProcess]) -> None:
    for process in tracked:
        if process.live():
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(process.pid).kill()


def _launchctl(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def _launchd_state(target: str, deadline: float) -> str | None:
    result = _launchctl("print", target, timeout=remaining(deadline))
    if result.returncode == 113 and "Could not find service" in result.stderr:
        return None
    if result.returncode:
        raise RuntimeError(f"Cannot inspect launchd job {target}: {result.stderr.strip()}")
    return result.stdout


def stop_launchd(label: str, *, force: bool, timeout_s: float) -> None:
    """Drain running processes before removing the exact job registration.

    On macOS, disable does not inhibit a loaded KeepAlive job, and bootout can
    kill a running process even with ExitTimeOut=0. Send TERM ourselves and wait
    for the original work to exit before bootout. If KeepAlive already launched
    a replacement, drain it too. The final no-PID read and bootout are not atomic;
    this proves the observed generations exited, not a launchd admission fence.
    """
    deadline = deadline_after(timeout_s)
    target = f"gui/{os.getuid()}/{label}"
    while True:
        state = _launchd_state(target, deadline)
        if state is None:
            return
        match = re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
        if match and int(match[1]) > 0:
            stop_detached(int(match[1]), force=force, timeout_s=remaining(deadline))
            continue
        result = _launchctl("bootout", target, timeout=remaining(deadline))
        if result.returncode and _launchd_state(target, deadline) is not None:
            raise RuntimeError(f"Cannot unload launchd job {label}: {result.stderr.strip()}")
        # Removal must be visible externally, not merely accepted by launchd.
        while _launchd_state(target, deadline) is not None:
            time.sleep(min(0.05, remaining(deadline)))
        return


def _systemd_state(unit: str, deadline: float) -> dict[str, str]:
    from cli.commands._gate_systemd import _systemctl

    result = _systemctl(
        "show",
        unit,
        "--property=LoadState,ActiveState,MainPID,SendSIGKILL,FragmentPath",
        timeout=remaining(deadline),
    )
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and state.get("LoadState") != "not-found":
        raise RuntimeError(f"Cannot inspect user unit {unit}: {result.stderr.strip()}")
    return state


def stop_systemd(
    unit: str, *, force: bool, timeout_s: float, expected_fragment: Path | None = None
) -> None:
    """Stop a caller-verified user unit, retaining its definition and enablement."""
    from cli.commands._gate_systemd import _systemctl

    deadline = deadline_after(timeout_s)
    state = _systemd_state(unit, deadline)
    if state["LoadState"] == "not-found":
        return
    if (
        expected_fragment is not None
        and Path(state["FragmentPath"]).resolve() != expected_fragment.resolve()
    ):
        raise RuntimeError(f"Refusing a foreign user unit: {unit}")
    if not force and state["SendSIGKILL"] != "no":
        raise RuntimeError(
            f"Loaded unit {unit} permits an automatic force kill; run ava converge "
            "to load the current definition before stopping, or explicitly use --force"
        )
    tracked = _capture(int(state["MainPID"]))
    result = _systemctl("stop", "--no-block", unit, timeout=remaining(deadline))
    if result.returncode:
        raise RuntimeError(f"Cannot stop user unit {unit}: {result.stderr.strip()}")
    if force:
        result = _systemctl(
            "kill", "--kill-whom=all", "--signal=SIGKILL", unit, timeout=remaining(deadline)
        )
        if result.returncode and _systemd_state(unit, deadline)["ActiveState"] not in {
            "inactive",
            "failed",
        }:
            raise RuntimeError(f"Cannot force-stop user unit {unit}: {result.stderr.strip()}")
        _force(tracked)
    wait_for_exit(tracked, deadline)
    while True:
        state = _systemd_state(unit, deadline)
        if state["ActiveState"] in {"inactive", "failed"} and int(state["MainPID"]) == 0:
            return
        time.sleep(min(0.05, remaining(deadline)))


def stop_detached(pid: int, *, force: bool, timeout_s: float) -> None:
    """Signal a caller-verified POSIX daemon, retaining identity through wait."""
    import signal

    tracked = _capture(pid)
    for process in tracked:
        if process.live():
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(process.pid).send_signal(signal.SIGTERM)
    if force:
        _force(tracked)
    wait_for_exit(tracked, deadline_after(timeout_s))
