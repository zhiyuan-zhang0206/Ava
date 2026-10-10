"""base.host.system.cron — fixed labels, launchd ownership, crontab safety."""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from base.host.system import cron


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _plant_plist(fake_home: Path, label: str) -> Path:
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text("<plist/>")
    return p


def _record_launchctl(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def _desired_plist(_interval_s: int) -> str:
    return "<desired-plist/>"


def _never_descendant(_label: str) -> bool:
    return False


def test_register_macos_never_reloads_its_own_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration cannot boot out the job containing its own process."""
    label = "com.ava.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    monkeypatch.setattr(cron, "_launchd_plist_content", _desired_plist)
    calls = _record_launchctl(monkeypatch)

    assert cron._register_macos(300) == 0
    assert plist.read_text() == "<old-plist/>"
    assert calls == []


def test_register_macos_still_reloads_from_another_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-protection is label-specific: boot autostart also runs
    ``ava start`` under launchd and must still converge the health probe."""
    plist = _plant_plist(fake_home, "com.ava.health-probe")
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.ava.autostart")
    monkeypatch.setattr(cron, "_launchd_plist_content", _desired_plist)
    monkeypatch.setattr(cron, "descends_from_launchd_job", _never_descendant)
    calls = _record_launchctl(monkeypatch)

    assert cron._register_macos(300) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]


def test_register_linux_aborts_when_crontab_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `crontab -l` failure that is NOT the benign 'no crontab for <user>'
    (permissions, broken cron) must abort — proceeding would rewrite the user's
    real crontab from an empty read."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        raise AssertionError(f"must not reach a crontab write: {cmd}")

    monkeypatch.setattr(cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert cron._register_linux(300) == 1
    assert "avoid clobbering" in capsys.readouterr().err


def test_register_linux_treats_no_crontab_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/x/ava")
    writes: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for u")
        if cmd == ["crontab", "-"]:
            writes["input"] = kw.get("input", "")  # pyright: ignore[reportUnknownMemberType]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert cron._register_linux(300) == 0
    assert "health-probe" in writes["input"]


def test_launchd_path_env_includes_brew_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd hands a job a minimal PATH without Homebrew's bin. Both LaunchAgents
    this repo writes (autostart, watchdog probe) shell out to the repo's tools, so the composed
    PATH must carry the brew prefix, the dir holding `ava`, and the system dirs."""
    import shutil

    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,  # pyright: ignore[reportUnknownArgumentType]
    )
    path = cron.launchd_path_env()
    assert "/opt/homebrew/bin" in path  # brew bin (launchd omits it)
    assert "/Users/x/.local/bin" in path  # dir holding `ava`
    assert "/usr/bin" in path  # base system dirs


def test_launchd_path_env_falls_back_without_brew(monkeypatch: pytest.MonkeyPatch) -> None:
    """No brew on PATH (a fresh box, or launchd's own stripped env): fall back to
    the standard Apple-silicon / Intel prefixes rather than emitting no brew dir."""
    import shutil

    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    path = cron.launchd_path_env()
    assert "/opt/homebrew/bin" in path
    assert "/usr/local/bin" in path


def test_launchd_path_env_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava` living in the brew prefix must not produce a doubled entry."""
    import shutil

    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/opt/homebrew/bin/ava")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert cron.launchd_path_env().split(":").count("/opt/homebrew/bin") == 1


# ---------------------------------------------------------------------------
# The crontab entry
# ---------------------------------------------------------------------------
# Its marker names the job, not a home: the host runs one cluster. Migration of
# lines older versions wrote with a per-home suffix is in test_os_job_markers.py.


def _crontab_stub(
    monkeypatch: pytest.MonkeyPatch,
    existing: str,
    writes: dict[str, str],
    *,
    read_rc: int = 0,
    read_error: str = "",
    write_rc: int = 0,
    write_error: str = "",
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cron, "ava_binary_path", lambda: "/x/ava")

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=read_rc, stdout=existing, stderr=read_error)
        if cmd == ["crontab", "-"]:
            writes["input"] = kw.get("input", "")  # pyright: ignore[reportUnknownMemberType]
        return types.SimpleNamespace(returncode=write_rc, stdout="", stderr=write_error)

    monkeypatch.setattr(cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]


def test_register_linux_stamps_the_fixed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, "", writes)

    assert cron._register_linux(300) == 0
    (line,) = writes["input"].splitlines()
    assert line.endswith("cluster health-probe # ava-health-probe")
    assert "--auto-rollback" not in line
    assert "--threshold" not in line


def test_register_linux_keeps_unrelated_lines_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other_job = (
        "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway"
    )
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"0 3 * * * backup\n{other_job}\n", writes)

    assert cron._register_linux(300) == 0
    assert capsys.readouterr() == (
        "  . crontab entry added (every 5 min)\n",
        "",
    )
    body = writes["input"]
    assert "0 3 * * * backup" in body and other_job in body
    assert body.count("# ava-health-probe") == 1


@pytest.mark.parametrize(
    ("read_rc", "read_error", "write_rc", "write_error", "expected_out", "expected_err"),
    [
        (
            1,
            "permission denied",
            0,
            "",
            "",
            "  * crontab -l failed (permission denied); skipping health-probe registration to avoid clobbering the crontab\n",
        ),
        (0, "", 1, "disk full", "", "  * crontab update failed: disk full\n"),
    ],
)
def test_register_linux_failure_messages_and_rc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    read_error: str,
    write_rc: int,
    write_error: str,
    expected_out: str,
    expected_err: str,
) -> None:
    writes: dict[str, str] = {}
    _crontab_stub(
        monkeypatch,
        "",
        writes,
        read_rc=read_rc,
        read_error=read_error,
        write_rc=write_rc,
        write_error=write_error,
    )

    assert cron._register_linux(300) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (expected_out, expected_err)
    assert ("input" in writes) == (read_rc == 0)


def test_register_linux_missing_crontab_message_and_rc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cron.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]

    assert cron._register_linux(300) == 0
    assert capsys.readouterr() == (
        "  ! health probe cron: crontab not installed on this host (skipping); cluster runs without a health-probe cron\n",
        "",
    )


def test_unregister_linux_removes_only_the_probe_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = "*/5 * * * * /x/ava cluster health-probe # ava-health-probe"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{probe}\n0 3 * * * backup\n", writes)

    assert cron._unregister_linux() == 0
    assert probe not in writes["input"]
    assert "0 3 * * * backup" in writes["input"]


def test_unregister_linux_ignores_the_watchdog_probe_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog probe writes its own crontab lines with its own marker; a
    health-probe unregister must not sweep them away."""
    probe_line = "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.ava-mine"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{probe_line}\n", writes)

    assert cron._unregister_linux() == 0
    assert writes == {}  # nothing matched, so the crontab was never rewritten


@pytest.mark.parametrize(
    ("read_rc", "existing", "write_rc", "expected_out", "should_write"),
    [
        (1, "", 0, "  . no crontab to unregister\n", False),
        (0, "0 3 * * * backup\n", 0, "  . no Ava health-probe entry found in crontab\n", False),
        (
            0,
            "*/5 * * * * /x/ava cluster health-probe # ava-health-probe\n",
            0,
            "  . crontab entry removed\n",
            True,
        ),
        (0, "*/5 * * * * /x/ava cluster health-probe # ava-health-probe\n", 1, "", True),
    ],
)
def test_unregister_linux_messages_and_success_gated_removal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    existing: str,
    write_rc: int,
    expected_out: str,
    should_write: bool,
) -> None:
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, existing, writes, read_rc=read_rc, write_rc=write_rc)

    assert cron._unregister_linux() == 0
    assert capsys.readouterr() == (expected_out, "")
    assert ("input" in writes) == should_write


def test_unregister_macos_removes_only_the_probe_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem-level proof of the scoping: with another job's plist on disk,
    unregistering the probe leaves it untouched and boots out the probe's label."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    probe = agents / "com.ava.health-probe.plist"
    autostart = agents / "com.ava.autostart.plist"
    probe.write_text("<plist/>")
    autostart.write_text("<plist/>")

    booted: list[str] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        booted.append(cmd[-1])  # pyright: ignore[reportUnknownArgumentType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

    assert cron._unregister_macos() == 0
    assert not probe.exists()
    assert autostart.exists()
    assert booted == [f"gui/{os.getuid()}/com.ava.health-probe"]


# ── registration guard: worktree checkout must not register prod's probe ──


def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the platform backend's register_cron to record calls."""
    calls: list[str] = []

    class _FakeBackend:
        def register_cron(self, interval_s: int) -> None:  # type: ignore[no-untyped-def]
            calls.append(str(interval_s))

    monkeypatch.setattr("base.host.system.backend.get_backend", _FakeBackend)
    return calls


def test_register_refused_for_worktree_checkout_against_prod_home(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #1025: a worktree process (a home with its own `source` + another
    checkout) must not register that home's health-probe plist — the 2026-08-07
    accident where a worktree-venv debug script rewrote the plist and the probe
    auto-rolled-back the cluster."""
    prod_home = default_home / ".ava"
    (prod_home / "source").mkdir(parents=True)
    monkeypatch.setattr("base.paths.repo_root", lambda: default_home / "Ava" / ".worktrees" / "r4")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True)

    assert calls == []  # backend never called; registration refused


def test_register_allowed_from_the_homes_own_source_checkout(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home's own `source` checkout registers normally."""
    prod_home = default_home / ".ava"
    (prod_home / "source").mkdir(parents=True)
    monkeypatch.setattr("base.paths.repo_root", lambda: prod_home / "source")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True)

    assert calls == ["300"]


def test_register_allowed_for_a_default_home_without_source(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default home with no checkout of its own runs whichever checkout registers it."""
    monkeypatch.setattr("base.paths.repo_root", lambda: default_home / "dev-src")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True)

    assert calls == ["300"]


def test_register_macos_defers_when_ancestry_proves_the_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On current macOS an exec'd descendant reads XPC_SERVICE_NAME="0", not the
    label (2026-09-17 recurrence), so the live process-tree check must defer
    even when the environment value is "0"."""
    label = "com.ava.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(cron, "_launchd_plist_content", _desired_plist)

    def _is_current(candidate: str) -> bool:
        return candidate == label

    monkeypatch.setattr(cron, "descends_from_launchd_job", _is_current)
    calls = _record_launchctl(monkeypatch)

    assert cron._register_macos(300) == 0
    assert plist.read_text() == "<old-plist/>"
    assert calls == []


def test_register_macos_reloads_when_env_reads_zero_but_tree_is_external(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "0" descendant reading must not block a legitimate external converge
    from applying a pending spec change."""
    plist = _plant_plist(fake_home, "com.ava.health-probe")
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(cron, "_launchd_plist_content", _desired_plist)
    monkeypatch.setattr(cron, "descends_from_launchd_job", _never_descendant)
    calls = _record_launchctl(monkeypatch)

    assert cron._register_macos(300) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]
