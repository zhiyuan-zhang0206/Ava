"""Linux native boot ownership, rendering and direct root adoption."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from base.host.system import boot_unit
from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S
from base.host.system.boot_unit import (
    START_TIMEOUT_S,
    BootUnitContext,
    install,
    render_unit,
    systemd_running,
    uninstall,
    unit_enabled,
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
    # bounded attempt -- the platform-agnostic policy of base/host/system/boot_policy.py.
    assert "Restart=on-failure" in unit
    assert f"RestartSec={BOOT_RETRY_INTERVAL_S}" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert f"TimeoutStartSec={START_TIMEOUT_S}" in unit
    assert "RuntimeMaxSec" not in unit
    assert "Type=forking" in unit and "GuessMainPID=no" in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert f"PIDFile={boot_unit.root_pid_path(ctx.home)}" in unit


def test_render_unit_binds_home_and_checkout(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    # Boot ordering + identity: the unit runs as the cluster's user, in the
    # checkout, with the checkout venv on PATH.
    assert "After=network-online.target\n" in unit
    assert "WantedBy=multi-user.target" in unit
    assert f"User={ctx.user}" in unit and f"Group={ctx.group}" in unit
    assert f"WorkingDirectory={ctx.repo}" in unit
    # Verified on systemd 255: quotes would become part of the path (fatal),
    # while the literal rest-of-line form keeps spaces working.
    assert f'WorkingDirectory="{ctx.repo}"' not in unit
    assert f'Environment="AVA_HOME={ctx.home}"' in unit
    assert "AVA_HOST_STATE_DIR" not in unit
    assert f'Environment="HOME={ctx.home_dir}"' in unit
    assert f'Environment="PATH={ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in unit
    # `:` disables $-expansion in the Exec line; the script path is quoted.
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


# --- detection ---------------------------------------------------------------


def test_systemd_running_requires_linux_systemctl_and_the_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(boot_unit, "is_linux", lambda: False)
    assert systemd_running() is False
    monkeypatch.setattr(boot_unit, "is_linux", lambda: True)
    monkeypatch.setattr(boot_unit.shutil, "which", _no_binary)
    assert systemd_running() is False
    monkeypatch.setattr(boot_unit.shutil, "which", _systemctl_binary)

    # /run/systemd/system is the booted-with-systemd marker; pin the path probe
    # rather than the host so the test reads the same on macOS and in CI.
    def fake_path(_p: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(is_dir=lambda: True)

    monkeypatch.setattr(boot_unit, "Path", fake_path)
    assert systemd_running() is True


def test_unit_enabled_reads_the_one_systemctl_seam(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "enabled\n", "")

    monkeypatch.setattr(boot_unit, "systemd_running", lambda: True)
    monkeypatch.setattr(boot_unit, "_systemctl", fake_systemctl)
    assert unit_enabled() is True
    assert calls == [("is-enabled", "ava-boot.service")]

    # Anything but the literal word "enabled" is not ownership.
    def disabled(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "disabled\n", "")

    monkeypatch.setattr(boot_unit, "_systemctl", disabled)
    assert unit_enabled() is False
    # A host without systemd can never have an enabled unit.
    monkeypatch.setattr(boot_unit, "systemd_running", lambda: False)
    assert unit_enabled() is False


# --- install / uninstall / status --------------------------------------------


def _install_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool
) -> tuple[list[list[str]], dict[str, str]]:
    """Patch the module's seams so install never touches the real host."""
    monkeypatch.setattr(boot_unit, "systemd_running", lambda: True)
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", tmp_path / "etc-systemd")

    def unit_enabled() -> bool:
        return enabled

    monkeypatch.setattr(boot_unit, "unit_enabled", unit_enabled)
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

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)
    return recorded, installed


def test_install_refuses_without_systemd(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot_unit, "systemd_running", lambda: False)
    with pytest.raises(RuntimeError, match="systemd"):
        install(context=ctx)


def test_uninstall_removes_only_the_boot_units_paths(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(boot_unit, "is_linux", lambda: True)
    recorded: list[list[str]] = []

    def fake_privileged(
        argv: list[str], *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)

    unit = units / "ava-boot.service"
    unit.write_text(render_unit(ctx))
    foreign = units / "unrelated.service"
    foreign.write_text("y")
    # The egress template unit of the gateway host is not ours, and is named in no command.
    template = units / "ava-gateway-egress@.service"
    template.write_text("z")
    steps = uninstall()

    assert ["systemctl", "disable", "--now", "ava-boot.service"] in recorded
    assert ["rm", "-f", str(unit)] in recorded
    assert ["systemctl", "daemon-reload"] in recorded
    # Only the exact path; a sibling unit is in no command.
    assert str(foreign) not in str(recorded) and "ava-gateway-egress" not in str(recorded)
    assert foreign.exists() and template.exists()
    assert steps == [f"removed {unit}"]

    # Nothing left to remove -> a no-op (the mocked `rm -f` never ran for real).
    unit.unlink()
    before = list(recorded)
    assert uninstall() == []
    assert recorded == before
    # A non-Linux host never touches /etc, whatever is on disk.
    unit.write_text(render_unit(ctx))
    monkeypatch.setattr(boot_unit, "is_linux", lambda: False)
    assert uninstall() == []
    assert unit.exists()


def _manager_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, active_state: str
) -> tuple[Path, list[list[str]]]:
    """A systemd host whose manager reports `active_state` for the boot unit."""
    units = tmp_path / "etc-systemd"
    units.mkdir(exist_ok=True)
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", units)
    monkeypatch.setattr(boot_unit, "is_linux", lambda: True)
    monkeypatch.setattr(boot_unit, "systemd_running", lambda: True)
    recorded: list[list[str]] = []

    def fake_privileged(
        argv: list[str], *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def show(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        del timeout
        assert args[:3] == ("show", "--property=ActiveState", "--value"), args
        return subprocess.CompletedProcess(["systemctl", *args], 0, f"{active_state}\n", "")

    monkeypatch.setattr(boot_unit, "privileged", fake_privileged)
    monkeypatch.setattr(boot_unit, "_systemctl", show)
    return units, recorded


def test_uninstall_leaves_no_failed_record_behind(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot unit whose stop failed stays listed as `not-found failed` after its
    file is removed and the manager reloaded (A/B/A run 6's destroy)."""
    units, recorded = _manager_reports(monkeypatch, tmp_path, "failed")
    name = "ava-boot.service"
    (units / name).write_text(render_unit(ctx))

    steps = uninstall()

    reset = ["systemctl", "reset-failed", name]
    assert recorded.index(["systemctl", "daemon-reload"]) < recorded.index(reset)
    assert steps[-1] == f"cleared the failed record of {name}"
    # A residue a previous destroy left (no file any more) is cleared too.
    (units / name).unlink()  # what the mocked `rm -f` would have done
    recorded.clear()
    assert uninstall() == [f"cleared the failed record of {name}"]
    assert recorded == [reset]


def test_uninstall_resets_nothing_it_did_not_leave_failed(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _units, recorded = _manager_reports(monkeypatch, tmp_path, "inactive")
    assert uninstall() == []
    assert recorded == []


def test_privileged_translates_a_missing_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host without sudo must fail actionably (RuntimeError with guidance),
    not surface a raw FileNotFoundError traceback through the CLI wrappers."""

    def run_missing(_cmd: list[str], **_kw: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "sudo")

    monkeypatch.setattr(boot_unit.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(boot_unit.subprocess, "run", run_missing)

    with pytest.raises(RuntimeError, match="passwordless sudo"):
        boot_unit.privileged(["true"])


def test_the_boot_unit_carries_no_home() -> None:
    """One unit per host: its name and path name no home, so the gate of
    `owns_os_jobs` is what keeps a scratch home off it."""
    assert boot_unit.UNIT_NAME == "ava-boot.service"
    assert boot_unit.unit_path() == boot_unit.SYSTEM_UNIT_DIR / "ava-boot.service"


def test_interactive_start_never_publishes(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.native_process.ownership import OwnedProcess

    monkeypatch.setattr(boot_unit, "is_linux", lambda: True)

    def interactive(_pid: int) -> str:
        return "/user.slice/interactive.scope"

    def no_manager() -> dict[str, str]:
        pytest.fail("interactive")

    monkeypatch.setattr(boot_unit, "process_cgroup", interactive)
    monkeypatch.setattr(boot_unit, "manager_properties", no_manager)
    boot_unit.publish_root_ready(ctx.home, OwnedProcess(123, 1.0, 1))


@pytest.mark.parametrize("fault", ["dead", "foreign", "manager", "reused", "none"])
def test_root_publication_binds_native_custody(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from base.native_process.ownership import OwnedProcess

    owner = OwnedProcess(123, 1.0, 1)
    expected = "/system.slice/ava-boot.service"

    def in_unit() -> bool:
        return True

    def cgroup(_pid: int) -> str:
        return "/foreign" if fault == "foreign" else expected

    alive = iter([False] if fault == "dead" else [True, fault != "reused"])

    def live(_self: OwnedProcess) -> bool:
        return next(alive)

    def properties() -> dict[str, str]:
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
        path.write_text("999999\n")  # A stale hint is replaced, never adopted as authority.
        boot_unit.publish_root_ready(ctx.home, owner)
        assert path.read_text() == "123\n"
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(RuntimeError, match=r"custody|birth"):
            boot_unit.publish_root_ready(ctx.home, owner)
        assert not path.exists()


def test_uninstall_failed_stop_preserves_unit(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot_unit, "is_linux", lambda: True)
    monkeypatch.setattr(boot_unit, "SYSTEM_UNIT_DIR", tmp_path)
    target = boot_unit.unit_path()
    target.write_text(render_unit(ctx))
    calls: list[list[str]] = []

    def fail_stop(argv: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "stop failed")

    monkeypatch.setattr(boot_unit, "privileged", fail_stop)
    with pytest.raises(RuntimeError, match="stop failed"):
        uninstall()
    assert target.exists()
    assert calls == [["systemctl", "disable", "--now", "ava-boot.service"]]


def test_install_enables_native_unit_without_recursively_starting(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, installed = _install_seams(monkeypatch, tmp_path, enabled=False)
    steps = install(context=ctx)
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
    assert install(context=ctx) == []
    assert recorded == []
