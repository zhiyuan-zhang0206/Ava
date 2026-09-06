"""The Linux gate's per-home user-systemd registration and teardown.

The manager owns liveness; the unit bytes own desired code/environment. Full stop
stops the unit without disabling its next-login registration; destroy removes it.
Neither operation is part of an ordinary update's service-session teardown.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from shared.cluster import home_slug
from shared.platform import file_lock


def unit_name(home: Path) -> str:
    """Escape a home slug using systemd's unit-name byte escape convention."""
    slug = home_slug(home)
    safe = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.-"
    escaped = "".join(chr(b) if b in safe else f"\\x{b:02x}" for b in slug.encode())
    return f"com.ava.gate.{escaped}.service"


def unit_path(home: Path) -> Path:
    """The current OS user's unit directory, independent of the AVA home."""
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not config.is_absolute():
        raise RuntimeError("XDG_CONFIG_HOME must be absolute for the gate user unit")
    return config / "systemd" / "user" / unit_name(home)


def _literal(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Gate unit paths and arguments cannot contain control characters")
    return value.replace("%", "%%")


def _argument(value: str) -> str:
    return '"' + _literal(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_content(home: Path, repo: Path, python: str, content_hash: str) -> str:
    """Render an explicit, clean child environment; no manager AVA vars leak in.

    The colon disables systemd's dollar expansion, while percent specifiers are
    escaped separately. GNU env changes directory before exec so quoted paths
    retain even trailing spaces; systemd's WorkingDirectory parser is different.
    """
    argv = [
        "/usr/bin/env",
        "--ignore-environment",
        f"--chdir={repo}",
        f"HOME={Path.home()}",
        f"PATH={Path(python).parent}:/usr/local/bin:/usr/bin:/bin",
        f"AVA_HOME={home}",
        f"AVA_GATE_CONTENT_HASH={content_hash}",
        python,
        "-m",
        "services.gate.daemon",
    ]
    command = " ".join(_argument(arg) for arg in argv)
    log = _literal(str(home / "logs" / "gate.log"))
    return f"""[Unit]
Description=Ava fleet UI gate
StartLimitIntervalSec=0

[Service]
Type=exec
ExecStart=:{command}
Restart=always
RestartSec=2
TimeoutStopSec=15
KillMode=control-group
StandardOutput=append:{log}
StandardError=inherit

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """One bounded seam, including cron callers without a login-shell bus env."""
    runtime = f"/run/user/{os.getuid()}"
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def _checked(*args: str) -> subprocess.CompletedProcess[str]:
    result = _systemctl(*args)
    if result.returncode:
        raise RuntimeError(f"Gate systemctl {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _state(home: Path) -> dict[str, str]:
    result = _systemctl("show", unit_name(home), "--property=LoadState,ActiveState,UnitFileState")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and fields.get("LoadState") != "not-found":
        raise RuntimeError(f"Cannot inspect gate user unit: {result.stderr.strip()}")
    return fields


def supervised(home: Path) -> bool:
    """A restart-in-progress is supervised; an unavailable manager is not."""
    try:
        state = _state(home)
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        return False
    return state.get("LoadState") == "loaded" and state.get("ActiveState") in {
        "active",
        "activating",
        "reloading",
    }


def _require_manager() -> None:
    try:
        _checked("show", "--property=Version", "--value")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        raise RuntimeError(
            "Linux gate requires a running systemd user manager. Enable systemd and "
            "user lingering for unattended boot, then run ava converge."
        ) from exc


def _write_unit(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, encoding="utf-8", delete=False
    ) as f:
        temporary = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _stop_legacy(home: Path, repo: Path) -> None:
    """Do not replace a live detached listener until its ownership is proved."""
    from cli.commands._converge_gate import gate_pid_is_ours
    from cli.commands._pgbouncer import _terminate_verified
    from shared.proc import process_alive

    pidfile = home / "run" / "gate.pid"
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink()
        return
    if process_alive(pid):
        if not gate_pid_is_ours(pid, repo):
            raise RuntimeError(
                f"Cannot confirm ownership of legacy gate PID {pid}; not signalling it"
            )
        if not _terminate_verified(pid, label="legacy gate daemon"):
            raise RuntimeError(f"Legacy gate PID {pid} survived termination")
    pidfile.unlink(missing_ok=True)


def ensure(home: Path, repo: Path, python: str, content_hash: str) -> None:
    """Idempotently register/recover a unit; update code only after stopping it."""
    from shared.os_cron import os_jobs_enabled, skip_os_job

    if not os_jobs_enabled():
        skip_os_job("gate systemd user unit")
        return
    desired = unit_content(home, repo, python, content_hash)
    _require_manager()  # No files or old processes touched when the manager is absent.
    path = unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock"), timeout_s=35):
        changed = not path.exists() or path.read_text(encoding="utf-8") != desired
        state = _state(home)
        if (
            not changed
            and state.get("ActiveState") == "active"
            and state.get("UnitFileState") == "enabled"
        ):
            return
        # Stop before replacing bytes. A failed stop must not make a later
        # converge mistake an old live process for the new desired generation.
        if changed and state.get("LoadState") != "not-found":
            _checked("stop", unit_name(home))
        _stop_legacy(home, repo)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        if changed:
            _write_unit(path, desired)
        _checked("daemon-reload")
        _checked("enable", unit_name(home))
        _checked("start", unit_name(home))
        if not supervised(home):
            raise RuntimeError(f"Gate unit {unit_name(home)} did not become supervised")


def stop(home: Path, *, remove: bool = False) -> bool:
    """Wait for this home's unit to stop; destroy also disables and removes it.

    Failure retains the registration and raises so teardown cannot claim success.
    An ordinary stop keeps next-login registration, matching launchd bootout.
    """
    path = unit_path(home)
    if not path.exists():
        # A deleted fragment does not unload a live unit. Stop by its home-bound
        # name before the orphan reaper can kill a process systemd would revive.
        if _state(home).get("LoadState") == "not-found":
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock"), timeout_s=35):
        _checked("stop", unit_name(home))
        state = _state(home)
        if state.get("ActiveState") not in {"inactive", "failed"}:
            raise RuntimeError(f"Gate unit {unit_name(home)} is still running after stop")
        if remove:
            _checked("disable", unit_name(home))
            path.unlink(missing_ok=True)
            _checked("daemon-reload")
    return True
