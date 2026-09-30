"""Linux systemd owns the lifetime of this home's application root.

The unit invokes ordinary ``ava start`` directly. Its successful readiness tail
publishes the birth-validated root PID. Type=forking adopts it only after the
ordinary start command exits successfully and the root becomes a manager child.
There is no resident boot wrapper and no separate service readiness policy.

KillMode=process deliberately leaves the independent native data plane alone.
The root closes its own captured application tree on TERM; failed closure keeps
custody and blocks replacement. Systemd cannot establish orphan closure after
an abrupt root death. It must never erase that retained custody.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from base.host.atomic_io import write_text_atomic
from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S
from base.native_process.os_platform import IS_LINUX
from base.native_process.ownership import OwnedProcess

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")

# Bound startup only; a healthy resident root has no runtime deadline.
START_TIMEOUT_S = 900


@dataclass(frozen=True)
class BootUnitContext:
    """Everything the unit renders from (explicit for tests)."""

    home: Path  # $AVA_HOME
    repo: Path  # the checkout that owns this home (holds .venv/)
    user: str
    group: str
    home_dir: Path  # the user's $HOME


# --- paths and names --------------------------------------------------------


UNIT_NAME = "ava-boot.service"
"""The one boot unit of the host: the host runs one cluster."""


def unit_path() -> Path:
    return SYSTEM_UNIT_DIR / UNIT_NAME


def root_pid_path(home: Path) -> Path:
    """Manager adoption hint; native root custody remains the ownership authority."""
    return home / "run" / "ava-root" / "systemd.pid"


def _default_context() -> BootUnitContext:
    import grp
    import pwd

    from base.paths import ava_home, repo_root

    entry = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(entry.pw_gid).gr_name
    return BootUnitContext(
        home=ava_home(),
        repo=repo_root(),
        user=entry.pw_name,
        group=group,
        home_dir=Path(entry.pw_dir),
    )


# --- detection --------------------------------------------------------------


def systemd_running() -> bool:
    """True when this host runs systemd as its service manager."""
    return (
        IS_LINUX and shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()
    )


def _systemctl(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """One bounded seam for read-only systemctl calls (tests patch this)."""
    return subprocess.run(  # noqa: S603 — fixed systemctl verbs, literal args
        ["systemctl", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def unit_enabled() -> bool:
    """Whether the boot unit is installed and enabled."""
    if not systemd_running():
        return False
    return _systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled"


# --- rendering --------------------------------------------------------------


def _clean(value: str, what: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{what} must not contain control characters")
    return value


def _quote(value: str, what: str) -> str:
    """One quoted systemd argument; never expands specifiers or shell."""
    escaped = (
        _clean(value, what)
        .replace(chr(92), chr(92) * 2)
        .replace('"', chr(92) + '"')
        .replace("%", "%%")
    )
    return '"' + escaped + '"'


def _path_value(value: str, what: str) -> str:
    """A path directive takes the complete literal value, not argument quotes."""
    return _clean(value, what).replace("%", "%%")


def render_unit(ctx: BootUnitContext) -> str:
    """Start normally, then let systemd own the verified application root."""
    # The ordinary source checkout invocation of the root boot owner.
    argv = (str(ctx.repo / ".venv/bin/python"), "-m", "cli.main", "start")
    environment = (
        ("HOME", str(ctx.home_dir)),
        ("AVA_HOME", str(ctx.home)),
        ("PATH", f"{ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"),
    )
    env_lines = "\n".join(
        f"Environment={_quote(f'{key}={value}', 'environment value')}" for key, value in environment
    )
    # Quote every argument. ':' also disables systemd's $ expansion.
    command = " ".join(_quote(value, "start argument") for value in argv)
    # Only the generic network target: the host's private-network or proxy
    # services are the operator's (order after them with a drop-in), and a boot
    # start that beats them fails and retries under Restart=on-failure.
    return (
        "[Unit]\n"
        "Description=Ava application root\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=forking\n"
        "GuessMainPID=no\n"
        f"PIDFile={_path_value(str(root_pid_path(ctx.home)), 'root PID path')}\n"
        f"User={_path_value(ctx.user, 'user')}\n"
        f"Group={_path_value(ctx.group, 'group')}\n"
        f"{env_lines}\n"
        f"WorkingDirectory={_path_value(str(ctx.repo), 'runtime path')}\n"
        f"ExecStart=:{command}\n"
        "Restart=on-failure\n"
        f"RestartSec={BOOT_RETRY_INTERVAL_S}\n"
        f"TimeoutStartSec={START_TIMEOUT_S}\n"
        "# Only root receives TERM; it owns captured application-tree closure.\n"
        "# Native data-plane siblings survive application-root shutdown.\n"
        "KillMode=process\n"
        "SendSIGKILL=no\n"
        "TimeoutStopSec=90\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def process_cgroup(pid: int) -> str:
    """Read the native systemd/unified cgroup, without process-name inference."""
    rows = (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
    for row in rows:
        hierarchy, controllers, path = row.split(":", 2)
        if hierarchy == "0" or "name=systemd" in controllers.split(","):
            return path
    raise RuntimeError(f"PID {pid} has no observable systemd cgroup")


def in_boot_unit() -> bool:
    """Interactive start has no systemd readiness tail, even on Linux."""
    if not IS_LINUX:
        return False
    return process_cgroup(os.getpid()) == f"/system.slice/{UNIT_NAME}"


def manager_properties() -> dict[str, str]:
    result = _systemctl("show", "--property=MainPID,ControlPID,ControlGroup,ActiveState", UNIT_NAME)
    if result.returncode:
        raise RuntimeError(f"cannot observe systemd root ownership: {result.stderr.strip()}")
    values = dict(row.split("=", 1) for row in result.stdout.splitlines() if "=" in row)
    if not {"MainPID", "ControlPID", "ControlGroup", "ActiveState"} <= values.keys():
        raise RuntimeError("systemd omitted root ownership properties")
    return values


def publish_root_ready(home: Path, root: OwnedProcess) -> None:
    """Publish the ready root; systemd adopts only after successful caller exit.

    Type=forking waits for this ordinary start process to exit before reading
    PIDFile. At that point root is systemd's child, so native stop waits for its
    closure. Interactive start writes nothing. This hint never replaces the
    birth-bound root custody checked by the caller.
    """
    if not in_boot_unit():
        return
    expected = f"/system.slice/{UNIT_NAME}"
    before = manager_properties()
    if (
        not root.live()
        or process_cgroup(root.pid) != expected
        or before["ControlGroup"] != expected
        or before["ControlPID"] != str(os.getpid())
        or before["MainPID"] != "0"
        or before["ActiveState"] != "activating"
    ):
        raise RuntimeError("root is outside this systemd startup's native custody")
    path = root_pid_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_text_atomic(path, f"{root.pid}\n", mode=0o600, sync_parent=True)
    if not root.live() or process_cgroup(root.pid) != expected:
        path.unlink()
        raise RuntimeError("root changed birth or native custody during PID publication")


# --- privileged steps -------------------------------------------------------


def privileged(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run one root step: direct when already root, else `sudo -n` (no prompt).

    A missing binary (`sudo` on a minimal host, or the tool itself) becomes the
    same actionable failure shape as a non-zero exit -- never a raw traceback.
    """
    command = argv if os.geteuid() == 0 else ["sudo", "-n", *argv]
    try:
        return subprocess.run(  # noqa: S603 — sudo -n + fixed verbs/paths from this module
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"{command[0]} not found on this host -- this step needs root: make "
            "passwordless sudo available, or run the step by hand"
        ) from e


def _privileged_failure(what: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    suffix = f": {detail}" if detail else ""
    return (
        f"{what} failed (rc={result.returncode}){suffix} -- this step needs root: "
        "make passwordless sudo available, or run the step by hand"
    )


def _privileged_or_raise(argv: list[str], what: str) -> None:
    result = privileged(argv)
    if result.returncode != 0:
        raise RuntimeError(_privileged_failure(what, result))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


# --- native registration / retirement -------------------------------------------


def install(*, context: BootUnitContext | None = None) -> list[str]:
    """Register and enable the sole Linux boot route without recursive startup."""
    if not systemd_running():
        raise RuntimeError(
            "the boot unit needs a Linux host running systemd as its service manager"
        )
    ctx = context if context is not None else _default_context()
    steps: list[str] = []

    destination = unit_path()
    unit_content = render_unit(ctx)
    if _read_text(destination) != unit_content:
        fd, name = tempfile.mkstemp(prefix="ava-boot-unit-", suffix=".service")
        os.close(fd)
        holder = Path(name)
        try:
            holder.write_text(unit_content)
            _privileged_or_raise(
                ["install", "-m", "0644", str(holder), str(destination)],
                f"write {destination}",
            )
        finally:
            holder.unlink(missing_ok=True)
        _privileged_or_raise(["systemctl", "daemon-reload"], "systemctl daemon-reload")
        steps.append(f"installed system unit {destination}")

    if not unit_enabled():
        _privileged_or_raise(["systemctl", "enable", UNIT_NAME], f"enable {UNIT_NAME}")
        steps.append(f"enabled {UNIT_NAME}")
    logger.info("boot unit ready for {}", ctx.home)
    return steps


def uninstall() -> list[str]:
    """Remove the host's boot unit after a successful native stop.

    Safe when nothing is installed, and safe to call on non-systemd hosts (a
    no-op): `ava cluster destroy` runs it unconditionally with the other OS
    jobs. Removes only the exact unit path of `UNIT_NAME`, then resets the failed
    record systemd keeps listing for a removed unit whose last stop failed.
    """
    if not IS_LINUX:
        return []
    steps: list[str] = []
    name = UNIT_NAME
    destination = unit_path()
    if destination.exists():
        _privileged_or_raise(["systemctl", "disable", "--now", name], f"stop {name}")
        _privileged_or_raise(["rm", "-f", str(destination)], f"remove {destination}")
        _privileged_or_raise(["systemctl", "daemon-reload"], "systemctl daemon-reload")
        steps.append(f"removed {destination}")
    if _failed_record(name):
        _privileged_or_raise(["systemctl", "reset-failed", name], f"reset-failed {name}")
        steps.append(f"cleared the failed record of {name}")
    return steps


def _failed_record(name: str) -> bool:
    """A failed unit outlives its removed file as a `not-found failed` record
    until reset; checked also when a previous removal left one behind."""
    if not systemd_running():
        return False
    result = _systemctl("show", "--property=ActiveState", "--value", name)
    if result.returncode:
        raise RuntimeError(f"cannot observe {name}: {result.stderr.strip()}")
    return result.stdout.strip() == "failed"
