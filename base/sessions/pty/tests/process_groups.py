import os
import signal
import subprocess

from base.sessions.posixproc import process_group_has_live_members

__all__ = ["kill_group_if_alive", "kill_group_or_prove_already_gone"]


def kill_group_or_prove_already_gone(
    proc: subprocess.Popen[bytes] | subprocess.Popen[str], exc: OSError
) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"killpg({proc.pid}, ...) raised {exc!r} but the leader never exited"
        ) from exc
    if process_group_has_live_members(proc.pid):
        raise AssertionError(
            f"killpg({proc.pid}, ...) raised {exc!r} but its process group still has live members"
        ) from exc


def kill_group_if_alive(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError) as exc:
            kill_group_or_prove_already_gone(proc, exc)
