"""Daily OS job for the PR-flow sampler (task #2139).

One daily job hands `scripts/pr_flow_export.py` to the platform scheduler
(launchd / crontab) at 00:25 host-local time: the previous cluster-tz day is
complete by then, so the run aggregates it — and re-emits the whole trailing
window — with the day's data final.

The job is **credential-gated**: it registers only on a unit whose machine
holds the sampler's two credentials — a `gh` session on PATH and
`~/.trunk/api-token` — and only on the registered production home. In the
fleet that is macmini, the location the 2026-09-14 design verified; every
other unit's converge skips quietly, and a machine that later gains the
credentials registers on its own next converge. Nothing here probes the
network: the checks are `shutil.which` plus one file read, so converge
stays cheap.

The job command runs the checkout's own venv python against the checkout's
`scripts/pr_flow_export.py` (same checkout-anchored resolution as
`shared.os_cron.ava_binary_path`), so a worktree's converge — should the gate
ever pass there — would register its own pair, never prod's.

Windows carries no registration path: its schtasks tasks invoke the CLI
(`python -m cli.main <argv>`) and the sampler is a script with no CLI verb;
the machine holding the credentials is macOS, so the Windows backend is a
deliberate no-op rather than a second invocation mechanism.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import shared.os_cron
from shared.platform import crontab_lock

# Daily at 00:25 host-local (macmini lives on the fleet wall clock,
# Asia/Shanghai): just past midnight so the previous day is complete, clear of
# the 00:00 self-evolution fire and the 04:00/04:40/05:00 maintenance block.
_HOUR = 0
_MINUTE = 25
# One invocation: steady-state is minutes; the bound leaves headroom for a
# cold 30-day window walk (bounded by the export script's own page caps).
_WINDOWS_TIME_LIMIT_S = 1800

_CRON_MARKER = "# ava-pr-flow"


def trunk_token_path() -> Path:
    """The Trunk API token the sampler reads — the design's credential file."""
    return Path.home() / ".trunk" / "api-token"


def _label(slug: str) -> str:
    return f"{shared.os_cron.LAUNCHD_LABEL_PREFIX}.{slug}.pr-flow"


def _launchd_plist_path(slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(slug)}.plist"


def _log_file() -> Path:
    return Path(shared.os_cron.job_home()) / "logs" / "pr-flow.out.log"


def _python_path() -> str:
    """The owning checkout's venv python, beside the venv's `ava` binary."""
    return str(Path(shared.os_cron.ava_binary_path()).parent / "python")


def _script_path() -> str:
    from shared.paths import repo_root

    return str(repo_root() / "scripts" / "pr_flow_export.py")


def _shell_command() -> str:
    return f"{shlex.quote(_python_path())} {shlex.quote(_script_path())}"


def _launchd_plist_content() -> str:
    slug = shared.os_cron._home_slug()
    log_file = _log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_label(slug)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(_shell_command())}</string>
    </array>
{shared.os_cron.launchd_env_block()}
    <key>StartCalendarInterval</key>
    <dict>
            <key>Hour</key>
            <integer>{_HOUR}</integer>
            <key>Minute</key>
            <integer>{_MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def _register_macos() -> int:
    """Rewrite and reload this cluster's PR-flow LaunchAgent."""
    slug = shared.os_cron._home_slug()
    label = _label(slug)
    plist_path = _launchd_plist_path(slug)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    result = subprocess.run(  # noqa: S603
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed for {}: {}", label, result.stderr)
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", label, _HOUR, _MINUTE)
    return 0


def _remove_macos_job(label: str, plist_path: Path) -> None:
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    plist_path.unlink(missing_ok=True)


def _unregister_macos(slug: str) -> int:
    _remove_macos_job(_label(slug), _launchd_plist_path(slug))
    return 0


def _cron_marker(slug: str) -> str:
    return f"{_CRON_MARKER}.{slug}"


def _cron_entry() -> str:
    log_file = _log_file()
    return (
        f"{_MINUTE} {_HOUR} * * * {shared.os_cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())} "
        f">> {shlex.quote(str(log_file))} 2>&1  # {_CRON_MARKER}"
    )


def _register_linux() -> int:
    """Replace this cluster's PR-flow line in the user crontab."""
    if shutil.which("crontab") is None:
        print(  # noqa: T201
            "  * PR flow: crontab not installed; the daily sampler job cannot be registered",
            file=sys.stderr,
        )
        return 1

    slug = shared.os_cron._home_slug()
    marker = _cron_marker(slug)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = f"{_cron_entry()}  {marker}"
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                "skipping PR-flow registration to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""
        lines = [line for line in current.splitlines() if marker not in line]
        lines.append(entry)
        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  * crontab update failed: {result.stderr}", file=sys.stderr)  # noqa: T201
            return 1
    logger.info("crontab PR-flow entry added ({})", marker)
    return 0


def _unregister_linux(slug: str) -> int:
    marker = _cron_marker(slug)
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return 0
        lines = [line for line in result.stdout.splitlines() if marker not in line]
        if len(lines) == len(result.stdout.splitlines()):
            return 0
        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return 1
    return 0


def credential_blocker() -> str | None:
    """Why this unit cannot run the sampler, or None when it can.

    Cheap and local by design (converge runs on every start): production-home
    identity, `gh` on PATH, the Trunk token file. No network probes — the
    sampler itself reports its own fetch failures loudly when it runs.
    """
    from shared.observability import production_identity

    if not production_identity():
        return "not the registered production home"
    if shutil.which("gh") is None:
        return "gh CLI not on PATH"
    token = trunk_token_path()
    if not token.exists() or not token.read_text(encoding="utf-8").strip():
        return f"no Trunk API token at {token}"
    return None


def register_pr_flow_job() -> None:
    """Register this cluster's daily PR-flow sampler (idempotent).

    Skipped when OS jobs are off (`AVA_OS_JOBS_ENABLED` — the test suite) or
    when the credential gate says this host cannot run the sampler; the skip
    reason is logged so converge output explains the absence.
    """
    if not shared.os_cron.os_jobs_enabled():
        shared.os_cron.skip_os_job("pr flow")
        return
    blocker = credential_blocker()
    if blocker is not None:
        logger.info("PR-flow sampler job not registered: {}", blocker)
        return
    from shared.platform_backend import get_backend

    get_backend().register_pr_flow_job()


def unregister_pr_flow_job(home: Path | None = None) -> None:
    """Remove a cluster's PR-flow sampler job; safe when none is registered."""
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_pr_flow_job(slug_for_home(home))
