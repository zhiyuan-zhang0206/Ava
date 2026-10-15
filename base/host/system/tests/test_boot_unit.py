from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from base.host.system import boot_unit
from base.host.system.boot_policy import BOOT_MAX_ATTEMPTS, BOOT_RETRY_INTERVAL_S
from base.host.system.boot_unit import (
    START_LIMIT_INTERVAL_S,
    START_TIMEOUT_S,
    BootUnitContext,
    install,
    render_unit,
    systemd_running,
    uninstall,
    unit_enabled,
)
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)


def _linux() -> Host:
    return fake_host(platform=LINUX_X86)


def _systemd_up(*, host: Host) -> bool:
    return True


def _systemd_down(*, host: Host) -> bool:
    return False


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


def test_render_unit_states_the_boot_policy(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    assert "Restart=on-failure" in unit
    assert f"RestartSec={BOOT_RETRY_INTERVAL_S}" in unit
    assert f"StartLimitBurst={BOOT_MAX_ATTEMPTS}" in unit
    assert f"StartLimitIntervalSec={START_LIMIT_INTERVAL_S}" in unit
    assert "StartLimitIntervalSec=0" not in unit
    assert "start" in unit and '"--boot"' in unit
    assert f"TimeoutStartSec={START_TIMEOUT_S}" in unit
    assert "RuntimeMaxSec" not in unit
    assert "Type=forking" in unit and "GuessMainPID=no" in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert f"PIDFile={boot_unit.root_pid_path(ctx.home)}" in unit


def test_render_unit_binds_home_and_checkout(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    assert "After=network-online.target\n" in unit
    assert "WantedBy=multi-user.target" in unit
    assert f"User={ctx.user}" in unit and f"Group={ctx.group}" in unit
    assert f"WorkingDirectory={ctx.repo}" in unit
    assert f'WorkingDirectory="{ctx.repo}"' not in unit
    assert f'Environment="AVA_HOME={ctx.home}"' in unit
    assert "AVA_HOST_STATE_DIR" not in unit
    assert f'Environment="HOME={ctx.home_dir}"' in unit
    assert f'Environment="PATH={ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in unit
    assert f'ExecStart=:"{ctx.repo}/.venv/bin/python" "-m" "cli.main" "start"' in unit


def test_render_unit_quotes_without_shell_expansion(ctx: BootUnitContext) -> None:
    odd = BootUnitContext(ctx.home, Path('/repo "odd" % $x'), ctx.user, ctx.group, ctx.home_dir)
    unit = render_unit(odd)
    assert 'ExecStart=:"/repo \\"odd\\" %% $x/.venv/bin/python" "-m" "cli.main" "start"' in unit
    assert "AVA_BOOT_PROXY_WAIT" not in unit


def test_render_unit_refuses_control_characters(ctx: BootUnitContext) -> None:
    bad = BootUnitContext(
        ctx.home, Path("/repo\nExecStart=/bad"), ctx.user, ctx.group, ctx.home_dir
    )
    with pytest.raises(ValueError, match="control characters"):
        render_unit(bad)


def test_systemd_running_requires_linux_systemctl_and_the_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = {"systemctl": "/usr/bin/systemctl"}
    assert systemd_running(host=fake_host(platform=DARWIN_ARM64, programs=programs)) is False
    assert systemd_running(host=_linux()) is False
    linux = fake_host(platform=LINUX_X86, programs=programs)

    def fake_path(_p: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(is_dir=lambda: True)

    monkeypatch.setattr(boot_unit, "Path", fake_path)
    assert systemd_running(host=linux) is True


def test_unit_enabled_reads_the_one_systemctl_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot_unit, "systemd_running", _systemd_up)
    commands = FakeCommands()
    commands.on(["systemctl", "is-enabled"], Reply("enabled\n"), Reply("disabled\n"))
    host = fake_host(commands, platform=LINUX_X86)
    assert unit_enabled(host=host) is True
    assert commands.argvs == [("systemctl", "is-enabled", "ava-boot.service")]

    assert unit_enabled(host=host) is False
    monkeypatch.setattr(boot_unit, "systemd_running", _systemd_down)
    assert unit_enabled(host=host) is False
    assert len(commands.calls) == 2


def _install_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool
) -> tuple[list[list[str]], dict[str, str]]:
    monkeypatch.setattr(boot_unit, "systemd_running", _systemd_up)
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", tmp_path / "etc-systemd")

    def unit_enabled(*, host: Host) -> bool:
        return enabled

    monkeypatch.setattr(boot_unit, "unit_enabled", unit_enabled)
    recorded: list[list[str]] = []
    installed: dict[str, str] = {}

    def fake_privileged(
        argv: list[str],
        *,
        timeout: float = 120.0,
        host: Host,
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        if argv and argv[0] == "install":
            installed["destination"] = argv[-1]
            installed["content"] = Path(argv[-2]).read_text()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)
    return recorded, installed


def test_install_refuses_without_systemd(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot_unit, "systemd_running", _systemd_down)
    with pytest.raises(RuntimeError, match="systemd"):
        install(context=ctx, host=_linux())


def test_uninstall_removes_only_the_boot_units_paths(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", units)
    recorded: list[list[str]] = []

    def fake_privileged(
        argv: list[str],
        *,
        timeout: float = 120.0,
        host: Host,
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)

    unit = units / "ava-boot.service"
    unit.write_text(render_unit(ctx))
    foreign = units / "unrelated.service"
    foreign.write_text("y")
    template = units / "ava-gateway-egress@.service"
    template.write_text("z")
    steps = uninstall(host=_linux())

    assert ["systemctl", "disable", "--now", "ava-boot.service"] in recorded
    assert ["rm", "-f", str(unit)] in recorded
    assert ["systemctl", "daemon-reload"] in recorded
    assert str(foreign) not in str(recorded) and "ava-gateway-egress" not in str(recorded)
    assert foreign.exists() and template.exists()
    assert steps == [f"removed {unit}"]

    unit.unlink()
    before = list(recorded)
    assert uninstall(host=_linux()) == []
    assert recorded == before
    unit.write_text(render_unit(ctx))
    assert uninstall(host=fake_host(platform=DARWIN_ARM64)) == []
    assert unit.exists()


def _manager_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, active_state: str
) -> tuple[Path, list[list[str]]]:
    units = tmp_path / "etc-systemd"
    units.mkdir(exist_ok=True)
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(boot_unit, "systemd_running", _systemd_up)
    recorded: list[list[str]] = []

    def fake_privileged(
        argv: list[str],
        *,
        timeout: float = 120.0,
        host: Host,
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def show(
        *args: str,
        timeout: float = 30.0,
        host: Host,
    ) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ("show", "--property=ActiveState", "--value"), args
        return subprocess.CompletedProcess(["systemctl", *args], 0, f"{active_state}\n", "")

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)
    monkeypatch.setattr(boot_unit, "systemctl", show)
    return units, recorded


def test_uninstall_leaves_no_failed_record_behind(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units, recorded = _manager_reports(monkeypatch, tmp_path, "failed")
    name = "ava-boot.service"
    (units / name).write_text(render_unit(ctx))

    steps = uninstall(host=_linux())

    reset = ["systemctl", "reset-failed", name]
    assert recorded.index(["systemctl", "daemon-reload"]) < recorded.index(reset)
    assert steps[-1] == f"cleared the failed record of {name}"
    (units / name).unlink()
    recorded.clear()
    assert uninstall(host=_linux()) == [f"cleared the failed record of {name}"]
    assert recorded == [reset]


def test_uninstall_resets_nothing_it_did_not_leave_failed(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _units, recorded = _manager_reports(monkeypatch, tmp_path, "inactive")
    assert uninstall(host=_linux()) == []
    assert recorded == []


def test_privileged_translates_a_missing_sudo() -> None:
    commands = FakeCommands()
    commands.on("sudo", FileNotFoundError(2, "No such file or directory", "sudo"))

    with pytest.raises(RuntimeError, match="passwordless sudo"):
        boot_unit.privileged(["true"], host=fake_host(commands, uid=1000))


def test_privileged_runs_a_root_command_directly_and_others_through_sudo() -> None:
    commands = FakeCommands()
    commands.on(["true"], Reply())
    commands.on(["sudo"], Reply())
    boot_unit.privileged(["true"], host=fake_host(commands, uid=0))
    boot_unit.privileged(["true"], host=fake_host(commands, uid=1000))
    assert commands.argvs == [("true",), ("sudo", "-n", "true")]


def test_the_boot_unit_carries_no_home() -> None:
    assert boot_unit.UNIT_NAME == "ava-boot.service"
    assert boot_unit.unit_path() == boot_unit.SYSTEM_UNIT_DIR / "ava-boot.service"


def test_interactive_start_never_publishes(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.native_process.ownership import OwnedProcess

    def interactive(_pid: int) -> str:
        return "/user.slice/interactive.scope"

    def no_manager(*, host: Host) -> dict[str, str]:
        pytest.fail("interactive")

    monkeypatch.setattr(boot_unit, "process_cgroup", interactive)
    monkeypatch.setattr(boot_unit, "manager_properties", no_manager)
    boot_unit.publish_root_ready(ctx.home, OwnedProcess(123, 1.0, 1), host=_linux())


@pytest.mark.parametrize("fault", ["dead", "foreign", "manager", "reused", "none"])
def test_root_publication_binds_native_custody(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from base.native_process.ownership import OwnedProcess

    owner = OwnedProcess(123, 1.0, 1)
    expected = "/system.slice/ava-boot.service"

    def in_unit(*, host: Host) -> bool:
        return True

    def cgroup(_pid: int) -> str:
        return "/foreign" if fault == "foreign" else expected

    alive = iter([False] if fault == "dead" else [True, fault != "reused"])

    def live(_self: OwnedProcess) -> bool:
        return next(alive)

    def properties(*, host: Host) -> dict[str, str]:
        return {
            "MainPID": "0",
            "ControlPID": "999" if fault == "manager" else str(boot_unit.os.getpid()),
            "ControlGroup": expected,
            "ActiveState": "activating",
        }

    monkeypatch.setattr(boot_unit, "in_boot_unit", in_unit)
    monkeypatch.setattr(boot_unit, "process_cgroup", cgroup)
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(boot_unit, "manager_properties", properties)
    path = boot_unit.root_pid_path(ctx.home)
    if fault == "none":
        path.parent.mkdir(parents=True)
        path.write_text("999999\n")
        boot_unit.publish_root_ready(ctx.home, owner, host=_linux())
        assert path.read_text() == "123\n"
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(RuntimeError, match=r"custody|birth"):
            boot_unit.publish_root_ready(ctx.home, owner, host=_linux())
        assert not path.exists()


def test_uninstall_failed_stop_preserves_unit(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", tmp_path)
    target = boot_unit.unit_path()
    target.write_text(render_unit(ctx))
    calls: list[list[str]] = []

    def fail_stop(
        argv: list[str],
        *,
        timeout: float = 120,
        host: Host,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "stop failed")

    monkeypatch.setattr(boot_unit, "privileged", fail_stop)
    with pytest.raises(RuntimeError, match="stop failed"):
        uninstall(host=_linux())
    assert target.exists()
    assert calls == [["systemctl", "disable", "--now", "ava-boot.service"]]


def test_install_enables_native_unit_without_recursively_starting(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, installed = _install_seams(monkeypatch, tmp_path, enabled=False)
    steps = install(context=ctx, host=_linux())
    assert installed["content"] == render_unit(ctx)
    assert ["systemctl", "enable", "ava-boot.service"] in recorded
    assert ["systemctl", "start", "ava-boot.service"] not in recorded
    assert not any("crontab" in arg for call in recorded for arg in call)
    assert "enabled ava-boot.service" in steps


def test_unchanged_enabled_unit_never_restarts_or_rewrites(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, _installed = _install_seams(monkeypatch, tmp_path, enabled=True)
    target = boot_unit.unit_path()
    target.parent.mkdir()
    target.write_text(render_unit(ctx))
    assert install(context=ctx, host=_linux()) == []
    assert recorded == []
