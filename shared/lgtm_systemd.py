"""Home-scoped user systemd lifecycle for the three native LGTM backends.

Only exact units and executables belonging to the requested home are managed.
No system services, foreign homes, or existing container deployments are changed.
"""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from shared.cluster import home_slug
from shared.lgtm_local import BACKENDS, backend_urls, binary_path
from shared.platform import user_systemd_unit_dir


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    environment: dict[str, str]


def unit_name(home: Path, name: str) -> str:
    if name not in BACKENDS:
        raise ValueError(f"Unknown native LGTM backend: {name}")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", home_slug(home.resolve()))
    return f"com.ava.{name}.{slug}.service"


def unit_path(home: Path, name: str) -> Path:
    return user_systemd_unit_dir() / unit_name(home, name)


def _quote(value: str) -> str:
    """Quote one systemd argument; never expand specifiers or shell expressions."""
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("Native LGTM service arguments must not contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + escaped + '"'


def _path_value(value: str) -> str:
    """Path directives use the complete literal value, not argument quoting."""
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("Native LGTM service paths must not contain control characters")
    return value.replace("%", "%%")


def render_unit(home: Path, name: str, command: Command) -> str:
    """A user service with bounded stop and automatic recovery after a crash."""
    native = home.resolve() / "lgtm/native"
    environment = "\n".join(
        f"Environment={_quote(key + '=' + value)}"
        for key, value in sorted(command.environment.items())
    )
    return (
        f"[Unit]\nDescription=Ava native {name}\n\n[Service]\nType=simple\n"
        f"WorkingDirectory={_path_value(str(native))}\n"
        f"ExecStart=:{' '.join(_quote(arg) for arg in command.argv)}\n"
        f"{environment}\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=30\n"
        "KillMode=control-group\nUMask=0077\n"
        f"StandardOutput={_path_value('append:' + str(native / 'logs' / (name + '.log')))}\n"
        f"StandardError={_path_value('append:' + str(native / 'logs' / (name + '.log')))}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    if platform.system() != "Linux":
        raise RuntimeError("Native LGTM user systemd lifecycle requires Linux")
    result = subprocess.run(  # noqa: S603 — fixed user-manager command and owned unit identity
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=45
    )
    if result.returncode:
        raise RuntimeError(f"LGTM systemctl {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _state(home: Path, name: str) -> dict[str, str]:
    result = _systemctl(
        "show", unit_name(home, name), "--property=LoadState,ActiveState,MainPID,FragmentPath"
    )
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (
        state["LoadState"] != "not-found"
        and Path(state["FragmentPath"]).resolve() != unit_path(home, name).resolve()
    ):
        raise RuntimeError(f"Refusing a foreign native LGTM unit: {unit_name(home, name)}")
    return state


def running_pid(home: Path, name: str) -> int | None:
    """Require both the canonical unit and its exact native executable."""
    state = _state(home, name)
    if state["LoadState"] == "not-found" or state["ActiveState"] != "active":
        return None
    pid = int(state["MainPID"])
    if pid <= 0:
        return None
    try:
        executable = Path(
            str(Path(f"/proc/{pid}/exe").readlink()).removesuffix(" (deleted)")
        ).resolve()
    except FileNotFoundError:
        return None
    return pid if executable == binary_path(home, name) else None


def register(home: Path, commands: dict[str, Command]) -> bool:
    """Publish this home's unit definitions and reload the user manager on change."""
    changed = False
    for name in BACKENDS:
        path = unit_path(home, name)
        content = render_unit(home, name, commands[name])
        if path.exists() and path.read_text() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(content)
                stream.flush()
                temporary.chmod(0o644)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        changed = True
    if changed:
        _systemctl("daemon-reload")
    return changed


def verify_loki(home: Path) -> None:
    """Keep the binary's own config gate before every start or restart."""
    result = subprocess.run(  # noqa: S603 — verified home-scoped native binary and config
        [
            str(binary_path(home, "loki")),
            f"-config.file={home.resolve()}/lgtm/native/config/loki.yaml",
            "-verify-config",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Loki config verification failed: {result.stderr.strip()}")


def _answers(url: str) -> bool:
    try:
        # Direct listener probes must not go through a machine HTTP proxy.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start(home: Path) -> None:
    verify_loki(home)
    urls = backend_urls()
    for name in BACKENDS:
        if not unit_path(home, name).is_file():
            raise RuntimeError(f"Native LGTM {name} unit is missing; run ava lgtm on")
        pid = running_pid(home, name)
        if pid and _answers(urls[name]):
            continue
        _systemctl("enable", unit_name(home, name))
        _systemctl("restart" if pid else "start", unit_name(home, name))
        for _ in range(15):
            if running_pid(home, name) and _answers(urls[name]):
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"Native LGTM {name} did not become reachable within 30 seconds")


def restart_running(home: Path) -> None:
    """Apply changed configs to running services without starting stopped ones."""
    verify_loki(home)
    for name in BACKENDS:
        if running_pid(home, name):
            _systemctl("restart", unit_name(home, name))


def stop(home: Path) -> None:
    """Disable and remove only the three exact units belonging to this home."""
    # Stop the UI/evaluator before the backends it queries.
    for name in reversed(BACKENDS):
        state = _state(home, name)
        path = unit_path(home, name)
        if state["LoadState"] != "not-found":
            _systemctl("disable", "--now", unit_name(home, name))
            if running_pid(home, name):
                raise RuntimeError(f"Native LGTM {name} is still running after stop")
        path.unlink(missing_ok=True)
    _systemctl("daemon-reload")


def main() -> None:
    from shared.paths import ava_home

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    action = parser.parse_args().action
    {"start": start, "stop": stop}[action](ava_home())


if __name__ == "__main__":
    main()
