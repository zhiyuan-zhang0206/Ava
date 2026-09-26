"""Linux native boot ownership, rendering and direct root adoption."""

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
    install,
    render_unit,
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
        registry=tmp_path / "private-registry.json",
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
    assert "Type=forking" in unit and "GuessMainPID=no" in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert f"PIDFile={os_boot_unit.root_pid_path(ctx.home)}" in unit


def test_render_unit_binds_home_checkout_and_registry(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
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
    assert f'Environment="AVA_CLUSTER_REGISTRY={ctx.registry}"' in unit
    assert f'Environment="HOME={ctx.home_dir}"' in unit
    assert f'Environment="PATH={ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in unit
    # `:` disables $-expansion in the Exec line; the script path is quoted.
    assert f'ExecStart=:"{ctx.repo}/.venv/bin/python" "-m" "cli.main" "start"' in unit


def test_render_unit_quotes_without_shell_expansion(ctx: BootUnitContext) -> None:
    odd = BootUnitContext(
        ctx.home, Path('/repo "odd" % $x'), ctx.user, ctx.group, ctx.home_dir, ctx.registry
    )
    unit = render_unit(odd)
    assert 'ExecStart=:"/repo \\"odd\\" %% $x/.venv/bin/python" "-m" "cli.main" "start"' in unit
    assert "AVA_BOOT_PROXY_WAIT" not in unit


def test_render_unit_refuses_control_characters(ctx: BootUnitContext) -> None:
    bad = BootUnitContext(
        ctx.home, Path("/repo\nExecStart=/bad"), ctx.user, ctx.group, ctx.home_dir, ctx.registry
    )
    with pytest.raises(ValueError, match="control characters"):
        render_unit(bad)


def test_release_stage_uses_same_native_root_owner(ctx: BootUnitContext) -> None:
    action = os_boot_unit.BootStartAction(
        (
            "/image/venv/bin/python",
            "-I",
            "-m",
            "cli.release_transition.stage",
            "--operation",
            "/private/$x%/operation.json",
        ),
        Path("/image/site"),
        os_boot_unit.source_start_action(ctx).environment,
        restart_on_failure=False,
    )
    unit = render_unit(ctx, action=action)
    assert '"cli.release_transition.stage" "--operation" "/private/$x%%/operation.json"' in unit
    assert "WorkingDirectory=/image/site" in unit
    assert "Type=forking" in unit
    assert f"PIDFile={os_boot_unit.root_pid_path(ctx.home)}" in unit
    assert "Restart=no\n" in unit
    assert "KillMode=process\n" in unit
    assert "cli.main" not in unit


@pytest.mark.parametrize("key", ["HOME", "AVA_HOME", "AVA_CLUSTER_REGISTRY"])
def test_boot_action_cannot_change_context_identity(ctx: BootUnitContext, key: str) -> None:
    source = os_boot_unit.source_start_action(ctx)
    action = os_boot_unit.BootStartAction(
        source.argv,
        source.cwd,
        tuple((k, "/other" if k == key else v) for k, v in source.environment),
    )
    with pytest.raises(ValueError, match=key):
        render_unit(ctx, action=action)


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


def test_paths_and_names_are_home_scoped(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert unit_name(first) == f"ava-boot.{home_slug(first)}.service"
    assert unit_name(first) != unit_name(second)
    assert os_boot_unit.unit_path(first) == os_boot_unit.SYSTEM_UNIT_DIR / unit_name(first)


def test_interactive_start_never_publishes(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.native_process.ownership import OwnedProcess

    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)

    def interactive(_pid: int) -> str:
        return "/user.slice/interactive.scope"

    def no_manager(_home: Path) -> dict[str, str]:
        pytest.fail("interactive")

    monkeypatch.setattr(os_boot_unit, "_process_cgroup", interactive)
    monkeypatch.setattr(os_boot_unit, "_manager_properties", no_manager)
    os_boot_unit.publish_root_ready(ctx.home, OwnedProcess(123, 1.0, 1))


@pytest.mark.parametrize("fault", ["dead", "foreign", "manager", "reused", "none"])
def test_root_publication_binds_native_custody(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from shared.native_process.ownership import OwnedProcess

    owner = OwnedProcess(123, 1.0, 1)
    expected = f"/system.slice/{unit_name(ctx.home)}"

    def in_unit(_home: Path) -> bool:
        return True

    def cgroup(_pid: int) -> str:
        return "/foreign" if fault == "foreign" else expected

    alive = iter([False] if fault == "dead" else [True, fault != "reused"])

    def live(_self: OwnedProcess) -> bool:
        return next(alive)

    def properties(_home: Path) -> dict[str, str]:
        return {
            "MainPID": "0",
            "ControlPID": "999" if fault == "manager" else str(os_boot_unit.os.getpid()),
            "ControlGroup": expected,
            "ActiveState": "activating",
        }

    monkeypatch.setattr(os_boot_unit, "in_boot_unit", in_unit)
    monkeypatch.setattr(os_boot_unit, "_process_cgroup", cgroup)
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(os_boot_unit, "_manager_properties", properties)
    path = os_boot_unit.root_pid_path(ctx.home)
    if fault == "none":
        path.parent.mkdir(parents=True)
        path.write_text("999999\n")  # A stale hint is replaced, never adopted as authority.
        os_boot_unit.publish_root_ready(ctx.home, owner)
        assert path.read_text() == "123\n"
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(RuntimeError, match=r"custody|birth"):
            os_boot_unit.publish_root_ready(ctx.home, owner)
        assert not path.exists()


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


def test_install_enables_native_unit_without_recursively_starting(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, installed = _install_seams(monkeypatch, tmp_path, enabled=False)
    steps = install(context=ctx)
    assert installed["content"] == render_unit(ctx)
    assert ["systemctl", "enable", unit_name(ctx.home)] in recorded
    assert ["systemctl", "start", unit_name(ctx.home)] not in recorded
    assert not any("crontab" in arg for call in recorded for arg in call)
    assert f"enabled {unit_name(ctx.home)}" in steps


def test_unchanged_enabled_unit_never_restarts_or_rewrites(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, _installed = _install_seams(monkeypatch, tmp_path, enabled=True)
    target = os_boot_unit.unit_path(ctx.home)
    target.parent.mkdir()
    target.write_text(render_unit(ctx))
    assert install(context=ctx) == []
    assert recorded == []


def test_default_context_carries_the_resolved_registry(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit, "registry_path", lambda: ctx.registry)
    assert os_boot_unit._default_context().registry == ctx.registry


@pytest.mark.parametrize("verb", ["install", "uninstall", "status"])
def test_no_second_linux_boot_management_cli(verb: str) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["cluster", "boot-unit", verb])
    assert error.value.code == 2
