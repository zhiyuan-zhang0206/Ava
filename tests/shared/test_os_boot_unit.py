"""Linux native boot ownership, rendering and acknowledged root handoff."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from shared import os_boot_unit
from shared.boot_policy import BOOT_RETRY_INTERVAL_S
from shared.cluster import home_slug
from shared.os_boot_unit import (
    START_TIMEOUT_S,
    BootUnitContext,
    boot_unit_owns_boot_path,
    install,
    render_unit,
    status,
    systemd_running,
    uninstall,
    unit_enabled,
    unit_name,
)


def _no_binary(_name: str) -> None:
    return None


def _systemctl_binary(_name: str) -> str:
    return "/usr/bin/systemctl"


def _context(tmp_path: Path) -> BootUnitContext:
    return BootUnitContext(
        home=tmp_path / "home",
        repo=tmp_path / "repo",
        user="ava-user",
        group="ava-group",
        home_dir=tmp_path / "user-home",
    )


@pytest.fixture()
def ctx(tmp_path: Path) -> BootUnitContext:
    return _context(tmp_path)


# --- rendering ---------------------------------------------------------------


def test_render_unit_states_the_boot_policy(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    # systemd is the retry supervisor: retry on failure, no attempt cap, one
    # bounded attempt -- the platform-agnostic policy of shared/boot_policy.py.
    assert "Restart=on-failure" in unit
    assert f"RestartSec={BOOT_RETRY_INTERVAL_S}" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert f"TimeoutStartSec={START_TIMEOUT_S}" in unit
    assert "RuntimeMaxSec" not in unit
    assert "Type=notify" in unit and "NotifyAccess=all" in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert "PIDFile" not in unit
    # Boot ordering + identity: the unit runs as the cluster's user, in the
    # checkout, with the checkout venv on PATH.
    assert "After=network-online.target tailscaled.service mihomo.service" in unit
    assert "WantedBy=multi-user.target" in unit
    assert f"User={ctx.user}" in unit and f"Group={ctx.group}" in unit
    assert f"WorkingDirectory={ctx.repo}" in unit
    # Verified on systemd 255: quotes would become part of the path (fatal),
    # while the literal rest-of-line form keeps spaces working.
    assert f'WorkingDirectory="{ctx.repo}"' not in unit
    assert f'Environment="AVA_HOME={ctx.home}"' in unit
    assert f'Environment="HOME={ctx.home_dir}"' in unit
    assert f'Environment="PATH={ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in unit
    # `:` disables $-expansion in the Exec line; the script path is quoted.
    assert f'ExecStart=:"{ctx.repo}/.venv/bin/python" -m cli.main start' in unit


def test_render_unit_quotes_without_shell_expansion(ctx: BootUnitContext) -> None:
    odd = BootUnitContext(ctx.home, Path('/repo "odd" % $x'), ctx.user, ctx.group, ctx.home_dir)
    unit = render_unit(odd)
    assert 'ExecStart=:"/repo \\"odd\\" %% $x/.venv/bin/python" -m cli.main start' in unit
    assert "AVA_BOOT_PROXY_WAIT" not in unit


def test_render_unit_refuses_control_characters(ctx: BootUnitContext) -> None:
    bad = BootUnitContext(
        ctx.home, Path("/repo\nExecStart=/bad"), ctx.user, ctx.group, ctx.home_dir
    )
    with pytest.raises(ValueError, match="control characters"):
        render_unit(bad)


# --- detection ---------------------------------------------------------------


def test_systemd_running_requires_linux_systemctl_and_the_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", False)
    assert systemd_running() is False
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)
    monkeypatch.setattr(os_boot_unit.shutil, "which", _no_binary)
    assert systemd_running() is False
    monkeypatch.setattr(os_boot_unit.shutil, "which", _systemctl_binary)

    # /run/systemd/system is the booted-with-systemd marker; pin the path probe
    # rather than the host so the test reads the same on macOS and in CI.
    def fake_path(_p: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(is_dir=lambda: True)

    monkeypatch.setattr(os_boot_unit, "Path", fake_path)
    assert systemd_running() is True


def test_unit_enabled_reads_the_one_systemctl_seam(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "enabled\n", "")

    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: True)
    monkeypatch.setattr(os_boot_unit, "_systemctl", fake_systemctl)
    assert unit_enabled(ctx.home) is True
    assert calls == [("is-enabled", unit_name(ctx.home))]

    # Anything but the literal word "enabled" is not ownership.
    def disabled(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "disabled\n", "")

    monkeypatch.setattr(os_boot_unit, "_systemctl", disabled)
    assert unit_enabled(ctx.home) is False
    # A host without systemd can never have an enabled unit.
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)
    assert unit_enabled(ctx.home) is False


def test_boot_unit_owns_boot_path_only_when_enabled(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def enabled_for_any_home(_home: Path | None = None) -> bool:
        return True

    monkeypatch.setattr(os_boot_unit, "unit_enabled", enabled_for_any_home)
    assert boot_unit_owns_boot_path(ctx.home) is True

    # The no-argument form asks about this process's own home.
    def enabled_only_for_this_home(home: Path | None = None) -> bool:
        return home == ctx.home

    monkeypatch.setattr(os_boot_unit, "unit_enabled", enabled_only_for_this_home)
    monkeypatch.setattr(os_boot_unit, "ava_home", lambda: ctx.home)
    assert boot_unit_owns_boot_path() is True

    def disabled(_home: Path | None = None) -> bool:
        return False

    monkeypatch.setattr(os_boot_unit, "unit_enabled", disabled)
    assert boot_unit_owns_boot_path(ctx.home) is False


# --- install / uninstall / status --------------------------------------------


def _install_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool
) -> tuple[list[list[str]], dict[str, str]]:
    """Patch the module's seams so install never touches the real host."""
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: True)
    monkeypatch.setattr(os_boot_unit, "SYSTEM_UNIT_DIR", tmp_path / "etc-systemd")

    def unit_enabled(_home: Path | None = None) -> bool:
        return enabled

    monkeypatch.setattr(os_boot_unit, "unit_enabled", unit_enabled)
    recorded: list[list[str]] = []
    installed: dict[str, str] = {}

    def fake_privileged(
        argv: list[str], *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        if argv and argv[0] == "install":
            # The unit file travels as a temp holder; capture what was written.
            installed["destination"] = argv[-1]
            installed["content"] = Path(argv[-2]).read_text()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(os_boot_unit, "_privileged", fake_privileged)
    return recorded, installed


def test_install_enable_writes_unit_and_swaps_the_boot_path(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, installed = _install_seams(monkeypatch, tmp_path, enabled=False)
    removed: list[str] = []

    def unregister_linux(slug: str) -> int:
        removed.append(slug)
        return 0

    def crontab_present(_home: Path | None = None) -> bool:
        return True

    monkeypatch.setattr("shared.os_autostart._unregister_linux", unregister_linux)
    monkeypatch.setattr(os_boot_unit, "_crontab_has_autostart", crontab_present)

    steps = install(context=ctx)

    destination = os_boot_unit.unit_path(ctx.home)
    assert installed["destination"] == str(destination)
    assert installed["content"] == render_unit(ctx)
    argv = recorded
    assert ["systemctl", "daemon-reload"] in argv
    assert ["systemctl", "enable", unit_name(ctx.home)] in argv
    # Enabling swaps the live boot path in the same call: exactly one owner.
    assert removed == [home_slug(ctx.home)]
    assert "removed the crontab autostart entry" in steps
    assert f"enabled {unit_name(ctx.home)}" in steps


def test_install_enable_skips_the_crontab_step_without_a_crontab_binary(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no crontab binary there is no entry to remove: the enable flow must
    report no cron step and must not shell out to a missing binary at all."""
    _install_seams(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(os_boot_unit.shutil, "which", _no_binary)

    def unregister_must_not_run(_slug: str) -> int:
        pytest.fail("no crontab binary: there is no entry to remove")

    monkeypatch.setattr("shared.os_autostart._unregister_linux", unregister_must_not_run)
    steps = install(context=ctx)
    assert "removed the crontab autostart entry" not in steps


def test_crontab_probes_degrade_without_a_crontab_binary(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit.shutil, "which", _no_binary)
    assert os_boot_unit._crontab_lines() == []
    assert os_boot_unit._crontab_has_autostart(ctx.home) is False


def test_install_staged_leaves_the_crontab_entry_live(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, _installed = _install_seams(monkeypatch, tmp_path, enabled=False)

    def crontab_must_not_be_consulted(_home: Path | None = None) -> bool:
        pytest.fail("a staged install must not consult the crontab")

    def unregister_must_not_run(_slug: str) -> int:
        pytest.fail("a staged install must not touch the crontab")

    monkeypatch.setattr(os_boot_unit, "_crontab_has_autostart", crontab_must_not_be_consulted)
    monkeypatch.setattr("shared.os_autostart._unregister_linux", unregister_must_not_run)

    steps = install(context=ctx, enable=False)

    argv = recorded
    assert ["systemctl", "enable", unit_name(ctx.home)] not in argv
    assert ["systemctl", "daemon-reload"] in argv  # the unit file is staged
    assert not any("crontab" in str(item) for item in argv)
    assert f"installed system unit {os_boot_unit.unit_path(ctx.home)}" in steps


def test_install_refuses_without_systemd(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)
    with pytest.raises(RuntimeError, match="systemd"):
        install(context=ctx)


def test_uninstall_removes_only_this_homes_paths(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    monkeypatch.setattr(os_boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)
    recorded: list[list[str]] = []

    def fake_privileged(
        argv: list[str], *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(os_boot_unit, "_privileged", fake_privileged)

    unit = units / unit_name(ctx.home)
    unit.write_text(render_unit(ctx))
    foreign = units / "unrelated.service"
    foreign.write_text("y")
    steps = uninstall(ctx.home)

    assert ["systemctl", "disable", "--now", unit_name(ctx.home)] in recorded
    assert ["rm", "-f", str(unit)] in recorded
    assert ["systemctl", "daemon-reload"] in recorded
    # Only this home's exact paths; a sibling unit is in no command.
    assert str(foreign) not in str(recorded)
    assert foreign.exists()
    assert steps == [f"removed {unit}"]

    # Nothing left to remove -> a no-op (the mocked `rm -f` never ran for real).
    unit.unlink()
    before = list(recorded)
    assert uninstall(ctx.home) == []
    assert recorded == before
    # A non-Linux host never touches /etc, whatever is on disk.
    unit.write_text(render_unit(ctx))
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", False)
    assert uninstall(ctx.home) == []
    assert unit.exists()


def test_privileged_translates_a_missing_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host without sudo must fail actionably (RuntimeError with guidance),
    not surface a raw FileNotFoundError traceback through the CLI wrappers."""

    def run_missing(_cmd: list[str], **_kw: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "sudo")

    monkeypatch.setattr(os_boot_unit.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os_boot_unit.subprocess, "run", run_missing)

    with pytest.raises(RuntimeError, match="passwordless sudo"):
        os_boot_unit._privileged(["true"])


def test_status_reports_the_read_only_surface(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    monkeypatch.setattr(os_boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(os_boot_unit, "_default_context", lambda: ctx)
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)

    def crontab_absent(_home: Path | None = None) -> bool:
        return False

    monkeypatch.setattr(os_boot_unit, "_crontab_has_autostart", crontab_absent)

    rows = dict(status(ctx.home))
    assert rows["unit"] == unit_name(ctx.home)
    assert rows["unit file"] == f"{os_boot_unit.unit_path(ctx.home)} (missing)"
    assert rows["cron entry"] == "absent"
    assert rows["systemd state"] == "not running (the boot unit needs systemd)"

    # The installed+enabled state is all visible without a single write.
    unit_file = os_boot_unit.unit_path(ctx.home)
    unit_file.write_text(render_unit(ctx))
    rows = dict(status(ctx.home))
    assert rows["unit file"] == f"{unit_file} (present)"
    assert rows["unit content"] == "matches rendered"


def test_status_does_not_compare_a_foreign_home(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign home renders from its own checkout/user by construction; the
    content comparison must not read as a spurious "differs"."""
    units = tmp_path / "etc-systemd"
    units.mkdir()
    other = tmp_path / "other-home"
    foreign = BootUnitContext(
        home=other, repo=ctx.repo, user=ctx.user, group=ctx.group, home_dir=ctx.home_dir
    )
    (units / unit_name(other)).write_text(render_unit(foreign))

    monkeypatch.setattr(os_boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(os_boot_unit, "_default_context", lambda: ctx)
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)

    def crontab_absent(_home: Path | None = None) -> bool:
        return False

    monkeypatch.setattr(os_boot_unit, "_crontab_has_autostart", crontab_absent)

    rows = dict(status(other))
    assert rows["unit content"] == "not compared (not this process's home)"

    # The own home still compares (and matches the renders this code produces).
    (units / unit_name(ctx.home)).write_text(render_unit(ctx))
    own_rows = dict(status(ctx.home))
    assert own_rows["unit content"] == "matches rendered"


def test_paths_and_names_are_home_scoped(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert unit_name(first) == f"ava-boot.{home_slug(first)}.service"
    assert unit_name(first) != unit_name(second)
    assert os_boot_unit.unit_path(first) == os_boot_unit.SYSTEM_UNIT_DIR / unit_name(first)


def test_interactive_start_never_notifies(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.proc_tree import OwnedProcess

    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)

    def interactive(_pid: int) -> str:
        return "/user.slice/interactive.scope"

    def no_manager(_home: Path) -> dict[str, str]:
        pytest.fail("interactive")

    monkeypatch.setattr(os_boot_unit, "_process_cgroup", interactive)
    monkeypatch.setattr(os_boot_unit, "_manager_properties", no_manager)
    os_boot_unit.notify_root_ready(ctx.home, OwnedProcess(123, 1.0, 1))


@pytest.mark.parametrize("fault", ["dead", "foreign", "manager", "reused", "not_adopted", "none"])
def test_root_notification_binds_native_custody(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from shared.proc_tree import OwnedProcess

    owner = OwnedProcess(123, 1.0, 1)
    expected = f"/system.slice/{unit_name(ctx.home)}"

    def in_unit(_home: Path) -> bool:
        return True

    def cgroup(_pid: int) -> str:
        return "/foreign" if fault == "foreign" else expected

    monkeypatch.setattr(os_boot_unit, "in_boot_unit", in_unit)
    monkeypatch.setattr(os_boot_unit, "_process_cgroup", cgroup)
    alive = iter([False] if fault == "dead" else [True, fault != "reused"])

    def live(_self: OwnedProcess) -> bool:
        return next(alive)

    monkeypatch.setattr(OwnedProcess, "live", live)
    snapshots = iter(
        [
            {
                "MainPID": "999" if fault == "manager" else str(os_boot_unit.os.getpid()),
                "ControlGroup": expected,
                "ActiveState": "activating",
            },
            {
                "MainPID": "999" if fault == "not_adopted" else str(owner.pid),
                "ControlGroup": expected,
                "ActiveState": "active",
            },
        ]
    )

    def properties(_home: Path) -> dict[str, str]:
        return next(snapshots)

    monkeypatch.setattr(os_boot_unit, "_manager_properties", properties)
    calls: list[list[str]] = []

    def notify(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(os_boot_unit.subprocess, "run", notify)
    if fault == "none":
        os_boot_unit.notify_root_ready(ctx.home, owner)
    else:
        with pytest.raises(RuntimeError, match=r"custody|retain"):
            os_boot_unit.notify_root_ready(ctx.home, owner)
    assert calls == (
        []
        if fault in {"dead", "foreign", "manager"}
        else [["systemd-notify", "--pid=123", "--ready"]]
    )


def test_uninstall_failed_stop_preserves_unit(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)
    monkeypatch.setattr(os_boot_unit, "SYSTEM_UNIT_DIR", tmp_path)
    target = os_boot_unit.unit_path(ctx.home)
    target.write_text(render_unit(ctx))
    calls: list[list[str]] = []

    def fail_stop(argv: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "stop failed")

    monkeypatch.setattr(os_boot_unit, "_privileged", fail_stop)
    with pytest.raises(RuntimeError, match="stop failed"):
        uninstall(ctx.home)
    assert target.exists()
    assert calls == [["systemctl", "disable", "--now", unit_name(ctx.home)]]
