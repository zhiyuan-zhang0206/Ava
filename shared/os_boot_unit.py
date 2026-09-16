"""The distro-level systemd boot unit for a Linux cluster ("boot unit").

`ava start`'s boot path on Linux has been a `@reboot` crontab entry
(`shared.os_autostart`): the scheduler fires it exactly once, so the retry loop
lives inside a stray child process (`cli.boot_retry`, `ava boot`) that nothing
supervises. On a host with systemd, this module registers the same boot job as
a **system** unit instead -- `ava-boot.<home-slug>.service` -- which runs a
small convergence script at boot while systemd itself does the retrying:
`Restart=on-failure`, `RestartSec=BOOT_RETRY_INTERVAL_S`, and
`StartLimitIntervalSec=0` (no attempt cap), the same policy
`shared.boot_policy.py` states for every platform, with `RuntimeMaxSec` killing
a wedged attempt so a hang cannot block retries.

Why a system unit and not a user unit (`systemctl --user`): the boot path must
not depend on a login session or on linger bookkeeping, and its enable/state
must be visible in `systemctl` and journald without a session. The job still
runs AS the cluster's user (`User=`), so every file it touches keeps its
ownership.

Once the unit is installed AND enabled, `shared.os_autostart._register_linux`
leaves the crontab entry out -- exactly one owner for the boot path. An
installed-but-not-yet-enabled unit is a staged install: the crontab entry
stays live until the unit is enabled, and is removed as soon as it is.

Privileges: `/etc/systemd/system` and the mutating `systemctl` verbs need root,
so they go through `_privileged()` (`sudo -n` -- never a password prompt -- or
direct when already root) and fail actionably when neither is available; the
same no-prompt stance as `shared.macos_firewall`. The convergence script under
`$AVA_HOME/bin` is written as the invoking user.

Operator surfaces: `ava cluster boot-unit install|uninstall|status` (wrappers
in `cli/commands/_cluster_boot_unit.py`), and `ava cluster destroy` removes the
unit with the other OS jobs.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from shared.boot_policy import BOOT_RETRY_INTERVAL_S
from shared.cluster import home_slug
from shared.paths import ava_home, repo_root
from shared.platform import IS_LINUX

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")

# Kill a wedged convergence attempt and let the unit's restart policy run the
# next one (see the module docstring). Well beyond a cold start yet bounded.
CONVERGE_RUNTIME_MAX_S = 900

# The proxy readiness probe is a REAL round trip (a liveness check that does
# not touch the resource it claims to check is theater).
GENERATE_204_URL = "http://www.gstatic.com/generate_204"

# Characters that would break the convergence script's double-quoted literals
# (paths are embedded; refuse rather than silently render a broken script).
_UNSAFE_IN_SCRIPT = ('"', "$", "`", chr(92))


@dataclass(frozen=True)
class BootUnitContext:
    """Everything the unit and the script render from (explicit for tests)."""

    home: Path  # $AVA_HOME
    repo: Path  # the checkout that owns this home (holds .venv/)
    user: str
    group: str
    home_dir: Path  # the user's $HOME


# --- paths and names --------------------------------------------------------


def unit_name(home: Path) -> str:
    """`ava-boot.<home-slug>.service` -- the com.ava.<kind>.<slug> shape, at
    system scope."""
    return f"ava-boot.{home_slug(home)}.service"


def unit_path(home: Path) -> Path:
    return SYSTEM_UNIT_DIR / unit_name(home)


def script_path(home: Path) -> Path:
    return home / "bin" / "ava-boot-converge.sh"


def state_path(home: Path) -> Path:
    """Where the convergence script records its last outcome (journal holds the
    full log; this is the terse operator surface `status` reads)."""
    return home / "logs" / "boot-converge.state"


def _default_context() -> BootUnitContext:
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


def unit_enabled(home: Path) -> bool:
    """Whether this home's boot unit is installed and enabled."""
    if not systemd_running():
        return False
    return _systemctl("is-enabled", unit_name(home)).stdout.strip() == "enabled"


def boot_unit_owns_boot_path(home: Path | None = None) -> bool:
    """Whether the systemd unit -- not the crontab entry -- owns this home's
    boot path.

    Installed AND enabled. A staged install (files written, unit not yet
    enabled) returns False on purpose: the crontab entry is still the live
    path until the switch, so the two never race at boot.
    """
    return unit_enabled(home if home is not None else ava_home())


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


def _validate_proxy_url(url: str) -> str:
    _clean(url, "proxy wait URL")
    if not re.match(r"^https?://[^\s\"'\x60$]+$", url):
        raise ValueError(
            f"proxy wait URL must be an http(s) URL without shell metacharacters: {url!r}"
        )
    return url


def render_unit(ctx: BootUnitContext, *, proxy_wait_url: str = "") -> str:
    """The system unit: systemd is the retry supervisor (module docstring)."""
    environment = [
        ("HOME", str(ctx.home_dir)),
        ("AVA_HOME", str(ctx.home)),
        ("PATH", f"{ctx.repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"),
    ]
    if proxy_wait_url:
        environment.append(("AVA_BOOT_PROXY_WAIT", _validate_proxy_url(proxy_wait_url)))
    env_lines = "\n".join(
        f"Environment={_quote(f'{key}={value}', 'environment value')}" for key, value in environment
    )
    return (
        "[Unit]\n"
        f"Description=Ava cluster boot convergence ({home_slug(ctx.home)})\n"
        "# Ordering is a no-op for units a host does not have; the proxy's\n"
        "# CONTENT readiness is the convergence script's to wait on.\n"
        "After=network-online.target tailscaled.service mihomo.service\n"
        "Wants=network-online.target\n"
        "# No attempt cap, per shared/boot_policy.py.\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={_path_value(ctx.user, 'user')}\n"
        f"Group={_path_value(ctx.group, 'group')}\n"
        f"{env_lines}\n"
        # Not quoted on purpose: WorkingDirectory= takes the rest of the line
        # literally (spaces included), and quotes would become part of the
        # path -- verified on systemd 255 (quoted = fatal, unquoted = clean).
        f"WorkingDirectory={_path_value(str(ctx.repo), 'checkout path')}\n"
        # `:` = no $-expansion in the command line (needs systemd >= 245;
        # Ubuntu >= 22.04, and the drill host runs 255).
        f"ExecStart=:{_quote(str(script_path(ctx.home)), 'script path')}\n"
        "# The retry policy, stated in systemd terms (shared/boot_policy.py).\n"
        "Restart=on-failure\n"
        f"RestartSec={BOOT_RETRY_INTERVAL_S}\n"
        "# A wedged attempt is killed and retried rather than blocking forever.\n"
        f"RuntimeMaxSec={CONVERGE_RUNTIME_MAX_S}\n"
        "TimeoutStopSec=30\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_script(ctx: BootUnitContext) -> str:
    """One convergence attempt; the exit code is the retry contract."""
    home = _clean(str(ctx.home), "ava home")
    repo = _clean(str(ctx.repo), "checkout path")
    home_dir = _clean(str(ctx.home_dir), "user home")
    if any(ch in f"{home}{repo}{home_dir}" for ch in _UNSAFE_IN_SCRIPT):
        raise ValueError("path values must not contain shell metacharacters")
    unit = unit_name(ctx.home)
    return f"""#!/bin/bash
# Ava cluster boot convergence -- ONE attempt per invocation.
#
# Installed as the ExecStart of {unit} (inspect with
# `ava cluster boot-unit status`). The retry is the UNIT's, not this script's:
# Restart=on-failure + RestartSec={BOOT_RETRY_INTERVAL_S} + StartLimitIntervalSec=0,
# no attempt cap (shared/boot_policy.py). Exit-code contract:
#
#   0  converged (`ava start` exited 0). A launched-but-unready service is the
#      watchdog's to revive -- never grounds for a retry (see boot_policy).
#   !=0 a step failed; the unit restarts this script after RestartSec.
set -u

export HOME="{home_dir}"
export AVA_HOME="{home}"
export PATH="{repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"

log="$AVA_HOME/logs/boot.log"
state="$AVA_HOME/logs/boot-converge.state"
t0=$SECONDS

# The redirects below and the state write assume logs/ exists; create it so a
# fresh home cannot turn a missing directory into an attempt that never ran
# ava start yet still asks the unit to retry forever.
mkdir -p "$AVA_HOME/logs"

# 1) Proxy readiness -- a real round trip through the configured entrypoint
#    (the unit's AVA_BOOT_PROXY_WAIT; empty disables the wait). Bounded; on
#    timeout the convergence below still runs and its failure, if any, is
#    retried by the unit.
proxy="disabled"
if [ -n "${{AVA_BOOT_PROXY_WAIT:-}}" ]; then
  proxy="no"
  for _ in $(seq 1 30); do
    if curl -sS -m 4 -x "$AVA_BOOT_PROXY_WAIT" -o /dev/null {GENERATE_204_URL}; then
      proxy="yes"
      break
    fi
    sleep 4
  done
fi
echo "[boot-converge] proxy_ready=$proxy t=+$((SECONDS - t0))s"

# 2) Converge (idempotent; the manual 2026-09-16 recovery ran the same verb).
#    --no-readiness-gate per boot_policy: the retried set stays "a step
#    failed"; boot.log keeps this attempt's output, truncated per attempt.
cd "{repo}"
"{repo}/.venv/bin/python" -m cli.main start --no-readiness-gate >"$log" 2>&1
rc=$?
if [ "$rc" != 0 ]; then
  echo "[boot-converge] ava start rc=$rc t=+$((SECONDS - t0))s -- the unit will retry"
  exit "$rc"
fi

# 3) Record readiness -- measurement only; the exit verdict above is final.
#    The URL is resolved the way every client resolves it -- env
#    AVA_GATEWAY_URL (the unit's .env) > $AVA_HOME/gateway_url file -- so the
#    probe dials what the cluster dials; a bare file read missed the .env URL.
gw_base=$("{repo}/.venv/bin/python" -c 'from shared.machines import gateway_url; print(gateway_url())' 2>/dev/null || true)
[ -n "$gw_base" ] || gw_base=$(cat "$AVA_HOME/gateway_url" 2>/dev/null || true)
gw="unknown"
if [ -n "$gw_base" ]; then
  gw="no"
  for _ in $(seq 1 15); do
    if curl -sS -m 3 -o /dev/null "$gw_base/api/health"; then
      gw="yes"
      break
    fi
    sleep 4
  done
fi
up=$(awk '{{print int($1)}}' /proc/uptime)
{{
  echo "state=$([ "$gw" = yes ] && echo ready || echo started)"
  echo "boot_to_ready_s=$up"
  echo "at=$(date -Is)"
  echo "proxy=$proxy"
  echo "gw_health=$gw"
}} >"$state"
echo "[boot-converge] done gw_health=$gw uptime=${{up}}s"
exit 0
"""


# --- privileged steps -------------------------------------------------------


def _privileged(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
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
    result = _privileged(argv)
    if result.returncode != 0:
        raise RuntimeError(_privileged_failure(what, result))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


# --- install / uninstall / status -------------------------------------------


def install(
    *,
    enable: bool = True,
    start: bool = False,
    proxy_wait_url: str = "",
    context: BootUnitContext | None = None,
) -> list[str]:
    """Render and install the boot unit + convergence script for this home.

    `enable=False` is a staged install (drills and reviewed rollouts write the
    files first). Enabling swaps the live boot path and removes the crontab
    entry in the same call, so the two never race at boot. `start=True` runs
    the unit once, now.
    `proxy_wait_url` is baked as the unit's AVA_BOOT_PROXY_WAIT (empty: no
    wait). Returns the steps performed, for the CLI to print. Raises
    RuntimeError when a privileged step fails.
    """
    if not systemd_running():
        raise RuntimeError(
            "the boot unit needs a Linux host running systemd as its service manager"
        )
    ctx = context if context is not None else _default_context()
    steps: list[str] = []

    script = script_path(ctx.home)
    script.parent.mkdir(parents=True, exist_ok=True)
    script_content = render_script(ctx)
    content_changed = _read_text(script) != script_content
    if content_changed:
        script.write_text(script_content)
        steps.append(f"wrote convergence script {script}")
    if script.stat().st_mode & 0o777 != 0o755:
        # A content-identical script whose mode drifted (e.g. 0644) would not
        # run under systemd -- repair it instead of reporting success over a
        # dead ExecStart.
        script.chmod(0o755)
        if not content_changed:
            steps.append(f"fixed {script} mode to 0755")

    destination = unit_path(ctx.home)
    unit_content = render_unit(ctx, proxy_wait_url=proxy_wait_url)
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

    if enable:
        if not unit_enabled(ctx.home):
            _privileged_or_raise(
                ["systemctl", "enable", unit_name(ctx.home)],
                f"enable {unit_name(ctx.home)}",
            )
            steps.append(f"enabled {unit_name(ctx.home)}")
        # Enabling swaps the live boot path -- drop the cron entry in the same
        # step so the two never race at boot (idempotent; converge also
        # reconciles this on every `ava start`).
        from shared.os_autostart import _unregister_linux

        if _crontab_has_autostart(ctx.home):
            _unregister_linux(home_slug(ctx.home))
            steps.append("removed the crontab autostart entry")
    if start:
        _privileged_or_raise(
            ["systemctl", "start", unit_name(ctx.home)],
            f"start {unit_name(ctx.home)}",
        )
        steps.append(f"started {unit_name(ctx.home)}")
    logger.info("boot unit ready for {}", ctx.home)
    return steps


def uninstall(home: Path | None = None) -> list[str]:
    """Remove this home's boot unit and convergence script.

    Safe when nothing is installed, and safe to call on non-systemd hosts (a
    no-op): `ava cluster destroy` runs it unconditionally with the other OS
    jobs. Removes only this home's exact unit/script paths.
    """
    if not IS_LINUX:
        return []
    home = home if home is not None else ava_home()
    steps: list[str] = []
    destination = unit_path(home)
    if destination.exists():
        result = _privileged(["systemctl", "disable", "--now", unit_name(home)])
        if result.returncode != 0:
            logger.warning("boot unit disable returned rc={}", result.returncode)
        _privileged_or_raise(["rm", "-f", str(destination)], f"remove {destination}")
        _privileged_or_raise(["systemctl", "daemon-reload"], "systemctl daemon-reload")
        steps.append(f"removed {destination}")
    script = script_path(home)
    if script.exists():
        script.unlink()
        steps.append(f"removed {script}")
    return steps


def status(home: Path | None = None) -> list[tuple[str, str]]:
    """Read-only operator report: unit, script, proxy wait, cron entry, last state.

    `unit content` / `script content` compare only for this process's own home:
    a foreign home renders from its own checkout/user by construction, so
    comparing it against this process's context would always read as
    "differs".
    """
    home = home if home is not None else ava_home()
    ctx = _default_context()
    rows: list[tuple[str, str]] = []

    destination = unit_path(home)
    unit_text = _read_text(destination)
    rows.append(("unit", unit_name(home)))
    rows.append(("unit file", f"{destination} ({'present' if unit_text else 'missing'})"))
    proxy_wait = ""
    if unit_text is not None:
        match = re.search(r'AVA_BOOT_PROXY_WAIT=([^"\n]*)', unit_text)
        proxy_wait = match.group(1) if match else ""
        if home == ctx.home:
            rendered = render_unit(ctx, proxy_wait_url=proxy_wait)
            rows.append(
                ("unit content", "matches rendered" if unit_text == rendered else "differs")
            )
        else:
            rows.append(("unit content", "not compared (not this process's home)"))
    if systemd_running():
        state = _systemctl("show", "-p", "ActiveState", "--value", unit_name(home)).stdout.strip()
        rows.append(("systemd state", state or "unknown"))
        rows.append(("enabled", "yes" if unit_enabled(home) else "no"))
    else:
        rows.append(("systemd state", "not running (the boot unit needs systemd)"))

    script = script_path(home)
    script_text = _read_text(script)
    rows.append(("script", f"{script} ({'present' if script_text else 'missing'})"))
    if script_text is not None:
        if home == ctx.home:
            rows.append(
                (
                    "script content",
                    "matches rendered" if script_text == render_script(ctx) else "differs",
                )
            )
        else:
            rows.append(("script content", "not compared (not this process's home)"))

    rows.append(("proxy wait", proxy_wait or "disabled"))

    rows.append(("cron entry", "present" if _crontab_has_autostart(home) else "absent"))
    rows.append(("last convergence", _read_text(state_path(home)) or "none"))
    return rows


def _crontab_lines() -> list[str]:
    if shutil.which("crontab") is None:
        # Minimal hosts (CI containers, benches) ship no crontab binary: there
        # is no entry to find, and install/status must degrade to "absent"
        # instead of raising (the same warn-and-skip stance as os_autostart).
        return []
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False, timeout=30
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


def _crontab_has_autostart(home: Path) -> bool:
    from shared.os_autostart import _CRON_MARKER

    marker = f"{_CRON_MARKER}.{home_slug(home)}"
    return any(marker in line for line in _crontab_lines())
