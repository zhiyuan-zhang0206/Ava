"""Build, register, and launchd-free-manage the Windows permissions helper.

Windows bring-up for the converge phase. There is no launchd and no TCC on
Windows: the helper's "permission" IS its session identity, so this module's
job is to (1) compile the C# helper with the csc.exe that ships with the
.NET Framework every Windows install has, and (2) register a logon scheduled
task that starts it in the user's interactive session (Session 1 / WinSta0).
A helper started from Session 0 (where converge itself runs) would see an
empty hidden desktop; the task's /IT flag is what lands it next to the user.

All steps are idempotent: a current binary is not recompiled, an existing
task is not recreated, and a healthy helper is not bounced.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_WINDOWS_DIR = Path(__file__).resolve().parent
_SOURCE = _WINDOWS_DIR / "helper.cs"
_TASK_NAME = "AvaPermissionsHelper"
# csc.exe ships with .NET Framework (present on every Windows 11 / 10 install;
# the M1 prototype verified this exact path on the fleet Windows box). A dotnet
# SDK (NativeAOT) is preferred when present, but not required.
_CSC_CANDIDATES = (
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
)


def _uia_ref(name: str) -> str:
    """Full path to a GAC_MSIL assembly for csc's /r: reference.

    helper.cs uses System.Windows.Automation; csc does not resolve the simple
    assembly name from the default reference path, so the GAC copy must be
    referenced explicitly. The versioned subdirectory under GAC_MSIL is
    stable on .NET Framework 4.x (Win10/11), but glob for it rather than
    hardcoding a version token.
    """
    matches = sorted(
        Path(r"C:\Windows\Microsoft.NET\assembly\GAC_MSIL").glob(f"{name}/*/{name}.dll")
    )
    if not matches:
        raise RuntimeError(f"GAC assembly {name}.dll not found — is .NET Framework installed?")
    return str(matches[-1])


def _csc() -> str | None:
    for candidate in _CSC_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


# /target:winexe: the helper is a logon scheduled task in the interactive
# session, so a console-subsystem exe opens a visible terminal window that sits
# on the desktop for the whole session — the zero-visible-windows invariant
# (Task #1095) applies to it like to every other background process. Nothing
# needs the console: the transport is a named pipe and logging goes to a file
# (helper.cs). Recorded in the build fingerprint so changing options rebuilds
# fleet exes whose helper.cs is otherwise current (see `build`).
_COMPILE_OPTIONS = ("/nologo", "/target:winexe")


def _compile(csc: str, exe: Path) -> subprocess.CompletedProcess[bytes]:
    refs = [f"/r:{dll}" for dll in (_uia_ref("UIAutomationClient"), _uia_ref("UIAutomationTypes"))]
    return subprocess.run(  # noqa: S603 — fixed csc.exe path, literal args
        [csc, *_COMPILE_OPTIONS, *refs, "/out:" + str(exe), str(_SOURCE)],
        capture_output=True,
        check=False,
    )


def _fingerprint_path(exe: Path) -> Path:
    return exe.with_suffix(".buildargs")


def _exe_is_current(exe: Path) -> bool:
    """Whether the built exe matches both the source and the compile options.

    The mtime check alone kept stale binaries alive across option changes: a
    fleet machine whose helper.cs never changed would keep its old
    console-subsystem exe forever, so a compile-option fix could never land
    without a manual rebuild. The options fingerprint written after each
    successful compile closes that: options drift ⇒ rebuild.
    """
    if not exe.exists() or exe.stat().st_mtime < _SOURCE.stat().st_mtime:
        return False
    fingerprint = _fingerprint_path(exe)
    return fingerprint.exists() and fingerprint.read_text().strip() == " ".join(_COMPILE_OPTIONS)


def _stop_running_helper() -> None:
    """Best-effort stop of a running helper instance.

    The helper is a logon scheduled task that lives in the user's interactive
    session, and Windows locks a running executable's file — a rebuild cannot
    overwrite it (csc CS0016, the 2026-08-02 win rollout: the live helper from
    the previous converge blocked the Phase-B rebuild and failed converge).
    ``schtasks /End`` asks the task to stop; ``taskkill`` is the belt for an
    instance running outside the task. Both are best-effort and idempotent:
    ``converge()`` relaunches the helper right after the build, so a healthy
    helper is merely bounced.
    """
    subprocess.run(  # noqa: S603 — fixed schtasks tool, literal args
        ["schtasks", "/End", "/TN", _TASK_NAME], capture_output=True, check=False
    )
    # fixed taskkill tool, literal args
    subprocess.run(
        ["taskkill", "/IM", "AvaPermissionsHelper.exe", "/F"], capture_output=True, check=False
    )


def build(app_dir: Path) -> tuple[Path, bool]:
    """Compile helper.cs -> app_dir/AvaPermissionsHelper.exe; (path, rebuilt)."""
    app_dir.mkdir(parents=True, exist_ok=True)
    exe = app_dir / "AvaPermissionsHelper.exe"
    csc = _csc()
    if csc is None:
        raise RuntimeError(
            "no csc.exe found (no .NET Framework?) — cannot build the permissions helper"
        )
    if _exe_is_current(exe):
        return exe, False
    # A rebuild overwrites the exe, and Windows locks a running executable's
    # file — a helper live in the user's session from the previous converge
    # makes csc fail with CS0016 (2026-08-02 win rollout). Stop it first;
    # converge()'s register_and_launch relaunches it right after the build.
    if exe.exists():
        _stop_running_helper()
    proc = _compile(csc, exe)
    if proc.returncode != 0 and exe.exists():
        # The stopped process may still be winding down (the stop is
        # best-effort and asynchronous); give it a beat and retry once before
        # failing loud — a second stop is harmless and idempotent.
        time.sleep(1.0)
        _stop_running_helper()
        proc = _compile(csc, exe)
    if proc.returncode != 0:
        raise RuntimeError(
            f"csc failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )
    _fingerprint_path(exe).write_text(" ".join(_COMPILE_OPTIONS) + "\n")
    return exe, True


def _task_exists() -> bool:
    proc = subprocess.run(  # noqa: S603 — fixed schtasks tool, literal args
        ["schtasks", "/Query", "/TN", _TASK_NAME],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def register_and_launch(exe: Path) -> None:
    """Register the logon scheduled task (if absent) and start it now.

    /IT makes the task run in the interactive user session — the one place
    the helper can see the desktop. Run on every converge so a helper that
    died is relaunched without a login.
    """
    if not _task_exists():
        proc = subprocess.run(  # noqa: S603 — fixed schtasks tool, literal args
            [
                "schtasks",
                "/Create",
                "/TN",
                _TASK_NAME,
                "/TR",
                f'"{exe}"',
                "/SC",
                "ONLOGON",
                "/IT",
                "/F",
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"schtasks /Create failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
            )
    subprocess.run(  # noqa: S603 — fixed schtasks tool, literal args
        ["schtasks", "/Run", "/TN", _TASK_NAME], capture_output=True, check=False
    )


def converge() -> None:
    """Idempotent full bring-up: build + register + launch. Raises on failure."""
    from shared.paths import run_dir

    exe, _ = build(run_dir() / "permissions-helper")
    register_and_launch(exe)
