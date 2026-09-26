"""OS-level cron registration for the cluster health probe.

Platform-aware registration of a periodic job that runs the cluster health probe.
The probe reports observations and graded alerts. Release actions are owned by
the retained release operation.

- macOS: launchd User LaunchAgent plist in ~/Library/LaunchAgents/
- Linux: user crontab entry
- Windows: a Task Scheduler job (see shared/os_schtasks.py)

This module is in the shared layer so both the gateway lifespan (primary
registration path) and the CLI converge step (belt-and-suspenders fallback) can
call the same functions without violating the import layering (shared < ava <
agent < gateway < cli).

It also carries the shared launchd / crontab mechanics used by the OS jobs:
the label prefix, the binary + `$AVA_HOME` a job spec is anchored to
(`ava_binary_path` / `job_home` / `launchd_env_block` / `cron_env_prefix`),
the crontab read-modify-write primitives, and the `os_jobs_enabled()` gate
every registrar consults.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from loguru import logger

from shared.config import settings
from shared.platform import crontab_lock, descends_from_launchd_job, launchd_job_label

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes

# launchd label: com.ava.<home-slug>.health-probe (the slug is
# `shared.cluster.home_slug` — basename + 8-hex path hash, so two homes sharing
# a basename get distinct labels).
LAUNCHD_LABEL_PREFIX = "com.ava"

# Crontab marker: the Linux analog of the launchd label's slug token, scoping a
# health-probe line to the cluster that registered it.
_CRON_MARKER = "# ava-health-probe"


def os_jobs_enabled() -> bool:
    """Whether this process may hand a job to the platform scheduler.

    Every other host resource a test isolates — `$AVA_HOME`, the database, redis,
    the service ports, the session records — is addressed by a value the process
    reads, so redirecting the value redirects the resource. The platform
    scheduler is not: launchd reads ONE `~/Library/LaunchAgents` per OS user,
    `crontab` edits ONE table per user, schtasks owns ONE `\\Ava\\` folder per
    user. A test-scoped home therefore isolates everything about a registered job
    except the namespace it lands in.

    So the suite turns registration off wholesale (`AVA_OS_JOBS_ENABLED=false`,
    set in `tests/conftest.py` and inherited by every subprocess it spawns)
    instead of registering into the operator's namespace and unregistering
    afterwards: a job that is never armed cannot fire mid-run, and cannot survive
    a session that is SIGKILLed before its teardown.

    Deregistration is deliberately NOT gated — a cleanup path has to work
    wherever registration is forbidden, including on the leftovers of a run that
    predates this switch.
    """
    return settings.general.os_jobs_enabled


def skip_os_job(kind: str) -> None:
    """Log that `kind` was not registered because `os_jobs_enabled()` is off."""
    logger.info("OS jobs disabled (AVA_OS_JOBS_ENABLED=false) — not registering {}", kind)


def reload_launchd_job(label: str, plist_path: Path) -> int:
    """Replace a loaded LaunchAgent after its caller writes the plist."""
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
    return 0


def remove_launchd_job(label: str, plist_path: Path) -> None:
    """Unload a LaunchAgent and remove its plist, even when already absent."""
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    plist_path.unlink(missing_ok=True)


def require_crontab(
    missing_message: str, *, missing_returncode: int, missing_stream: TextIO
) -> int | None:
    """Report a missing crontab with the caller's existing message and policy."""
    if shutil.which("crontab") is None:
        print(missing_message, file=missing_stream)
        return missing_returncode
    return None


def replace_crontab_entry(
    marker: str,
    entry: str,
    *,
    skip_phrase: str,
    update_failure: Callable[[str], None],
    owns_line: Callable[[str], bool] | None = None,
) -> int:
    """Replace owned lines, defaulting to a marker substring match.

    The caller reports a failed write in its own channel.
    """
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        # Only benign "no crontab" means empty; other read failures must block a clobbering rewrite.
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                f"skipping {skip_phrase} to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""
        lines = [
            line
            for line in current.splitlines()
            if not (owns_line(line) if owns_line is not None else marker in line)
        ]
        lines.append(entry)
        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            update_failure(result.stderr)
            return 1
    return 0


def remove_crontab_entry(
    marker: str,
    *,
    write_failure_rc: int,
    on_removed: Callable[[], None] | None,
    owns_line: Callable[[str], bool] | None = None,
    on_read_failed: Callable[[], None] | None = None,
    on_absent: Callable[[], None] | None = None,
) -> int:
    """Remove owned lines, defaulting to a marker substring match.

    Read failure and absent callbacks run before their no-op return. Removal is
    reported only after a successful write.
    """
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            if on_read_failed is not None:
                on_read_failed()
            return 0
        lines = [
            line
            for line in result.stdout.splitlines()
            if not (owns_line(line) if owns_line is not None else marker in line)
        ]
        if len(lines) == len(result.stdout.splitlines()):
            if on_absent is not None:
                on_absent()
            return 0
        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return write_failure_rc
        if on_removed is not None:
            on_removed()
    return 0


def ava_binary_path() -> str:
    """The absolute path to the `ava` binary of THIS CHECKOUT.

    The checkout is the anchor, not PATH. A job spec written by a worktree's code
    must run that worktree's binary, and prod's `~/.local/bin/ava` is a symlink to
    the prod checkout's venv binary anyway — so resolving the venv directly names
    the same file for prod and the correct file everywhere else. `shutil.which`
    survives only as the fallback for an install with no venv (a global pip
    install).

    Why the order matters: `which` resolves against the CALLING process's PATH,
    which under `uv run` / an activated venv is whatever checkout the caller came
    from — not necessarily the checkout that owns `$AVA_HOME`. A process could
    therefore write a job labelled for its own cluster but pointed at another
    cluster's binary; when that other binary is prod's, the job runs prod's
    health probe against prod's home. Resolving from
    `repo_root()` makes binary, `$AVA_HOME` and label come from one checkout.
    """
    from shared.paths import repo_root
    from shared.platform import IS_WINDOWS
    from shared.platform_backend import get_backend

    scripts = repo_root() / ".venv" / get_backend().venv_bin_dir_name()
    candidate = scripts / ("ava.exe" if IS_WINDOWS else "ava")
    if candidate.exists():
        return str(candidate)

    import shutil

    return shutil.which("ava") or str(candidate)


def job_home() -> str:
    """The `$AVA_HOME` a generated job spec pins, as a string.

    Redundancy, not the primary defence: `ava_binary_path()` already resolves to
    the checkout that owns this home, and that binary's own boot resolves the
    same home (`resolve_ava_home`). Pinning it in the spec makes the job
    self-describing — it no longer depends on the checkout's `.ava_home` pointer
    still being on disk — and it removes the one path by which a stale job could
    reach the prod home: an `ava` that resolved through PATH with no `AVA_HOME`
    set falls back to the prod source.
    """
    return str(Path(settings.general.ava_home))


def launchd_env_block(indent: str = "    ", extra: dict[str, str] | None = None) -> str:
    """The `EnvironmentVariables` plist fragment every LaunchAgent here carries:
    the cluster's `AVA_HOME` (see `job_home`) and a usable PATH (see
    `launchd_path_env`), followed by whatever `extra` the caller adds.

    `extra` is rendered in the caller's own order, so one caller's dict always
    produces the same bytes — a plist that is compared against the one on disk to
    decide whether the job needs replacing
    cannot afford a fragment that reshuffles between runs.
    """
    entries = {"AVA_HOME": job_home(), "PATH": launchd_path_env(), **(extra or {})}
    return (
        f"{indent}<key>EnvironmentVariables</key>\n"
        f"{indent}<dict>\n"
        + "".join(
            f"{indent}    <key>{key}</key>\n{indent}    <string>{value}</string>\n"
            for key, value in entries.items()
        )
        + f"{indent}</dict>"
    )


def cron_env_prefix() -> str:
    """The `AVA_HOME=<home> ` prefix every crontab line here carries — the
    crontab analog of `launchd_env_block` (cron runs each line through /bin/sh,
    so a leading assignment scopes to that command alone; a bare `AVA_HOME=`
    line would instead apply to the whole file, including a co-located cluster's
    entries)."""
    return f"AVA_HOME={job_home()} "


def _home_slug() -> str:
    """The per-cluster label token — the home-path slug (path-only identity)."""
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


def _cron_marker(slug: str) -> str:
    """Trailing comment that stamps a crontab line with the cluster that owns it.

    The macOS label already carries the slug; the crontab line had nothing, so
    every cluster's line looked identical and one cluster's register/unregister
    rewrote them all.
    """
    return f"{_CRON_MARKER}.{slug}"


def _owns_health_probe_line(line: str, slug: str) -> bool:
    """True when `line` is the health-probe crontab entry of the cluster `slug`.

    An UNMARKED health-probe line also matches, whichever slug is asked. Markers
    are new: before them the register path rewrote every health-probe line it
    found, so a host could hold at most one unmarked entry and there is nothing
    to disambiguate. Leaving it behind would strand a job pointing at a home that
    may no longer have a cluster — the exact failure this scoping exists to
    prevent. Self-limiting: one `ava start` after the upgrade marks the line.
    """
    if _CRON_MARKER in line:
        return _cron_marker(slug) in line
    return "ava cluster health-probe" in line or "health-probe-cron" in line


def launchd_path_env() -> str:
    """PATH for a LaunchAgent's environment.

    launchd hands a job a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that omits
    Homebrew's bin, so an `ava` subcommand launched from a plist cannot find
    npx / node. Compose a PATH from the dir holding this host's `ava`, the brew
    prefix (probed from the current PATH, falling back to the standard
    Apple-silicon / Intel locations), and the base system dirs.

    Shared by every LaunchAgent this repo writes — the autostart job (which runs
    `ava start`) and the watchdog probe (which respawns a
    dead watchdog). A probe without brew on PATH would fail exactly when it is
    needed, so this is not cosmetic.
    """
    import shutil

    dirs: list[str] = [str(Path(ava_binary_path()).parent)]  # ~/.local/bin or venv/bin
    brew = shutil.which("brew")
    dirs += [str(Path(brew).parent)] if brew else ["/opt/homebrew/bin", "/usr/local/bin"]
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen: set[str] = set()
    return ":".join(d for d in dirs if not (d in seen or seen.add(d)))


def _health_probe_label(slug: str) -> str:
    """The launchd label for one cluster's health probe."""
    return f"{LAUNCHD_LABEL_PREFIX}.{slug}.health-probe"


def _launchd_plist_path(slug: str) -> Path:
    """Path to the launchd plist for the cluster whose home slug is `slug`."""
    return Path.home() / "Library" / "LaunchAgents" / f"{_health_probe_label(slug)}.plist"


def _launchd_plist_content(interval_s: int) -> str:
    """Generate the observation-only health-probe launchd job."""
    ava_path = ava_binary_path()
    label = _health_probe_label(_home_slug())
    log_dir = Path(settings.general.ava_home) / "logs"
    log_file = log_dir / "health-probe.log"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ava_path}</string>
        <string>cluster</string>
        <string>health-probe</string>
    </array>
{launchd_env_block()}
    <key>StartInterval</key>
    <integer>{interval_s}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def _own_probe_job_of(own_labels: set[str]) -> str | None:
    """The health-probe label this process runs under, or None when external.

    ``XPC_SERVICE_NAME`` matches only for the job's direct child — exec'd
    descendants read "0" (see `shared.platform.descends_from_launchd_job`) —
    so the environment is a fast path and the live process tree is the proof.
    Labels are probed in sorted order for deterministic logging/reporting."""
    current = launchd_job_label()
    if current is not None and current in own_labels:
        return current
    for candidate in sorted(own_labels):
        if descends_from_launchd_job(candidate):
            return candidate
    return None


def _register_macos(interval_s: int) -> int:
    """Register the health probe as a launchd User LaunchAgent.

    Writes the plist to ~/Library/LaunchAgents/ and loads it with
    `launchctl bootstrap`. A call descended from this job leaves its own loaded
    spec untouched; the next external converge applies any pending change."""
    slug = _home_slug()
    label = _health_probe_label(slug)
    plist_path = _launchd_plist_path(slug)

    # `bootout` terminates the job's whole process tree. A registering job
    # cannot replace itself safely. Leave its plist in place so the next
    # external converge sees the desired-content change and reloads it.
    # Ownership is two-pronged: the inherited `XPC_SERVICE_NAME` is a cheap
    # fast path that only the job's direct child matches, while descendants —
    # where converges actually run — read "0", so the live process tree check
    # is the proof (postmortems/0008).
    own_job = _own_probe_job_of({label})
    if own_job is not None:
        logger.info("Health probe '{}' is registering itself — deferring reload", own_job)
        return 0

    # Ensure the LaunchAgents directory exists.
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the plist.
    plist_path.write_text(_launchd_plist_content(interval_s))
    logger.info("Wrote plist to {}", plist_path)

    # Unload any existing instance (ignore errors if not loaded).
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )

    # Load (bootstrap) the new plist.
    result = subprocess.run(  # noqa: S603
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed: {}", result.stderr)
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, interval_s)
    return 0


def _unregister_macos(slug: str) -> int:
    """Remove the launchd job and plist of the cluster whose home slug is `slug`."""
    plist_path = _launchd_plist_path(slug)
    label = _health_probe_label(slug)

    # Bootout the job.
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )

    # Remove the plist.
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed plist {}", plist_path)

    logger.info("launchd job '{}' unloaded", label)
    return 0


def _register_linux(interval_s: int) -> int:
    """Add the health probe to the user's crontab.

    Reads the current crontab, removes any existing Ava health-probe entry,
    appends the new one, and writes it back. Idempotent. The crontab line runs
    the observation-only probe directly, with no shell wrapper.

    When crontab is not installed on the host (a minimal Linux box such as a
    hermetic bench / CI container), the health probe is a capability this host
    cannot provide — warn and skip rather than fail the whole bring-up, the same
    way converge degrades on an absent browser / permissions-helper. A real long-lived
    gateway has cron and registers normally; an actual registration error (cron
    present but the write fails) still returns non-zero below."""
    missing = require_crontab(
        "  ! health probe cron: crontab not installed on this host (skipping); "
        "cluster runs without a health-probe cron",
        missing_returncode=0,
        missing_stream=sys.stdout,
    )
    if missing is not None:
        return missing

    ava_path = ava_binary_path()
    slug = _home_slug()
    minutes = max(1, interval_s // 60)
    entry = (
        f"*/{minutes} * * * * {cron_env_prefix()}{ava_path} cluster health-probe "
        f"{_cron_marker(slug)}"
    )

    def report_update_failure(err: str) -> None:
        print(f"  * crontab update failed: {err}", file=sys.stderr)  # noqa: T201

    rc = replace_crontab_entry(
        _cron_marker(slug),
        entry,
        skip_phrase="health-probe registration",
        update_failure=report_update_failure,
        owns_line=lambda line: _owns_health_probe_line(line, slug),
    )
    if rc == 0:
        print(f"  . crontab entry added (every {minutes} min)")  # noqa: T201
    return rc


def _unregister_linux(slug: str) -> int:
    """Remove one cluster's health-probe entry from the user's crontab."""

    def report_read_failure() -> None:
        print("  . no crontab to unregister")  # noqa: T201

    def report_absent() -> None:
        print("  . no Ava health-probe entry found in crontab")  # noqa: T201

    def report_removed() -> None:
        print("  . crontab entry removed")  # noqa: T201

    return remove_crontab_entry(
        _cron_marker(slug),
        write_failure_rc=0,
        on_removed=report_removed,
        owns_line=lambda line: _owns_health_probe_line(line, slug),
        on_read_failed=report_read_failure,
        on_absent=report_absent,
    )


# Windows-only: how long ONE health-probe invocation may run before Task Scheduler
# ends it (launchd and cron bound nothing, so there is no equivalent to mirror).
# A bound is needed because a task's instance policy is `IgnoreNew`, so a wedged
# invocation blocks every later one — three days of it, at the scheduler's 72h
# default.
#
# Keep the existing bound for observation and alert delivery. IgnoreNew prevents
# overlapping probes; release transitions never run beneath this scheduled job.
# Gateway is POSIX-only today, so this applies if Windows gains that capability.
_WINDOWS_TIME_LIMIT_S = 1800


def _register_windows(interval_s: int) -> str | None:
    """Register the observation-only health probe as a Windows task."""
    from shared.os_schtasks import create_minute_task

    return create_minute_task(
        "health-probe",
        ("cluster", "health-probe"),
        interval_s // 60,
        time_limit_s=_WINDOWS_TIME_LIMIT_S,
    )


def _unregister_windows(slug: str) -> int:
    from shared.os_schtasks import delete_task

    return delete_task("health-probe", slug)


def register_os_cron(
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Register the OS cron job for the cluster health probe.

    Platform-aware: delegates to ``PlatformBackend.register_cron``.
    Idempotent — re-running updates the interval and reloads the job.
    A no-op when ``os_jobs_enabled()`` is off (the test suite).

    Refused (with an error log) when this process runs from a non-prod
    checkout against the prod home — the 2026-08-07 accident (Task #1025): a
    debug/test process launched from a worktree's venv (``from gateway.app
    import app`` + TestClient, no conftest) invoked this registrar, which
    rewrote the prod health-probe plist to point at the worktree's disposable
    ``ava``. The probe then ran worktree code against prod data, misjudged
    schema health, and auto-rolled-back (quiesce restarted every agent). The
    job's content is anchored to THIS process's checkout, so the registration
    must be too: same rule as ``ava start`` (``prod_service_checkout_error``).

    Raises:
        RuntimeError: on registration failure (POSIX). The Windows backend
        degrades to a loud warning instead — see `WindowsPlatformBackend`.
    """
    if not os_jobs_enabled():
        skip_os_job("health-probe")
        return
    from shared.paths import prod_service_checkout_error, repo_root

    refusal = prod_service_checkout_error(repo_root())
    if refusal is not None:
        logger.error("health-probe registration refused: {}", refusal)
        return
    from shared.platform_backend import get_backend

    get_backend().register_cron(interval_s=interval_s)


def unregister_os_cron(home: Path | None = None) -> None:
    """Remove the OS cron job for a cluster's health probe.

    `home` selects WHICH cluster's job to remove; it defaults to this process's
    own home. Pass it explicitly when acting on another cluster (`ava cluster
    destroy --path`) — the target cannot be carried in `AVA_HOME`, because
    `settings` is built once at import and a later mutation of the environment
    would silently deregister this process's cluster instead.

    There is deliberately no such parameter on `register_os_cron`: a registered
    job's content (the `ava` binary path, the log directory, the cluster env) all
    comes from this process, so registering "for" another home would write a job
    labelled with that home but wired to this one. Only the job's NAME is needed
    to remove it, and that is exactly the home slug.

    Platform-aware. Safe to call when no job is registered (no-op).
    """
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_cron(slug_for_home(home))
