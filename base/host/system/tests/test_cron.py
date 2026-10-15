from __future__ import annotations

from pathlib import Path

import pytest

from base import paths
from base.host.system import cron
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)

_UID = 501


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


def _launchctl() -> tuple[FakeCommands, Host]:
    commands = FakeCommands()
    commands.on("launchctl", Reply(), repeat=True)
    return commands, fake_host(commands, platform=DARWIN_ARM64, uid=_UID)


def _desired_plist(_interval_s: int, *, host: Host) -> str:
    return "<desired-plist/>"


def _fixed_ava(*, host: Host) -> str:
    return "/x/ava"


def _never_descendant(_label: str) -> bool:
    return False


def test_register_macos_never_reloads_its_own_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "com.ava.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    monkeypatch.setattr(cron, "launchd_plist_content", _desired_plist)
    commands, host = _launchctl()

    assert cron.register_macos(300, host=host) == 0
    assert plist.read_text() == "<old-plist/>"
    assert commands.argvs == []


def test_register_macos_still_reloads_from_another_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = _plant_plist(fake_home, "com.ava.health-probe")
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.ava.autostart")
    monkeypatch.setattr(cron, "launchd_plist_content", _desired_plist)
    monkeypatch.setattr(cron, "descends_from_launchd_job", _never_descendant)
    commands, host = _launchctl()

    assert cron.register_macos(300, host=host) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]


def _crontab(
    monkeypatch: pytest.MonkeyPatch,
    existing: str = "",
    *,
    read_rc: int = 0,
    read_error: str = "",
    write_rc: int = 0,
    write_error: str = "",
) -> tuple[FakeCommands, Host]:
    monkeypatch.setattr(cron, "ava_binary_path", _fixed_ava)
    commands = FakeCommands()
    commands.on(["crontab", "-l"], Reply(existing, read_error, read_rc), repeat=True)
    commands.on(["crontab", "-"], Reply("", write_error, write_rc), repeat=True)
    host = fake_host(commands, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})
    return commands, host


def _written(commands: FakeCommands) -> str | None:
    bodies = [str(call.kwargs["input"]) for call in commands.calls if call.argv == ("crontab", "-")]
    return bodies[-1] if bodies else None


def test_register_linux_aborts_when_crontab_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, host = _crontab(monkeypatch, read_rc=1, read_error="permission denied")

    assert cron.register_linux(300, host=host) == 1
    assert "avoid clobbering" in capsys.readouterr().err
    assert _written(commands) is None


def test_register_linux_treats_no_crontab_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    commands, host = _crontab(monkeypatch, read_rc=1, read_error="no crontab for u")

    assert cron.register_linux(300, host=host) == 0
    assert "health-probe" in str(_written(commands))


def _local_ava(*, host: Host) -> str:
    return "/Users/x/.local/bin/ava"


def _brew_ava(*, host: Host) -> str:
    return "/opt/homebrew/bin/ava"


def test_launchd_path_env_includes_brew_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "ava_binary_path", _local_ava)
    path = cron.launchd_path_env(host=fake_host(programs={"brew": "/opt/homebrew/bin/brew"}))
    assert "/opt/homebrew/bin" in path
    assert "/Users/x/.local/bin" in path
    assert "/usr/bin" in path


def test_launchd_path_env_falls_back_without_brew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "ava_binary_path", _local_ava)
    path = cron.launchd_path_env(host=fake_host())
    assert "/opt/homebrew/bin" in path
    assert "/usr/local/bin" in path


def test_launchd_path_env_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "ava_binary_path", _brew_ava)
    host = fake_host(programs={"brew": "/opt/homebrew/bin/brew"})
    assert cron.launchd_path_env(host=host).split(":").count("/opt/homebrew/bin") == 1


def test_register_linux_stamps_the_fixed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    commands, host = _crontab(monkeypatch)

    assert cron.register_linux(300, host=host) == 0
    (line,) = str(_written(commands)).splitlines()
    assert line.endswith("cluster health-probe # ava-health-probe")
    assert "--auto-rollback" not in line
    assert "--threshold" not in line


def test_register_linux_keeps_unrelated_lines_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other_job = (
        "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway"
    )
    commands, host = _crontab(monkeypatch, f"0 3 * * * backup\n{other_job}\n")

    assert cron.register_linux(300, host=host) == 0
    assert capsys.readouterr() == (
        "  . crontab entry added (every 5 min)\n",
        "",
    )
    body = str(_written(commands))
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
    commands, host = _crontab(
        monkeypatch,
        read_rc=read_rc,
        read_error=read_error,
        write_rc=write_rc,
        write_error=write_error,
    )

    assert cron.register_linux(300, host=host) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (expected_out, expected_err)
    assert (_written(commands) is not None) == (read_rc == 0)


def test_register_linux_missing_crontab_message_and_rc(capsys: pytest.CaptureFixture[str]) -> None:
    assert cron.register_linux(300, host=fake_host(platform=LINUX_X86)) == 0
    assert capsys.readouterr() == (
        "  ! health probe cron: crontab not installed on this host (skipping); cluster runs without a health-probe cron\n",
        "",
    )


def test_unregister_linux_removes_only_the_probe_line(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = "*/5 * * * * /x/ava cluster health-probe # ava-health-probe"
    commands, host = _crontab(monkeypatch, f"{probe}\n0 3 * * * backup\n")

    assert cron.unregister_linux(host=host) == 0
    assert probe not in str(_written(commands))
    assert "0 3 * * * backup" in str(_written(commands))


def test_unregister_linux_ignores_the_watchdog_probe_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    probe_line = "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.ava-mine"
    commands, host = _crontab(monkeypatch, f"{probe_line}\n")

    assert cron.unregister_linux(host=host) == 0
    assert _written(commands) is None


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
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    existing: str,
    write_rc: int,
    expected_out: str,
    should_write: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands, host = _crontab(monkeypatch, existing, read_rc=read_rc, write_rc=write_rc)

    assert cron.unregister_linux(host=host) == 0
    assert capsys.readouterr() == (expected_out, "")
    assert (_written(commands) is not None) == should_write


def test_unregister_macos_removes_only_the_probe_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    probe = agents / "com.ava.health-probe.plist"
    autostart = agents / "com.ava.autostart.plist"
    probe.write_text("<plist/>")
    autostart.write_text("<plist/>")

    commands, host = _launchctl()

    assert cron.unregister_macos(host=host) == 0
    assert not probe.exists()
    assert autostart.exists()
    assert [argv[-1] for argv in commands.argvs] == [f"gui/{_UID}/com.ava.health-probe"]


def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def register(interval_s: int, *, host: Host) -> int:
        calls.append(str(interval_s))
        return 0

    monkeypatch.setattr(cron, "register_macos", register)
    return calls


def test_register_refused_for_worktree_checkout_against_prod_home(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod_home = default_home / ".ava"
    (prod_home / "source").mkdir(parents=True)
    monkeypatch.setattr(paths, "repo_root", lambda: default_home / "Ava" / ".worktrees" / "r4")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True, host=fake_host(platform=DARWIN_ARM64))

    assert calls == []


def test_register_allowed_from_the_homes_own_source_checkout(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod_home = default_home / ".ava"
    (prod_home / "source").mkdir(parents=True)
    monkeypatch.setattr(paths, "repo_root", lambda: prod_home / "source")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True, host=fake_host(platform=DARWIN_ARM64))

    assert calls == ["300"]


def test_register_allowed_for_a_default_home_without_source(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "repo_root", lambda: default_home / "dev-src")
    calls = _fake_backend(monkeypatch)

    cron.register_os_cron(enabled_reader=lambda: True, host=fake_host(platform=DARWIN_ARM64))

    assert calls == ["300"]


def test_register_macos_defers_when_ancestry_proves_the_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "com.ava.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(cron, "launchd_plist_content", _desired_plist)

    def _is_current(candidate: str) -> bool:
        return candidate == label

    monkeypatch.setattr(cron, "descends_from_launchd_job", _is_current)
    commands, host = _launchctl()

    assert cron.register_macos(300, host=host) == 0
    assert plist.read_text() == "<old-plist/>"
    assert commands.argvs == []


def test_register_macos_reloads_when_env_reads_zero_but_tree_is_external(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = _plant_plist(fake_home, "com.ava.health-probe")
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(cron, "launchd_plist_content", _desired_plist)
    monkeypatch.setattr(cron, "descends_from_launchd_job", _never_descendant)
    commands, host = _launchctl()

    assert cron.register_macos(300, host=host) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]
