"""Windows permissions-helper lifecycle: csc build + schtasks registration.

The real tools are Windows-only and converge runs them in Session 0, so these
tests pin the command shapes with fakes (same pattern as the macOS lifecycle
tests): what is portable is the idempotency logic and the exact argv.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from services.permissions_helper.windows import lifecycle


def _patch_uia_gac(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give `_uia_ref`'s GAC glob a fake layout under tmp_path."""
    gac = tmp_path / "GAC_MSIL"
    dlls = {}
    for name in ("UIAutomationClient", "UIAutomationTypes"):
        d = gac / name / "v4.0_4.0.0.0__31bf3856ad364e35"
        d.mkdir(parents=True)
        dll = d / f"{name}.dll"
        dll.write_bytes(b"")
        dlls[name] = dll

    def fake_glob(self, pat: str):
        for n, dll in dlls.items():
            if pat.startswith(f"{n}/"):
                return (dll,)
        return ()

    monkeypatch.setattr(lifecycle.Path, "glob", fake_glob)  # pyright: ignore[reportUnknownArgumentType]


def test_build_compiles_with_csc_and_skips_when_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "helper.cs"
    src.write_text("// helper")
    monkeypatch.setattr(lifecycle, "_SOURCE", src)
    monkeypatch.setattr(lifecycle, "_CSC_CANDIDATES", (str(tmp_path / "csc.exe"),))
    (tmp_path / "csc.exe").write_bytes(b"")
    gac = tmp_path / "GAC_MSIL"
    for name in ("UIAutomationClient", "UIAutomationTypes"):
        (gac / name / "v4.0_4.0.0.0__31bf3856ad364e35").mkdir(parents=True)
        (gac / name / "v4.0_4.0.0.0__31bf3856ad364e35" / f"{name}.dll").write_bytes(b"")
    gac_dlls = {
        n: gac / n / "v4.0_4.0.0.0__31bf3856ad364e35" / f"{n}.dll"
        for n in ("UIAutomationClient", "UIAutomationTypes")
    }

    def fake_glob(self, pat: str):
        for n, dll in gac_dlls.items():
            if pat.startswith(f"{n}/"):
                return (dll,)
        return ()

    monkeypatch.setattr(lifecycle.Path, "glob", fake_glob)  # pyright: ignore[reportUnknownArgumentType]
    app_dir = tmp_path / "app"
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        if cmd[0] == str(tmp_path / "csc.exe"):
            (tmp_path / "app" / "AvaPermissionsHelper.exe").parent.mkdir(
                parents=True, exist_ok=True
            )
            (tmp_path / "app" / "AvaPermissionsHelper.exe").write_bytes(b"\x00")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    exe, rebuilt = lifecycle.build(app_dir)
    assert rebuilt is True and exe.name == "AvaPermissionsHelper.exe"
    uia = [r for r in recorded[0] if r.startswith("/r:")]
    assert len(uia) == 2 and all("UIAutomation" in r and r.endswith(".dll") for r in uia)
    assert recorded[0][-2:] == ["/out:" + str(exe), str(src)]

    exe2, rebuilt2 = lifecycle.build(app_dir)
    assert rebuilt2 is False and exe2 == exe


def test_build_fails_without_csc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lifecycle, "_CSC_CANDIDATES", (str(tmp_path / "missing.exe"),))
    with pytest.raises(RuntimeError, match=r"no csc\.exe"):
        lifecycle.build(tmp_path)


def test_register_creates_logon_task_and_runs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    exists = [False]

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        if cmd[1] == "/Query":
            return subprocess.CompletedProcess(cmd, 0 if exists[0] else 1, b"", b"")  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    lifecycle.register_and_launch(Path("C:/x/AvaPermissionsHelper.exe"))

    assert recorded[0][1] == "/Query"
    create = recorded[1]
    assert create[1:3] == ["/Create", "/TN"]
    assert create[3] == "AvaPermissionsHelper"
    assert "/SC" in create and create[create.index("/SC") + 1] == "ONLOGON"
    assert "/IT" in create
    assert recorded[2][1] == "/Run"

    # second call: task exists -> no /Create
    exists[0] = True
    lifecycle.register_and_launch(Path("C:/x/AvaPermissionsHelper.exe"))
    assert "/Create" not in recorded[3]


def _stale_exe_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A build context whose exe exists but is older than the source — the
    rebuild case (what a rollout lands on when helper.cs changed). Returns
    (app_dir, exe, csc)."""
    src = tmp_path / "helper.cs"
    src.write_text("// helper")
    monkeypatch.setattr(lifecycle, "_SOURCE", src)
    csc = tmp_path / "csc.exe"
    csc.write_bytes(b"")
    monkeypatch.setattr(lifecycle, "_CSC_CANDIDATES", (str(csc),))
    _patch_uia_gac(monkeypatch, tmp_path)
    app_dir = tmp_path / "app"
    exe = app_dir / "AvaPermissionsHelper.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"old binary")
    old = time.time() - 100.0
    os.utime(exe, (old, old))
    return app_dir, exe, csc


def test_build_stops_running_helper_before_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale exe forces a rebuild; the live helper from the previous converge
    must be stopped FIRST — Windows locks a running executable's file, so an
    overwrite attempt is csc CS0016 (the 2026-08-02 win rollout)."""
    app_dir, exe, csc = _stale_exe_setup(monkeypatch, tmp_path)
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        if cmd[0] == str(csc):
            exe.write_bytes(b"new binary")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    built_exe, rebuilt = lifecycle.build(app_dir)

    assert rebuilt is True
    assert built_exe == exe
    # stop (schtasks /End, then taskkill) precedes the compile
    assert recorded[0][:3] == ["schtasks", "/End", "/TN"]
    assert recorded[1][:2] == ["taskkill", "/IM"]
    assert recorded[2][0] == str(csc)


def test_build_retries_once_when_first_compile_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stop is asynchronous: the process may still hold the file when csc
    runs. One bounded retry (stop again + compile again) covers that race
    before failing loud."""
    app_dir, exe, csc = _stale_exe_setup(monkeypatch, tmp_path)
    recorded: list[list[str]] = []
    csc_calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        if cmd[0] == str(csc):
            csc_calls["n"] += 1
            if csc_calls["n"] == 1:
                return subprocess.CompletedProcess(cmd, 1, b"", b"CS0016: output file in use")  # pyright: ignore[reportUnknownArgumentType]
            exe.write_bytes(b"new binary")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        lifecycle.time,
        "sleep",
        lambda _s: None,  # pyright: ignore[reportUnknownArgumentType]
    )  # no real wait in tests

    built_exe, rebuilt = lifecycle.build(app_dir)

    assert rebuilt is True and built_exe == exe
    assert csc_calls["n"] == 2
    stops = [c for c in recorded if c[0] in ("schtasks", "taskkill")]
    assert len(stops) == 4  # two stop rounds (schtasks /End + taskkill each)


def test_build_rebuilds_when_compile_options_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fleet exe whose helper.cs is current must still be rebuilt when the
    compile options change — the mtime check alone kept a console-subsystem
    helper (visible terminal window in the interactive session, Task #1095)
    alive forever once /target:winexe was added, because nothing ever touched
    helper.cs. The options fingerprint written after each successful compile is
    the rebuild trigger, and the compile argv must carry /target:winexe."""
    src = tmp_path / "helper.cs"
    src.write_text("// helper")
    monkeypatch.setattr(lifecycle, "_SOURCE", src)
    monkeypatch.setattr(lifecycle, "_CSC_CANDIDATES", (str(tmp_path / "csc.exe"),))
    (tmp_path / "csc.exe").write_bytes(b"")
    _patch_uia_gac(monkeypatch, tmp_path)
    app_dir = tmp_path / "app"
    exe = app_dir / "AvaPermissionsHelper.exe"
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        if cmd[0] == str(tmp_path / "csc.exe"):
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"\x00")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    # A pre-existing exe, newer than the source, from a build with OLD options.
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"\x00")
    os.utime(exe, (time.time() + 60, time.time() + 60))
    lifecycle._fingerprint_path(exe).write_text("/nologo\n")

    built, rebuilt = lifecycle.build(app_dir)
    assert rebuilt is True and built == exe
    csc_calls = [c for c in recorded if c and c[0] == str(tmp_path / "csc.exe")]
    assert csc_calls and "/target:winexe" in csc_calls[0]
    fingerprint = lifecycle._fingerprint_path(exe).read_text().strip()
    assert fingerprint == " ".join(lifecycle._COMPILE_OPTIONS)

    _, rebuilt2 = lifecycle.build(app_dir)
    assert rebuilt2 is False
