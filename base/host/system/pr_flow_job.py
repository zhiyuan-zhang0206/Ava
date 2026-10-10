"""Daily OS job for the PR-flow sampler (task #2139).

One daily job hands `scripts/ci/pull_requests/pr_flow_export.py` to the platform scheduler
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
`scripts/ci/pull_requests/pr_flow_export.py` (same checkout rule as
`base.host.system.cron.ava_binary_path`), so a worktree's converge — should the gate
ever pass there — would register its own pair, never prod's.

"""

from __future__ import annotations

import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron

# Daily at 00:25 host-local (macmini lives on the fleet wall clock,
# Asia/Shanghai): just past midnight so the previous day is complete, clear of
# the 00:00 self-evolution fire and the 04:00/04:40/05:00 maintenance block.
_HOUR = 0
_MINUTE = 25

_CRON_MARKER = "# ava-pr-flow"


def trunk_token_path() -> Path:
    """The Trunk API token the sampler reads — the design's credential file."""
    return Path.home() / ".trunk" / "api-token"


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.pr-flow"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "pr-flow.out.log"


def _python_path() -> str:
    """The owning checkout's venv python, beside the venv's `ava` binary."""
    return str(Path(base.host.system.cron.ava_binary_path()).parent / "python")


def _script_path() -> str:
    from base.paths import repo_root

    return str(repo_root() / "scripts" / "ci" / "pull_requests" / "pr_flow_export.py")


def _shell_command() -> str:
    return f"{shlex.quote(_python_path())} {shlex.quote(_script_path())}"


def _launchd_plist_content() -> str:
    log_file = _log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(_shell_command())}</string>
    </array>
{base.host.system.cron.launchd_env_block()}
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
    """Rewrite and reload the PR-flow LaunchAgent."""
    label = _LABEL
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if base.host.system.cron.reload_launchd_job(label, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", label, _HOUR, _MINUTE)
    return 0


def _unregister_macos() -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, _launchd_plist_path())
    return 0


def _cron_entry() -> str:
    log_file = _log_file()
    return (
        f"{_MINUTE} {_HOUR} * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())} "
        f">> {shlex.quote(str(log_file))} 2>&1  {_CRON_MARKER}"
    )


def _register_linux() -> int:
    """Replace the PR-flow line in the user crontab.

    Lines are matched by the marker as a substring, so a line an older version
    wrote with a per-home suffix after the marker is replaced in place.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * PR flow: crontab not installed; the daily sampler job cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if base.host.system.cron.replace_crontab_entry(
        marker,
        _cron_entry(),
        skip_phrase="PR-flow registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
    ):
        return 1
    logger.info("crontab PR-flow entry added ({})", marker)
    return 0


def _unregister_linux() -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None
    )


def credential_blocker() -> str | None:
    """Why this unit cannot run the sampler, or None when it can.

    Cheap and local by design (converge runs on every start): production-home
    identity, `gh` on PATH, the Trunk token file. No network probes — the
    sampler itself reports its own fetch failures loudly when it runs.
    """
    from base.telemetry.observability import production_identity

    if not production_identity():
        return "not the registered production home"
    if shutil.which("gh") is None:
        return "gh CLI not on PATH"
    token = trunk_token_path()
    if not token.exists() or not token.read_text(encoding="utf-8").strip():
        return f"no Trunk API token at {token}"
    return None


def register_pr_flow_job(*, enabled_reader: Callable[[], bool]) -> None:
    """Register the daily PR-flow sampler (idempotent).

    Skipped when OS jobs are off (`AVA_OS_JOBS_ENABLED` — the test suite), when
    this home is not the default home (`owns_os_jobs`) or when the credential
    gate says this host cannot run the sampler; the skip reason is logged so
    converge output explains the absence.
    """
    if not base.host.system.cron.os_jobs_enabled(enabled_reader=enabled_reader):
        base.host.system.cron.skip_os_job("pr flow")
        return
    if not base.host.system.cron.owns_os_jobs("pr flow"):
        return
    blocker = credential_blocker()
    if blocker is not None:
        logger.info("PR-flow sampler job not registered: {}", blocker)
        return
    from base.host.system.backend import get_backend

    get_backend().register_pr_flow_job()


def unregister_pr_flow_job() -> None:
    """Remove the PR-flow sampler job; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("pr flow"):
        return
    from base.host.system.backend import get_backend

    get_backend().unregister_pr_flow_job()
