"""The distro-level systemd boot unit (`shared.os_boot_unit`).

On a Linux host whose service manager is systemd, the boot path's retry lives
in systemd itself -- `Restart=on-failure`, no attempt cap, `RuntimeMaxSec`
bounding one attempt -- instead of an unsupervised child loop. These tests pin
the retry contract in the two rendered artifacts (the unit and the one-attempt
convergence script), the strictly-one-owner install semantics (enabling swaps
the crontab entry out in the same call; a staged install leaves it live), and
that uninstall / status touch only this home's paths.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from shared import os_boot_unit
from shared.boot_policy import BOOT_RETRY_INTERVAL_S
from shared.cluster import home_slug
from shared.os_boot_unit import (
    CONVERGE_RUNTIME_MAX_S,
    GENERATE_204_URL,
    BootUnitContext,
    boot_unit_owns_boot_path,
    install,
    render_script,
    render_unit,
    status,
    systemd_running,
    uninstall,
    unit_enabled,
    unit_name,
)


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
    assert f"RuntimeMaxSec={CONVERGE_RUNTIME_MAX_S}" in unit
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
    assert f'ExecStart=:"{os_boot_unit.script_path(ctx.home)}"' in unit


def test_render_unit_carries_the_proxy_wait_only_when_configured(ctx: BootUnitContext) -> None:
    assert "AVA_BOOT_PROXY_WAIT" not in render_unit(ctx)
    unit = render_unit(ctx, proxy_wait_url="http://127.0.0.1:7897")
    assert 'Environment="AVA_BOOT_PROXY_WAIT=http://127.0.0.1:7897"' in unit


def test_render_unit_refuses_a_metacharacter_proxy_url(ctx: BootUnitContext) -> None:
    with pytest.raises(ValueError, match="shell metacharacters"):
        render_unit(ctx, proxy_wait_url="http://127.0.0.1:7897/$(oops)")


def test_render_script_is_one_attempt_and_the_rc_is_the_contract(ctx: BootUnitContext) -> None:
    script = render_script(ctx)
    assert script.startswith("#!/bin/bash")
    # The retry is the unit's; the script states the rc contract and returns a
    # failure so the unit restarts it after RestartSec.
    assert "Restart=on-failure" in script
    assert 'if [ "$rc" != 0 ]' in script and 'exit "$rc"' in script
    assert "start --no-readiness-gate" in script
    # One attempt truncates the per-attempt log; the state file is the terse
    # operator surface `status` reads.
    assert '>"$log" 2>&1' in script
    assert 'log="$AVA_HOME/logs/boot.log"' in script
    assert 'state="$AVA_HOME/logs/boot-converge.state"' in script
    # A fresh home must not turn the missing logs/ dir into a failed redirect
    # (an attempt that never ran ava start but asks the unit to retry forever).
    assert 'mkdir -p "$AVA_HOME/logs"' in script
    # Proxy readiness is a real round trip through the unit's URL.
    assert "AVA_BOOT_PROXY_WAIT" in script and GENERATE_204_URL in script
    # Readiness resolves the gateway URL like every client does (env / .env
    # AVA_GATEWAY_URL > the legacy file), never a bare file read -- the wsl
    # gateway carries its URL in .env, so the file read alone recorded nothing.
    assert "from shared.machines import gateway_url" in script
    assert 'cat "$AVA_HOME/gateway_url"' in script


def test_render_script_refuses_metacharacter_paths(tmp_path: Path) -> None:
    bad = BootUnitContext(
        home=tmp_path / 'ho"me',
        repo=tmp_path / "repo",
        user="u",
        group="g",
        home_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="metacharacters"):
        render_script(bad)


# --- detection ---------------------------------------------------------------


def test_systemd_running_requires_linux_systemctl_and_the_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", False)
    assert systemd_running() is False
    monkeypatch.setattr(os_boot_unit, "IS_LINUX", True)
    monkeypatch.setattr(os_boot_unit.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    assert systemd_running() is False
    monkeypatch.setattr(os_boot_unit.shutil, "which", lambda _name: "/usr/bin/systemctl")  # pyright: ignore[reportUnknownArgumentType]

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


def test_install_enable_writes_both_artifacts_and_swaps_the_boot_path(
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

    steps = install(context=ctx, proxy_wait_url="http://127.0.0.1:7897")

    script = os_boot_unit.script_path(ctx.home)
    assert script.read_text() == render_script(ctx)
    assert script.stat().st_mode & 0o777 == 0o755
    destination = os_boot_unit.unit_path(ctx.home)
    assert installed["destination"] == str(destination)
    assert installed["content"] == render_unit(ctx, proxy_wait_url="http://127.0.0.1:7897")
    argv = recorded
    assert ["systemctl", "daemon-reload"] in argv
    assert ["systemctl", "enable", unit_name(ctx.home)] in argv
    # Enabling swaps the live boot path in the same call: exactly one owner.
    assert removed == [home_slug(ctx.home)]
    assert "removed the crontab autostart entry" in steps
    assert f"wrote convergence script {script}" in steps
    assert f"enabled {unit_name(ctx.home)}" in steps


def test_install_enable_skips_the_crontab_step_without_a_crontab_binary(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no crontab binary there is no entry to remove: the enable flow must
    report no cron step and must not shell out to a missing binary at all."""
    _install_seams(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(os_boot_unit.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]

    def unregister_must_not_run(_slug: str) -> int:
        pytest.fail("no crontab binary: there is no entry to remove")

    monkeypatch.setattr("shared.os_autostart._unregister_linux", unregister_must_not_run)
    steps = install(context=ctx)
    assert "removed the crontab autostart entry" not in steps


def test_crontab_probes_degrade_without_a_crontab_binary(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
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
    assert f"wrote convergence script {os_boot_unit.script_path(ctx.home)}" in steps


def test_install_refuses_without_systemd(
    ctx: BootUnitContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)
    with pytest.raises(RuntimeError, match="systemd"):
        install(context=ctx)


def test_install_restores_a_drifted_executable_mode(
    ctx: BootUnitContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-identical but not executable is a dead ExecStart: the mode is
    repaired (and reported), not silently accepted."""
    _install_seams(monkeypatch, tmp_path, enabled=False)
    script = os_boot_unit.script_path(ctx.home)
    script.parent.mkdir(parents=True)
    script.write_text(render_script(ctx))
    script.chmod(0o644)

    steps = install(context=ctx, enable=False)

    assert script.stat().st_mode & 0o777 == 0o755
    assert f"fixed {script} mode to 0755" in steps


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
    script = os_boot_unit.script_path(ctx.home)
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")

    steps = uninstall(ctx.home)

    assert ["systemctl", "disable", "--now", unit_name(ctx.home)] in recorded
    assert ["rm", "-f", str(unit)] in recorded
    assert ["systemctl", "daemon-reload"] in recorded
    # Only this home's exact paths; a sibling unit is in no command.
    assert str(foreign) not in str(recorded)
    assert foreign.exists()
    assert not script.exists()
    assert steps == [f"removed {unit}", f"removed {script}"]

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
    monkeypatch.setattr(os_boot_unit.subprocess, "run", run_missing)  # pyright: ignore[reportUnknownArgumentType]

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
    assert rows["script"] == f"{os_boot_unit.script_path(ctx.home)} (missing)"
    assert rows["proxy wait"] == "disabled"
    assert rows["cron entry"] == "absent"
    assert rows["last convergence"] == "none"
    assert rows["systemd state"] == "not running (the boot unit needs systemd)"

    # The installed+enabled state is all visible without a single write.
    unit_file = os_boot_unit.unit_path(ctx.home)
    unit_file.write_text(render_unit(ctx, proxy_wait_url="http://127.0.0.1:7897"))
    script = os_boot_unit.script_path(ctx.home)
    script.parent.mkdir(parents=True)
    script.write_text(render_script(ctx))
    state = os_boot_unit.state_path(ctx.home)
    state.parent.mkdir(parents=True)
    state.write_text("state=ready\n")

    rows = dict(status(ctx.home))
    assert rows["unit file"] == f"{unit_file} (present)"
    assert rows["unit content"] == "matches rendered"
    assert rows["proxy wait"] == "http://127.0.0.1:7897"
    assert rows["script content"] == "matches rendered"
    assert rows["last convergence"] == "state=ready\n"


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

    # The own home still compares (and matches a unit this code rendered).
    (units / unit_name(ctx.home)).write_text(render_unit(ctx))
    assert dict(status(ctx.home))["unit content"] == "matches rendered"


def test_paths_and_names_are_home_scoped(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    assert unit_name(first) == f"ava-boot.{home_slug(first)}.service"
    assert unit_name(first) != unit_name(second)
    assert os_boot_unit.unit_path(first) == os_boot_unit.SYSTEM_UNIT_DIR / unit_name(first)
    assert os_boot_unit.script_path(first) == first / "bin" / "ava-boot-converge.sh"
    assert os_boot_unit.state_path(first) == first / "logs" / "boot-converge.state"
