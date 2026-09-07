"""OS-level cron registration for the cluster health probe.

Platform-aware registration of a periodic job that runs the cluster health probe.
The probe itself counts consecutive failures and triggers rollback once the
threshold is reached.

- macOS: launchd User LaunchAgent plist in ~/Library/LaunchAgents/
- Linux: user crontab entry
- Windows: a Task Scheduler job (see shared/os_schtasks.py)

This module is in the shared layer so both the gateway lifespan (primary
registration path) and the CLI converge step (belt-and-suspenders fallback) can
call the same functions without violating the import layering (shared < ava <
agent < gateway < cli).

It also carries what all four OS-scheduled job kinds share, because they share
the same launchd / crontab mechanics and `os_autostart` / `os_watchdog_probe`
already import from here: the label prefix, the binary + `$AVA_HOME` a job spec
is anchored to (`ava_binary_path` / `job_home` / `launchd_env_block` /
`cron_env_prefix`), and the `os_jobs_enabled()` gate every registrar consults.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from shared.config import settings
from shared.platform import crontab_lock, launchd_job_label

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes
DEFAULT_CONSECUTIVE_THRESHOLD = 3

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
    health probe (`--auto-rollback`) against prod's home. Resolving from
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
    decide whether the job needs replacing (`cli/commands/_converge_gate.py`)
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


def _legacy_label_tokens() -> list[str]:
    """Transitional (path-only cutover): the pre-path-only label tokens for THIS
    home — the cluster name the retired `~/.ava[-<name>]` convention derived
    (`~/.ava` -> main, `~/.ava-<x>` -> x), PLUS whatever the retired
    `$AVA_HOME/cluster` identity file says if it is still on disk (an
    explicitly-named / off-convention pre-cutover home labeled its jobs by that
    name, not the convention). Used ONLY to clean up this home's own old launchd
    job; scoping to this home's tokens means a co-located cluster's job is never
    touched."""
    from shared.cluster import is_default_home
    from shared.paths import ava_home

    home = ava_home()
    tokens: list[str] = []
    if is_default_home(home):
        tokens.append("main")
    prefix = ".ava-"
    if home.name.startswith(prefix):
        tokens.append(home.name[len(prefix) :])
    legacy_file = home / "cluster"
    if legacy_file.exists():
        v = legacy_file.read_text().strip()
        if v and v not in tokens:
            tokens.append(v)
    return tokens


def cleanup_legacy_macos_job(kind: str) -> None:
    """Boot out + delete this home's pre-path-only launchd job
    (`com.ava.<old-cluster-name>.<kind>`), if present. Idempotent; called by the
    register paths (health-probe / autostart) after the slug-labeled job is in
    place, so a relabel never leaves a stale duplicate running."""
    for token in _legacy_label_tokens():
        label = f"{LAUNCHD_LABEL_PREFIX}.{token}.{kind}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.exists():
            continue
        res = subprocess.run(  # noqa: S603
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        plist.unlink(missing_ok=True)
        if res.returncode == 0:
            print(f"  . removed legacy launchd job '{label}'")  # noqa: T201
            logger.info("Removed legacy launchd job '{}'", label)
        else:
            # The plist is gone either way (that is the durable part); do not
            # claim the bootout succeeded when it did not.
            print(  # noqa: T201
                f"  ! removed legacy plist for '{label}' (bootout rc={res.returncode}: "
                f"{res.stderr.strip() or 'job not loaded'})",
                file=sys.stderr,
            )


def _health_probe_label(slug: str) -> str:
    """The launchd label for one cluster's health probe."""
    return f"{LAUNCHD_LABEL_PREFIX}.{slug}.health-probe"


def _launchd_plist_path(slug: str) -> Path:
    """Path to the launchd plist for the cluster whose home slug is `slug`."""
    return Path.home() / "Library" / "LaunchAgents" / f"{_health_probe_label(slug)}.plist"


def _launchd_plist_content(interval_s: int, threshold: int) -> str:
    """Generate the launchd plist XML for the health probe.

    The probe runs with `--auto-rollback --threshold <threshold>` so it counts
    consecutive failures and triggers rollback itself (macOS launchd has no
    shell wrapper to do the counting)."""
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
        <string>--auto-rollback</string>
        <string>--threshold</string>
        <string>{threshold}</string>
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


def _register_macos(interval_s: int, threshold: int) -> int:
    """Register the health probe as a launchd User LaunchAgent.

    Writes the plist to ~/Library/LaunchAgents/ and loads it with
    `launchctl bootstrap`. A call descended from this job leaves its own loaded
    spec untouched; the next external converge applies any pending change."""
    slug = _home_slug()
    label = _health_probe_label(slug)
    plist_path = _launchd_plist_path(slug)

    # `bootout` terminates the job's whole process tree. Auto-rollback invokes
    # `ava start` below this LaunchAgent, so replacing the job here would kill
    # the rollback before its finally block can resume the cluster and release
    # the update lease. Leave the old plist in place so a later external start
    # still sees any desired-content change and performs the deferred reload.
    current_job = launchd_job_label()
    if current_job is not None:
        own_labels = {label, *(_health_probe_label(token) for token in _legacy_label_tokens())}
        if current_job in own_labels:
            logger.info("Health probe '{}' is registering itself — deferring reload", current_job)
            return 0

    # Ensure the LaunchAgents directory exists.
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the plist.
    plist_path.write_text(_launchd_plist_content(interval_s, threshold))
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
    cleanup_legacy_macos_job("health-probe")
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


def _register_linux(interval_s: int, threshold: int) -> int:
    """Add the health probe to the user's crontab.

    Reads the current crontab, removes any existing Ava health-probe entry,
    appends the new one, and writes it back. Idempotent. The crontab line runs
    the probe directly with `--auto-rollback --threshold N` — the probe counts
    failures and triggers rollback itself, so there is no shell wrapper.

    When crontab is not installed on the host (a minimal Linux box such as a
    hermetic bench / CI container), the health probe is a capability this host
    cannot provide — warn and skip rather than fail the whole bring-up, the same
    way converge degrades on an absent browser / permissions-helper. A real long-lived
    gateway has cron and registers normally; an actual registration error (cron
    present but the write fails) still returns non-zero below."""
    import shutil

    if shutil.which("crontab") is None:
        print(  # noqa: T201
            "  ! health probe cron: crontab not installed on this host (skipping); "
            "cluster runs without a health-probe cron"
        )
        return 0

    ava_path = ava_binary_path()

    # Read current crontab. A non-zero rc is only safe to treat as "no crontab
    # yet" when it IS that case — any other failure (permissions, a broken cron)
    # would make the rewrite below clobber the user's real crontab from "".
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                "skipping health-probe registration to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""

        # Drop this cluster's own entry (and any pre-marker one, see _is_ava_health_probe_line),
        # leaving a co-located cluster's marked line alone.
        slug = _home_slug()
        lines = [line for line in current.splitlines() if not _owns_health_probe_line(line, slug)]

        # Add the new entry, marker-tagged so a later unregister can tell whose it is.
        minutes = max(1, interval_s // 60)
        new_entry = (
            f"*/{minutes} * * * * {cron_env_prefix()}{ava_path} cluster health-probe "
            f"--auto-rollback --threshold {threshold} {_cron_marker(slug)}"
        )
        lines.append(new_entry)

        # Write the new crontab.
        new_crontab = "\n".join(lines) + "\n"
        result = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  * crontab update failed: {result.stderr}", file=sys.stderr)  # noqa: T201
            return 1
        print(f"  . crontab entry added (every {minutes} min, threshold={threshold})")  # noqa: T201
        return 0


def _unregister_linux(slug: str) -> int:
    """Remove one cluster's health-probe entry from the user's crontab."""
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print("  . no crontab to unregister")  # noqa: T201
            return 0

        lines = [
            line for line in result.stdout.splitlines() if not _owns_health_probe_line(line, slug)
        ]

        if len(lines) == len(result.stdout.splitlines()):
            print("  . no Ava health-probe entry found in crontab")  # noqa: T201
            return 0

        new_crontab = "\n".join(lines) + "\n"
        subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            capture_output=True,
            text=True,
            check=False,
        )
        print("  . crontab entry removed")  # noqa: T201
    return 0


# Windows-only: how long ONE health-probe invocation may run before Task Scheduler
# ends it (launchd and cron bound nothing, so there is no equivalent to mirror).
# A bound is needed because a task's instance policy is `IgnoreNew`, so a wedged
# invocation blocks every later one — three days of it, at the scheduler's 72h
# default.
#
# 1800s, an order of magnitude above the probe's own checks, because the bound has
# to clear the longest thing an invocation can legitimately DO, not the longest
# check. With `--auto-rollback` the probe runs `ava cluster rollback --yes` as a
# child (`cli/commands/_cluster_health.py:_handle_consecutive_failure`), and that
# quiesces every agent, reverses migrations, `git reset --hard`, `uv sync`, `ava
# start` — plus a recovery path that does a second `uv sync` + start. Task
# Scheduler ends a task by tearing down its job object, so the child dies with the
# probe: a limit that truncated a rollback would leave schema behind code, which is
# far worse than the missing health signal a long bound costs (at a 300s cadence,
# at most five skipped ticks). `IgnoreNew` is also what stops two auto-rollbacks
# from ever running at once, so it stays.
#
# Not reachable on Windows today — the health probe is gateway-gated and `gateway`
# is POSIX-only — so this is the value that applies if that ever changes.
_WINDOWS_TIME_LIMIT_S = 1800


def _register_windows(interval_s: int, threshold: int) -> str | None:
    """Register the health probe as a Windows scheduled task.

    Same payload as the launchd / crontab paths — the probe counts consecutive
    failures and triggers rollback itself (`--auto-rollback --threshold N`)."""
    from shared.os_schtasks import create_minute_task

    return create_minute_task(
        "health-probe",
        ("cluster", "health-probe", "--auto-rollback", "--threshold", str(threshold)),
        interval_s // 60,
        time_limit_s=_WINDOWS_TIME_LIMIT_S,
    )


def _unregister_windows(slug: str) -> int:
    from shared.os_schtasks import delete_task

    return delete_task("health-probe", slug)


def register_os_cron(
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
) -> None:
    """Register the OS cron job for the cluster health probe.

    Platform-aware: delegates to ``PlatformBackend.register_cron``.
    Idempotent — re-running updates the interval/threshold and reloads the job.
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

    get_backend().register_cron(interval_s=interval_s, threshold=threshold)


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
