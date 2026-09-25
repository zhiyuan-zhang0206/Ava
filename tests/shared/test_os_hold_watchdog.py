"""shared.os_hold_watchdog — one job per home, three platforms (task #3887).

The registration is what makes the hold watchdog exist at all on a stopped
box, so the things worth pinning are the ones whose silent failure would undo
it: the launchd job must carry a PATH that can find the repo's tools and must
not fire during the start that registers it, a crontab rewrite must never
clobber the user's real crontab, and the job command must be exactly the one
`shared.hold_watchdog` implements.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shared import os_cron
from shared import os_hold_watchdog as hw
from shared import os_watchdog_probe as probe
from shared.config import settings


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _stable_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "_home_slug", lambda: "ava-deadbeef")
    monkeypatch.setattr("shared.platform.launchd_job_label", lambda: None)

    def _not_descends(_label: str) -> bool:
        return False

    monkeypatch.setattr("shared.platform.descends_from_launchd_job", _not_descends)


def _completed(rc: int = 0, stderr: str = "", stdout: str = "") -> object:
    return type("R", (), {"returncode": rc, "stderr": stderr, "stdout": stdout})()


# --- labels ------------------------------------------------------------------


def test_one_label_per_home() -> None:
    """The hold is host-level state: a two-capability box must complete it in
    ONE place, so the label carries no capability token."""
    assert hw.hold_watchdog_label("ava-deadbeef") == "com.ava.ava-deadbeef.hold-watchdog"


def test_cron_marker_scopes_by_home() -> None:
    assert hw._cron_marker("ava-deadbeef") == "# ava-hold-watchdog.ava-deadbeef"


# --- launchd -----------------------------------------------------------------


def test_plist_carries_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd hands a job a minimal PATH; the watchdog's ladder shells out to
    the repo's own tools, so the job must carry a PATH that can find them."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    content = hw._plist_content(300)
    assert "<key>PATH</key>" in content
    assert "/opt/homebrew/bin" in content


def test_plist_does_not_run_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunAtLoad would fire the watchdog during the very `ava start` whose
    converge registers it - evaluating a transition that is alive and well."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    content = hw._plist_content(300)
    assert "<key>RunAtLoad</key>\n    <false/>" in content


def test_plist_invokes_the_hold_watchdog_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    content = hw._plist_content(120)
    assert "<string>hold-watchdog</string>" in content
    assert "<string>cluster</string>" in content
    assert "<integer>120</integer>" in content


def test_register_macos_writes_and_bootstraps(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        return _completed(rc=113 if cmd[1] == "print" else 0)

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_macos(300) == 0
    plist = fake_home / "Library" / "LaunchAgents" / "com.ava.ava-deadbeef.hold-watchdog.plist"
    assert plist.exists()
    assert [c[1] for c in calls] == ["print", "bootstrap"]


def test_register_macos_reports_a_bootstrap_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        return _completed(rc=113 if cmd[1] == "print" else 1, stderr="boom")

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_macos(300) == 1


def test_register_macos_defers_when_it_is_the_job_itself(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's own ladder runs `ava start`, whose converge step
    registers this job; booting out our own ancestor would kill the
    completion mid-flight (the probe's deferral, same reasoning)."""
    monkeypatch.setattr(
        "shared.platform.launchd_job_label", lambda: "com.ava.ava-deadbeef.hold-watchdog"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda cmd, **_kw: calls.append(cmd) or _completed(),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert hw._register_macos(300) == 0
    assert calls == []


def test_unregister_macos_removes_the_plist(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "com.ava.ava-deadbeef.hold-watchdog.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(hw.subprocess, "run", lambda *_a, **_k: _completed())  # pyright: ignore[reportUnknownArgumentType]
    assert hw._unregister_macos("ava-deadbeef") == 0
    assert not plist.exists()


# --- crontab -----------------------------------------------------------------


def _fake_crontab(existing: str = "") -> tuple[dict[str, str], object]:
    written: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-"]:
            written["body"] = kw["input"]
            return _completed()
        if existing:
            return _completed(rc=0, stdout=existing)
        return _completed(rc=1, stderr="no crontab for u")

    return written, _run


def test_register_linux_skips_when_crontab_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: None)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_linux(300) == 0
    output = capsys.readouterr()
    assert output.out == (
        "  ! hold watchdog: crontab not installed on this host (skipping); "
        "an orphaned maintenance hold will not be completed automatically\n"
    )
    assert output.err == ""


def test_register_linux_preserves_foreign_lines_and_replaces_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    other = "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.ava-deadbeef"
    stale = "*/9 * * * * /old/ava cluster hold-watchdog  " + hw._cron_marker("ava-deadbeef")
    existing = "\n".join(["0 3 * * * /usr/bin/backup.sh", other, stale])
    written, run = _fake_crontab(existing)
    monkeypatch.setattr(hw.subprocess, "run", run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_linux(300) == 0

    body = written["body"]
    assert "/usr/bin/backup.sh" in body  # foreign line untouched
    assert other in body  # the neighbour job untouched
    assert "/old/ava" not in body  # this home's stale line replaced
    assert body.count(hw._cron_marker("ava-deadbeef")) == 1


def test_register_linux_captures_output_and_pins_the_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cron has no StandardErrorPath, so the redirect is the only record; and
    the env prefix must sit immediately before the binary (/bin/sh scopes
    `VAR=v cmd` to that one command)."""
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hw, "ava_binary_path", lambda: "/x/ava")
    written, run = _fake_crontab("")
    monkeypatch.setattr(hw.subprocess, "run", run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_linux(300) == 0

    log_file = Path(settings.general.ava_home) / "logs" / "hold-watchdog.log"
    body = written["body"]
    assert body.startswith("*/5 * * * *")
    assert f"mkdir -p {log_file.parent}" in body
    assert f">> {log_file} 2>&1" in body
    assert f"{hw.cron_env_prefix()}/x/ava cluster hold-watchdog" in body


def test_register_linux_aborts_when_crontab_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        assert cmd != ["crontab", "-"], "must not write after a failed read"
        return _completed(rc=1, stderr="permission denied")

    monkeypatch.setattr(hw.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_linux(300) == 1
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping hold-watchdog registration to avoid clobbering the crontab\n"
    )


def test_register_linux_reports_write_failure_through_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    errors: list[tuple[object, ...]] = []

    def record_error(*args: object) -> None:
        errors.append(args)

    monkeypatch.setattr(hw.logger, "error", record_error)

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-"]:
            return _completed(rc=1, stderr="write denied")
        return _completed(rc=1, stderr="no crontab for u")

    monkeypatch.setattr(hw.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert hw._register_linux(300) == 1
    assert errors == [("crontab update failed for hold watchdog: {}", "write denied")]


def test_unregister_linux_logs_only_after_successful_removal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = hw._cron_marker("ava-deadbeef")
    line = f"*/5 * * * * /x/ava cluster hold-watchdog  {marker}\n"
    body = "0 3 * * * backup\n"
    write_rc = 0
    written: list[str] = []
    infos: list[tuple[object, ...]] = []

    def record_info(*args: object) -> None:
        infos.append(args)

    monkeypatch.setattr(hw.logger, "info", record_info)

    def _run(cmd: list[str], **kw: object) -> object:
        if cmd == ["crontab", "-"]:
            assert isinstance(kw["input"], str)
            written.append(kw["input"])
            return _completed(rc=write_rc)
        return _completed(stdout=body)

    monkeypatch.setattr(hw.subprocess, "run", _run)
    assert hw._unregister_linux("ava-deadbeef") == 0
    assert written == [] and infos == []

    body = line
    write_rc = 1
    assert hw._unregister_linux("ava-deadbeef") == 0
    assert infos == []
    assert capsys.readouterr() == ("", "")

    write_rc = 0
    assert hw._unregister_linux("ava-deadbeef") == 0
    assert written == ["\n", "\n"]
    assert infos == [("crontab hold-watchdog entry removed ({})", marker)]


# --- windows -----------------------------------------------------------------


def test_register_windows_uses_the_shared_minute_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _create(kind: str, args: object, minutes: int, *, time_limit_s: int) -> str | None:
        seen.update(kind=kind, args=args, minutes=minutes, time_limit=time_limit_s)
        return None

    monkeypatch.setattr("shared.os_schtasks.create_minute_task", _create)
    assert hw._register_windows(300) is None
    assert seen["kind"] == "hold-watchdog"
    assert seen["args"] == ("cluster", "hold-watchdog")
    assert seen["minutes"] == 5
    assert seen["time_limit"] == hw._WINDOWS_TIME_LIMIT_S


# --- the public entry points ---------------------------------------------------


def test_register_is_gated_on_os_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "os_jobs_enabled", lambda: False)
    skipped: list[str] = []
    monkeypatch.setattr(hw, "skip_os_job", skipped.append)
    hw.register_hold_watchdog(interval_s=300)
    assert skipped == ["hold-watchdog"]


def test_register_reads_the_interval_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr(settings.gateway, "hold_watchdog_interval_seconds", 120)
    seen: list[int] = []

    class _Backend:
        def register_hold_watchdog(self, interval_s: int) -> None:
            seen.append(interval_s)

    monkeypatch.setattr("shared.platform_backend.get_backend", _Backend)
    hw.register_hold_watchdog()
    assert seen == [120]


def test_unregister_addresses_the_requested_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    class _Backend:
        def unregister_hold_watchdog(self, slug: str) -> None:
            seen.append(slug)

    monkeypatch.setattr("shared.platform_backend.get_backend", _Backend)

    def _slug(_home: Path) -> str:
        return "slug-of-other-home"

    monkeypatch.setattr("shared.cluster.slug_for_home", _slug)
    hw.unregister_hold_watchdog(tmp_path / "other-home")
    assert seen == ["slug-of-other-home"]
