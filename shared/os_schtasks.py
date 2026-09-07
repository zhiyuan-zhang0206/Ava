"""Windows Task Scheduler primitives — the ``schtasks.exe`` analog of the launchd
plist writer and the crontab editor.

Four job kinds need the platform scheduler (health probe, watchdog probe, boot
autostart, log maintenance). On macOS they share the LaunchAgent plist machinery; on Linux they
share the crontab rewrite. This module is the third of those — the shared
Windows half — so each ``os_*`` module's ``_register_windows`` stays a few lines.

Three decisions are worth stating, because all three are load-bearing and none is
obvious:

**Registration goes through task XML (``/Create /XML``), not ``/Create`` flags.**

A bare ``schtasks /Create`` accepts every Task Scheduler *default*, and two of
those defaults silently make the whole OS-level self-healing chain conditional on
a power cable: ``DisallowStartIfOnBatteries`` and ``StopIfGoingOnBatteries`` are
both true by default, so on a laptop runner the minute-cadence watchdog probe —
the supervisor of last resort, the only thing that revives a dead watchdog — is
stopped the moment the machine is unplugged and refuses to start again while on
battery. ``StartWhenAvailable`` defaults to false, so the missed ticks are never
caught up either, and the default ``ExecutionTimeLimit`` of 72 hours means one
wedged invocation blocks every later one for three days (the instance policy is
``IgnoreNew``). None of that has a ``schtasks`` command-line flag.

XML is what makes every one of those settings explicit in a single reviewable
artifact, and the artifact is exactly the shape ``schtasks /Query /XML`` returns,
so an operator can diff intent against reality. The alternatives lost on two
counts. Creating with flags and then patching with PowerShell
``Set-ScheduledTask`` is two steps whose *second* step is the one that matters:
if it fails, the task exists carrying precisely today's defaults — a silent
degradation, which is the failure mode this module is being fixed for. Driving
``Register-ScheduledTask`` throughout would put the whole configuration inside a
PowerShell one-liner assembled by string concatenation, unreviewable and
untestable off-box, and adds a second scripting language to a module that shells
one binary. With ``/XML`` a mistake is loud instead: ``schtasks`` rejects the file,
``create_*`` returns non-zero and converge fails fast.

**The command runs the venv's ``pythonw.exe -m cli.main``, never ``ava.exe``.**

- *Windowless.* A ``schtasks`` job runs in the logged-on user's session (the
  correct analog of a macOS ``gui/<uid>`` LaunchAgent, since the winproc
  sessions it supervises live in that session too). ``ava.exe`` is a console
  entry point, so a once-a-minute job would flash a console window at the user
  forever. ``pythonw.exe`` is Python's windowless launcher and does not.
- *Deterministic interpreter.* There is no ``~/.local/bin/ava`` symlink model on
  Windows (``supports_ava_symlink()`` is False), so PATH lookup is not
  trustworthy: on the reference box ``shutil.which("ava")`` resolves to a
  globally pip-installed ``C:\\Python313\\Scripts\\ava.exe``, a different binary
  from the prod venv's. Naming the venv interpreter outright removes the
  ambiguity — the scheduled job runs the same interpreter (and dependency set)
  as every other service on that host.

**Task names are folder-scoped per cluster home** — ``\\Ava\\<home-slug>\\<kind>``
— mirroring the launchd label ``com.ava.<home-slug>.<kind>``. Two co-located
clusters therefore never overwrite each other's jobs.

Unlike the launchd plist (``EnvironmentVariables``) and the crontab line (a
leading ``AVA_HOME=`` assignment), a task action has no slot for environment, so
a Windows task cannot pin ``$AVA_HOME`` the way ``shared.os_cron.job_home`` does
elsewhere. It does not need to: the action names the venv interpreter of one
specific checkout, and that checkout's boot resolves the home it owns
(``resolve_ava_home`` falls back to the checkout's ``.ava_home`` pointer).
"""

from __future__ import annotations

import getpass
import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from shared.platform import CREATE_NO_WINDOW

# Root folder for every task this repo registers, so `schtasks /Query /TN \Ava\`
# shows the whole set and nothing of ours is loose in the root namespace.
TASK_FOLDER = "Ava"

# The shape every live cluster's task folder carries: `<basename>-<8-hex>`
# (home_slug() has produced exactly this since the path-only identity cutover).
# Anything under \Ava\ that does not match it cannot belong to a live cluster —
# every cluster registers under its hash slug — so it is a leftover of an older
# naming or of a deleted cluster, and the reap reclaims it.
_CURRENT_SLUG_RE = re.compile(r".+-[0-9a-f]{8}$")

# Delay before the one retry of a failed `schtasks /Create`. The failure class
# this retry exists for is transient (win 2026-08-11, task #1196: a concurrent
# updater tore the definition XML mid-write and a ghost task instance raced the
# /Create), so one short wait clears it; a second failure is real and is
# reported as such.
_REGISTER_RETRY_DELAY_S = 1.0

# The Task Scheduler v2 schema every registered task declares.
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# `time_limit_s=0` means "no execution time limit": `PT0S` is the schema's
# documented way to disable it (it is also what the GUI writes when "Stop the
# task if it runs longer than" is unchecked). Only the boot job asks for it —
# see `create_logon_task`.
NO_TIME_LIMIT_S = 0

# Phase anchor for a repeating trigger — the repetition pattern, not this
# boundary, is what schedules the runs. A fixed date rather than "now" so the
# generated definition is byte-identical across re-registrations: converge
# re-registers on every `ava start`, and an anchor of "now" would rewrite the
# timestamp and re-phase the occurrence grid each time, making a real change
# indistinguishable from noise. It does NOT delay the first run — with
# `StartWhenAvailable` the tick the grid says was just missed can run promptly
# after registration, which is what a `/Create /SC MINUTE` (start time = now) did
# too.
# No `Z`/offset suffix — Task Scheduler interprets a bare `<StartBoundary>`
# in the MACHINE'S LOCAL timezone, not UTC (tz audit, 2026-08, PR-6). Harmless
# for this specific value: a MINUTE-repetition trigger's phase only cares
# about the boundary's second-of-minute, which is 0 in every timezone. A task
# anchored on a sub-minute-significant field would need to account for this.
_START_BOUNDARY = "2000-01-01T00:00:00"


def _home_slug() -> str:
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


def task_name(kind: str, slug: str | None = None) -> str:
    """Full Task Scheduler path for one job kind.

    ``\\Ava\\<home-slug>\\<kind>`` — the slug keeps two clusters that share a
    home basename distinct, exactly as it does in the launchd label.

    `slug` names which cluster's task this is; omitted means this process's own
    home. Deletion must pass it explicitly — `ava cluster destroy` removes
    *another* cluster's tasks, and resolving the slug from this process would
    name (and delete) its own.
    """
    return rf"\{TASK_FOLDER}\{slug or _home_slug()}\{kind}"


def _pythonw() -> Path:
    """The venv's windowless interpreter, falling back to ``python.exe``.

    A venv always ships ``pythonw.exe`` on Windows; the fallback only matters for
    an unusual/stripped environment, where a flashing console beats not running.
    """
    from shared.paths import repo_root
    from shared.platform_backend import get_backend

    scripts = repo_root() / ".venv" / get_backend().venv_bin_dir_name()
    pythonw = scripts / "pythonw.exe"
    return pythonw if pythonw.exists() else scripts / "python.exe"


def _task_user() -> str:
    """The account the task runs as: whoever is running this registration.

    Unqualified, which is what Task Scheduler resolves against this machine — the
    same account a bare ``schtasks /Create`` would have picked implicitly. Naming
    it is what lets the definition state its principal at all, and pairing it with
    ``InteractiveToken`` is what keeps registration password-free: the task runs
    only while this user is logged on, which is the identity that owns
    ``$AVA_HOME`` and the supervised sessions.
    """
    return getpass.getuser()


def iso_duration(seconds: int) -> str:
    """`seconds` as the ISO-8601 duration the task schema wants.

    Whole hours and whole minutes are emitted as such (``PT30M``, not ``PT1800S``)
    so the artifact reads the way an exported task does. Zero is the schema's
    "no limit"; a negative bound is a caller bug, not a value to normalise.
    """
    if seconds < 0:
        raise ValueError(f"time limit must not be negative: {seconds}")
    if seconds == NO_TIME_LIMIT_S:
        return "PT0S"
    if seconds % 3600 == 0:
        return f"PT{seconds // 3600}H"
    if seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


def _settings_xml(time_limit_s: int) -> str:
    """The ``<Settings>`` block — every value this repo relies on, stated.

    Element order follows what Task Scheduler itself exports, so the result can be
    diffed directly against ``schtasks /Query /XML /TN <name>``.

    The four settings that carry the reason this block is written by hand at all:

    - ``DisallowStartIfOnBatteries`` / ``StopIfGoingOnBatteries`` **false**. Both
      default to true, which on a laptop runner makes every Ava job conditional on
      the power cable — including the watchdog probe, whose whole purpose is to be
      the supervisor that cannot itself die. An Ava runner supervises a cluster;
      it does that on battery too.
    - ``StartWhenAvailable`` **true**, so a tick missed while the box was asleep or
      suspended runs on resume instead of being dropped. It applies to time-based
      triggers only, so it is inert for the logon task and set there for uniformity.
    - ``AllowHardTerminate`` **true**, without which ``ExecutionTimeLimit`` is
      toothless — the scheduler could not end a wedged invocation, and the bound
      that keeps ``IgnoreNew`` from becoming a permanent block would not hold.

    ``WakeToRun`` stays false deliberately: a minute-cadence job that woke a
    sleeping laptop every 60s would be hostile, and ``StartWhenAvailable`` already
    covers the resume. ``RunLevel``/``Priority``/``Hidden`` restate the defaults a
    bare ``/Create`` produced, so this is not a silent change in security posture
    or scheduling weight.
    """
    return f"""  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{iso_duration(time_limit_s)}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>"""


def _minute_trigger(minutes: int) -> str:
    """A time trigger repeating every ``minutes`` minutes, indefinitely.

    An omitted ``<Duration>`` is the schema's "repeat forever"; ``schtasks
    /SC MINUTE`` produces the same shape.
    """
    return f"""  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{_START_BOUNDARY}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>"""


def _daily_trigger(hour: int, minute: int) -> str:
    """A daily local-time trigger with a stable calendar boundary."""
    if not 0 <= hour <= 23:
        raise ValueError(f"daily task hour must be between 0 and 23: {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"daily task minute must be between 0 and 59: {minute}")
    boundary = f"2000-01-01T{hour:02d}:{minute:02d}:00"
    return f"""  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{boundary}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>"""


def _logon_trigger() -> str:
    """A logon trigger scoped to this user — the ``/SC ONLOGON`` shape."""
    return f"""  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(_task_user())}</UserId>
    </LogonTrigger>
  </Triggers>"""


def task_xml(*, description: str, trigger: str, args: Sequence[str], time_limit_s: int) -> str:
    """One complete task definition: registration info, trigger, principal, settings, action.

    ``<Command>`` and ``<Arguments>`` are separate elements, so the interpreter
    path is *not* quoted here — Task Scheduler treats ``<Command>`` as one path
    however many spaces it contains (``C:\\Users\\First Last\\...``), and manual
    quotes would become part of it.
    """
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns={quoteattr(_TASK_NS)}>
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
{trigger}
  <Principals>
    <Principal id="Author">
      <UserId>{escape(_task_user())}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
{_settings_xml(time_limit_s)}
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(_pythonw()))}</Command>
      <Arguments>{escape(" ".join(["-m", "cli.main", *args]))}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _xml_path(kind: str) -> Path:
    """Where this cluster's task definition is written.

    Under ``$AVA_HOME`` rather than a temp dir: the file is the registered
    configuration, so keeping it beside the cluster that owns it makes what was
    asked for inspectable after the fact, and it is removed with the home when
    the cluster is destroyed. Re-registration overwrites it in place.
    """
    from shared.paths import run_dir

    target = run_dir() / "schtasks"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{kind}.xml"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _register(kind: str, xml: str, what: str) -> str | None:
    """Write ``kind``'s definition and hand it to ``schtasks /Create /XML``.

    ``/F`` makes this idempotent — re-running replaces the existing task, which is
    how an interval or a settings change takes effect (and is why a manual fix
    applied in the Task Scheduler UI is wiped by the next converge).

    One retry on failure: the observed `schtasks /Create` failures on the win
    box have been transient races (a concurrent updater tearing the definition
    XML mid-write, a ghost-task instance starting at the same moment — win
    2026-08-11, task #1196), and a single retry — re-writing the XML so a torn
    file heals, then `/Create` again — clears them. A second failure is a real
    registration problem and is returned to the caller, which decides how loud
    to be about it.

    **The return carries `schtasks`' own words, not just "it failed".** This used
    to answer with an rc and put the reason in a `logger.error`, which is a sink
    that may not exist in the process doing the registering: a converge running
    under the updater chain has stderr wired to the updater log but no loguru sink
    configured, so on the fleet's Windows box the reason has never once reached
    disk — nine months of `watchdog-probe registration failed on Windows` with
    nothing after it. The caller prints its warning to stderr already; handing it
    the reason is what makes the next occurrence diagnosable without a debugger on
    the box. None means registered.

    UTF-16 with a BOM, matching what Task Scheduler exports: ``schtasks`` is picky
    about the encoding of a definition it is asked to read, and the exported
    encoding is the one path known to be accepted.
    """
    name = task_name(kind)
    path = _xml_path(kind)
    for attempt in (1, 2):
        path.write_text(xml, encoding="utf-16")
        result = _run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"])
        if result.returncode == 0:
            logger.info("scheduled task '{}' registered ({})", name, what)
            return None
        if attempt == 1:
            # Transient class (concurrent write / running-instance race) — one
            # retry is the fix; a second failure is not this class.
            logger.warning(
                "schtasks /Create failed for {} (retrying once): {}",
                name,
                result.stderr.strip(),
            )
            time.sleep(_REGISTER_RETRY_DELAY_S)
    reason = result.stderr.strip() or f"schtasks exited {result.returncode} with no message"
    logger.error("schtasks /Create failed for {}: {}", name, reason)
    return reason


def _description(kind: str) -> str:
    """What an operator sees in the Task Scheduler UI.

    Names the cluster that owns the task, and says that hand-edits do not
    survive — converge re-registers with ``/F`` on every ``ava start``, so the UI
    is a place to read this configuration, never to change it. Plain ASCII: this
    string is the only free text in a definition handed to a Windows binary.
    """
    return (
        f"Ava {kind} for cluster {_home_slug()} - registered by `ava converge`; "
        "manual edits are replaced on the next `ava start`."
    )


def create_minute_task(
    kind: str, args: Sequence[str], minutes: int, *, time_limit_s: int
) -> str | None:
    """Register (or replace) a task that repeats every ``minutes`` minutes.

    The scheduler's valid range is 1-999 minutes, so a caller asking for less than
    a minute is clamped to 1 rather than emitting an interval Task Scheduler would
    reject.

    ``time_limit_s`` bounds one invocation and every caller must choose it, because
    the right value follows from what the command does — the callers state their
    reasoning next to their own interval constants. Two properties make the choice
    matter more than it looks:

    - The instance policy is ``IgnoreNew``, which is right for a cadence job (an
      invocation that overruns its own interval must not accumulate overlapping
      copies, and for the health probe it is what keeps two auto-rollbacks from
      running at once). It does mean the time limit is the *only* thing preventing
      one wedged invocation from blocking every later one — with the scheduler's
      72h default, three days of no supervision.
    - The bound must clear the slowest *legitimate* run with real headroom, or the
      limit starts killing work that was about to succeed.

    Creating a per-user task needs no password (verified on the reference box);
    ``InteractiveToken`` in the principal is what keeps it that way, and the result
    runs as the current user with ``Logon Mode: Interactive only``.
    """
    every = max(1, min(999, minutes))
    xml = task_xml(
        description=_description(kind),
        trigger=_minute_trigger(every),
        args=args,
        time_limit_s=time_limit_s,
    )
    return _register(kind, xml, f"every {every} min")


def create_daily_task(
    kind: str,
    args: Sequence[str],
    *,
    hour: int,
    minute: int,
    time_limit_s: int,
) -> str | None:
    """Register (or replace) a task that runs daily at one local wall-clock time."""
    xml = task_xml(
        description=_description(kind),
        trigger=_daily_trigger(hour, minute),
        args=args,
        time_limit_s=time_limit_s,
    )
    return _register(kind, xml, f"daily at {hour:02d}:{minute:02d}")


def create_logon_task(kind: str, args: Sequence[str], *, time_limit_s: int) -> str | None:
    """Register (or replace) a task that runs once at user logon.

    A logon trigger is the user-session analog of launchd ``RunAtLoad`` and the
    Linux ``@reboot`` crontab line: it fires when the user logs on, not when the
    machine powers up. ``ONSTART`` would fire earlier but runs as SYSTEM, which is
    the wrong identity — ``$AVA_HOME`` and the supervised sessions belong to the
    user. The consequence is a real operational limit and is stated as one in
    ``conventions/windows-setup.md``: a reboot with no interactive logon does
    not bring Ava back on that host.

    ``time_limit_s`` is a caller's choice here too, but note ``NO_TIME_LIMIT_S`` is
    the answer for the boot job: ``cli/boot_retry.py`` retries ``ava start`` with
    no attempt cap on purpose (``shared/boot_policy.py`` — "the three platforms
    must agree, or a box whose VPN is down for 45 minutes recovers on one and
    stays down forever on the others"), and neither launchd nor cron imposes a
    runtime bound on their equivalent. A finite limit here would be a Windows-only
    attempt cap, applied by the scheduler, to the one job nothing else recovers.
    """
    xml = task_xml(
        description=_description(kind),
        trigger=_logon_trigger(),
        args=args,
        time_limit_s=time_limit_s,
    )
    return _register(kind, xml, "at logon")


def delete_task(kind: str, slug: str) -> int:
    """Remove one cluster's task. Absent is success — this mirrors the
    launchd/crontab unregister paths, which are all no-ops when nothing is
    registered.

    `slug` is required rather than defaulted: this is the path `ava cluster
    destroy` drives against a *different* home, and a silent fallback to this
    process's own slug is exactly the class of bug that made it deregister prod.
    """
    name = task_name(kind, slug)
    result = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    if result.returncode != 0:
        # The only expected failure is "task does not exist"; anything else is
        # still not worth failing a teardown over, but say so rather than
        # claiming a deletion that did not happen.
        logger.debug(
            "schtasks /Delete for {} returned {}: {}",
            name,
            result.returncode,
            result.stderr.strip(),
        )
        return 0
    logger.info("scheduled task '{}' removed", name)
    return 0


def _legacy_slug() -> str:
    """This home's pre-hash slug form: the bare basename home_slug used before
    the 8-hex suffix existed. A home whose slug was re-derived keeps its old
    tasks registered under this older folder — the ghost-task class that made
    the win host's converge race itself (task #1196, `\\Ava\\ava\\` next to
    the current `\\Ava\\ava-<hash>\\`).
    """
    return _home_slug().rsplit("-", 1)[0]


def _list_tasks() -> list[str]:
    """Every scheduled task under the ``\\Ava\\`` folder, full task names.

    CSV + no-header, filtered locally rather than scoping the query with
    ``/TN \\Ava\\``: the folder-query recursion is not worth betting a cleanup
    on, and the unfiltered shape is the one the suite's leak guard already
    parses. Returns [] on any query failure (logged) — reap is best-effort.
    """
    result = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if result.returncode != 0:
        logger.warning(
            "schtasks /Query failed (rc={}): {} — skipping stale-task reap",
            result.returncode,
            result.stderr.strip(),
        )
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        name = line.split(",", 1)[0].strip('"')
        if name.startswith("\\Ava\\"):
            names.append(name)
    return names


def reap_stale_tasks() -> int:
    """Delete every scheduled task under ``\\Ava\\`` that no live cluster can
    own, returning how many were deleted.

    A cluster's task lives at ``\\Ava\\<home-slug>\\<kind>``, and registration
    (`/Create /F`) is idempotent only for the CURRENT slug — when a home's slug
    changes, the old tasks keep firing under the old folder, each racing the
    new ones' registration on every converge (win 2026-08-11, task #1196: the
    ``\\Ava\\ava\\`` ghost fired a second watchdog probe every minute and the
    concurrent `/Create` failed intermittently, holding the host offline for
    ~45 minutes). This is the schtasks analog of
    ``shared.os_cron.cleanup_legacy_macos_job``.

    Only tasks a live cluster cannot own are touched:

    - tasks under THIS home's own legacy slug folder (the bare-basename form
      home_slug produced before the 8-hex suffix — ``ava`` for ``~/.ava``);
    - tasks under any folder that does not match the current slug shape
      `<basename>-<8-hex>` — no live cluster registers there, so it is a
      leftover of an older naming or of a deleted cluster (tasks at the root
      of ``\\Ava\\`` with no slug folder at all are reclaimed the same way);

    A task under any OTHER current-shape slug folder is left alone — that is a
    co-located cluster's live namespace, and the slug exists precisely to keep
    those apart. The ``\\Ava\\`` folder is this repo's declared namespace
    (``TASK_FOLDER``), so everything in it that is not a current slug is ours
    to reclaim.

    Never raises: reap is best-effort cleanup — a query or delete failure must
    not fail the very converge that (re-)arms the current tasks, and the
    register steps that follow remain the supervision-critical ones.
    """
    try:
        names = _list_tasks()
    except OSError:
        logger.warning("schtasks unavailable — skipping stale-task reap")
        return 0
    current = _home_slug()
    legacy = _legacy_slug()
    deleted = 0
    for name in names:
        parts = name[len("\\Ava\\") :].split("\\")
        folder = "\\".join(parts[:-1]) if len(parts) >= 2 else ""
        if folder == current:
            continue
        if folder != legacy and _CURRENT_SLUG_RE.match(folder):
            # Another live cluster's namespace — keep.
            continue
        result = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        if result.returncode != 0:
            logger.warning(
                "schtasks /Delete failed for stale task {}: {}",
                name,
                result.stderr.strip(),
            )
            continue
        logger.info("removed stale scheduled task '{}'", name)
        deleted += 1
    return deleted
