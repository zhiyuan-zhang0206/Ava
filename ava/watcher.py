"""Background watchers that wake you when something happens."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import logging
import pathlib as _pl
import re as _re
import tempfile
from typing import Any

from ava.sdk_surface.validation import coerce_str
from ava.shell import background
from ava.shell import sessions as _sessions
from base.daemon.schedules.watcher import (
    DEFAULT_STANDING_CRON_MAX_SECONDS,
    build_at_script,
    build_cron_script,
    normalize_when,
    parse_timeout,
    session_deadline,
    validate_cron,
    validate_timezone,
)
from base.daemon.schedules.watcher import (
    CronExprError as CronExprError,
)
from base.host.env.dotenv_boot import watcher_runner_env

__all_for_ava__ = [
    "at",
    "cron",
    "launch",
]

# A watcher runs as a standalone child process, so it does NOT inherit the agent
# process's in-memory state. The generated bootstrap file establishes identity
# (agent id inlined) before running the watcher script, so every SDK call inside
# the script — including ava.agents.send_message — knows who launched it. The
# watcher's own session id rides as an env var so the prebuilt time watchers
# (at / cron) can tag their wake-ups with which watcher fired; it names this
# watcher, not the agent. (Gateway URL / machine auth come from settings;
# cluster env is forwarded onto the session by the session machinery.)
_SESSION_ID_ENV = "AVA_WATCHER_SESSION_ID"
type WatcherTimeout = float | datetime.timedelta | str


def _validate_message(message: str) -> None:
    if not message.strip():
        raise ValueError("message cannot be empty")


def _watchers_dir() -> _pl.Path:
    # Generated watcher scripts live under the system temp dir, scoped per
    # cluster + agent — NOT in $AVA_HOME (the old global `watchers/` dir there
    # accumulated 180+ files and let co-agents overwrite each other's scripts,
    # 2026-08-02) and NOT in the workspace. A watcher reads its script +
    # bootstrap exactly once, at launch (runpy), so the files are ephemeral
    # carriers: temp storage is their natural home, and the OS reclaims
    # whatever a killed watcher leaves behind.
    #
    # The cluster segment matters: session ids are per-agent DB counters, so
    # two co-located clusters (each its own Postgres) allocate the same ids —
    # a tmp path keyed only on agent id would collide across clusters. The
    # cluster's identity IS its home path (AGENTS.md), so the home basename +
    # a short hash of the full path makes the segment unique per cluster.
    #
    # Not a durable index — the session is the source of truth; the file only
    # needs to outlive launch, and the bootstrap self-deletes both files when
    # the watcher exits (see _build_boot), so the dir stays empty except while
    # a watcher is actually running; stale pairs are pruned at the next launch.
    from base.paths import ava_home

    home = ava_home()
    slug = home.name.lstrip(".") or "cluster"
    # sha1 is fine here: the digest is a directory-name uniquifier, not a
    # security boundary (S324).
    digest = hashlib.sha1(str(home).encode()).hexdigest()[:8]  # noqa: S324
    p = _pl.Path(tempfile.gettempdir()) / "ava" / f"{slug}-{digest}" / str(_agent_id()) / "watchers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _agent_id() -> int:
    import ava.agent_identity

    return ava.agent_identity.require_agent_id()


_SCRIPT_FILE_RE = _re.compile(r"^watcher_(\d+)\.py$")
_BOOT_FILE_RE = _re.compile(r"^watcher_(\d+)_boot\.py$")
_LAUNCH_FILE_RE = _re.compile(r"^watcher_(\d+)_launch\.sh$")


def _prune_stale_watcher_files(keep: _pl.Path) -> None:
    """Delete generated watcher files whose watcher SESSION no longer exists,
    except the pair about to be written.

    A watcher reads its script + bootstrap exactly once, at launch — but
    launch is asynchronous: `_spawn` sends the command into a fresh session
    whose shell takes a moment to come up, so the files must stay on disk
    until the child has actually started. Deleting every old pair at every
    launch (the previous behavior) could delete a sibling's not-yet-read
    files when watchers are created back-to-back, making that sibling's
    python start fail with "can't open file ... _boot.py" (observed
    2026-08-08/09: watcher_0_boot.py / watcher_3_boot.py — Bug A, task
    #1116). So prune only what is provably dead: a pair whose session id is
    not in the live session list. A running watcher's pair stays on disk (a
    few KB) and is pruned once its session closes; a hard-killed watcher's
    pair is pruned once its session is gone too. If the session list is
    unavailable, delete nothing — a stale file is harmless, a not-yet-read
    one is fatal. Files that do not match the generated-name patterns are
    left alone.
    """
    from ava.shell import sessions as _sessions

    try:
        alive = set(_sessions.list())
    except Exception:
        logger.warning(
            "[watcher] listing live sessions failed; skipping the prune of stale watcher files",
            exc_info=True,
        )
        return  # conservative: no session info, no pruning
    d = keep.parent
    for pat in (_SCRIPT_FILE_RE, _BOOT_FILE_RE, _LAUNCH_FILE_RE):
        for f in d.iterdir():
            if f == keep or not pat.match(f.name):
                continue
            m = pat.match(f.name)
            if m and int(m.group(1)) in alive:
                continue  # that watcher's session still exists — possibly still launching
            f.unlink(missing_ok=True)


def _build_boot(script_path: _pl.Path, watchdog_secs: float | None, agent_id: int) -> str:
    """Source of the generated bootstrap file: establish the agent identity,
    arm the optional timeout watchdog, then run the watcher script as
    ``__main__``.

    Agent identity is INLINED as ``AVA_AGENT_ID`` into the bootstrap, because
    the session machinery deliberately does NOT forward it: the env allowlist
    ``base/host/env/registry.py`` ``child_env`` (Task #856) drops every
    agent-scope / non-modeled ``AVA_*`` knob from session children. Without
    the inline, a watcher child would see ``ava.self.AGENT_ID=None`` and its
    wake-up ``send_message`` would hit ``/api/agents/None/messages`` → 422 →
    the watcher dies before ever waking its agent (Task #964 — fleet-wide
    silent loss of every scheduled wake-up). The bootstrap is per-agent
    generated code and the id is the agent's own, so nothing sensitive leaks.
    Overwrite, not setdefault: the watcher belongs to the spawning agent, so
    a stale env value (e.g. a parent-shell id accidentally forwarded) must
    not win. ``ava.agent_identity`` then lazily establishes identity from the env var
    on first use (``owns_loop=False``).

    Plugin namespaces are loaded explicitly (``ava.ensure_plugins_loaded()``)
    before the script runs: a fresh child's ``import ava`` is the factory module
    with none of the agent process's plugin setattrs, so without this step
    ``ava.tasks`` (and every other plugin-registered namespace) would raise
    ``AttributeError`` inside the watcher.

    The ``ava`` module is still imported and passed via ``init_globals`` to
    ``runpy.run_path`` so that watcher code can use ``ava`` without an
    explicit import — this is the public contract of ``launch()``.

    runpy is wrapped in try/finally — no except anywhere: an exception still
    propagates, Python prints the traceback to stderr (teed to the watcher's
    log file and session capture) and exits non-zero, and the shell-level
    completion notice reports that exit code and carries the tail of the log. A
    ``SystemExit(n)`` likewise becomes exit code n. The finally block deletes
    the generated script + bootstrap files: a watcher reads them exactly once
    at launch, so removing them on exit keeps the watchers dir empty instead
    of accumulating a script graveyard (the old global $AVA_HOME/watchers dir
    grew past 180 files). The watchdog is a daemon timer that prints its
    reason (into the log) and hard-exits with code 124 (the ``timeout(1)``
    convention), so a script stuck in a Python-level loop still dies on time
    — the one thing it cannot preempt is native code that never releases the
    GIL; a killed watcher skips the finally and leaves its pair behind, which
    the next launch prunes (_prune_stale_watcher_files).

    Every bootstrap also arms the ORPHAN GUARD (task #1726): a daemon thread
    that compares ``os.getppid()`` against the parent the process booted
    under every few seconds and hard-exits with code 125 on a mismatch. The
    login shell IS the session; when it ends without taking the watcher
    child with it (the pty-sessions service crashing, an external SIGKILL of
    the shell), the child is reparented to init — still alive, still firing
    cron/at. The guard makes session end → child death within a few
    seconds, on every such path (a kill-path cascade cannot cover a crash
    or an external SIGKILL). This is the only
    thing standing between a dead session and a watcher that keeps firing
    forever: nothing tracks or restarts watchers
    (docs/decisions/2026-09-27-watchers-are-never-restarted.md), so a watcher
    child that outlived its session would otherwise run unsupervised.
    """
    watchdog = ""
    if watchdog_secs is not None:
        timeout_msg = (
            f"[watcher timed out] this watcher reached its {watchdog_secs:g}s "
            "limit and stopped. Re-launch it if you still need it."
        )
        watchdog = (
            "\n"
            "def _timeout():\n"
            f"    print({timeout_msg!r}, file=sys.stderr, flush=True)\n"
            "    os._exit(124)\n"
            "\n"
            f"_watchdog = threading.Timer({watchdog_secs!r}, _timeout)\n"
            "_watchdog.daemon = True\n"
            "_watchdog.start()\n"
        )
    return (
        "# Auto-generated watcher bootstrap. Do not edit manually.\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "import threading\n"
        "import time\n"
        "\n"
        # Identity is NOT inherited: the session env allowlist
        # (base/host/env/registry.py child_env, Task #856) drops
        # AVA_AGENT_ID from session children, so without this line the child
        # would see ava.self.AGENT_ID=None and its wake-up send_message would
        # 422 on /api/agents/None/messages (Task #964). Inline the spawning
        # agent's id — per-agent generated file, own id, nothing sensitive.
        # The inline must land BEFORE `import ava`: importing ava with a
        # stale AVA_AGENT_ID in the environment (a session backend freezes the
        # env of its first session, so a pane can carry another agent's id)
        # establishes the WRONG identity at import time, and the assignment
        # below would then be too late to move it (2026-08-09: every watcher
        # child on the shared server woke agent 2959 instead of its owner).
        f'os.environ["AVA_AGENT_ID"] = "{agent_id}"\n'
        # Same session-env leak as AVA_AGENT_ID: the child inherits
        # AVA_PROCESS_PROFILE from the creating process's env,
        # but the watcher is an agent subprocess and needs the agent profile
        # to import ava without hitting the per-process config guard
        # (agent/db/__init__.py reads settings.agent at module level, and the runner
        # profile does not construct the agent domain — Task #856 fail-fast).
        f'os.environ["AVA_PROCESS_PROFILE"] = "agent"\n'
        "\n"
        # Orphan guard (task #1726): a watcher child must never outlive
        # its session. The login shell IS the session; when it ends without
        # taking this process with it (service crash, external SIGKILL of
        # the shell), this process is reparented to init — still alive, still firing
        # cron/at (2026-08-26: 49 of 85 watcher processes on the fleet
        # host were multi-generation orphans of exactly this shape).
        # Every few seconds, compare getppid() against the parent we
        # booted under: a mismatch means the session chain is gone, and
        # the watcher hard-exits instead of firing forever. This covers
        # every session-end path — a kill-path cascade cannot (crash,
        # external SIGKILL, ad-hoc sweeps).
        "_parent_pid = os.getppid()\n"
        "\n"
        "def _orphan_guard() -> None:\n"
        "    while True:\n"
        "        time.sleep(5)\n"
        "        if os.getppid() != _parent_pid:\n"
        "            try:\n"
        "                print(\n"
        "                    '[watcher] session gone (pty session ended) — exiting (orphan guard)',\n"
        "                    file=sys.stderr,\n"
        "                    flush=True,\n"
        "                )\n"
        "            finally:\n"
        "                os._exit(125)\n"
        "\n"
        "_orphan_thread = threading.Thread(target=_orphan_guard, daemon=True)\n"
        "_orphan_thread.start()\n"
        "\n"
        "import ava\n"
        "\n"
        f"{watchdog}"
        # Load plugin namespaces (ava.tasks etc.) into this child before the
        # watcher script runs — a fresh process's `import ava` is the factory
        # module with none of the agent process's plugin setattrs. Identity is
        # set explicitly above; this is the symmetric step for plugin
        # namespaces.
        "ava.ensure_plugins_loaded()\n"
        "try:\n"
        f"    runpy.run_path({str(script_path)!r}, run_name='__main__', init_globals={{'ava': ava}})\n"
        "finally:\n"
        "    # Self-cleanup: delete both generated files (read once at launch).\n"
        "    # Only OSError is swallowed — a watcher exception still propagates.\n"
        f"    for _p in (__file__, {str(script_path)!r}):\n"
        "        try:\n"
        "            os.unlink(_p)\n"
        "        except OSError:\n"
        "            pass\n"
    )


def _spawn(
    code: str,
    watchdog_secs: float | None,
    name: str,
    *,
    kind: str,
    fires_at: Any = None,
    cron_end_at: Any = None,
    timeout_secs: float | None = None,
    notify: str | None = None,
) -> int:
    """Start a watcher child running ``code``; return its watcher id.

    A watcher is nothing more than a shell session running a generated
    script — there is no separate registry or desired-state record
    (docs/decisions/2026-09-27-watchers-are-never-restarted.md): list it, capture
    its output, renew its deadline, or kill it exactly like any other session
    via ``ava.shell.sessions``. Re-registering the same schedule (`cron()`
    with the same expression/timezone, say) does not replace anything — it
    simply starts another independent session; nothing dedupes.

    The session sources a generated shell launcher, which runs the agent's
    script (written verbatim) and a generated bootstrap that inlines the agent
    identity, arms the optional
    watchdog and runs the script via runpy — so the command line typed into
    the session stays short and readable. Identity is inlined because the
    session env allowlist does not forward ``AVA_AGENT_ID`` (Task #856 /
    #964; see ``_build_boot``). Output is teed to a per-agent log file and
    session capture, and a completion notice (exit code + log path + output
    tail) is delivered from the shell level when the child exits, on every
    exit path — a crashed or hard-killed child cannot skip it. The session
    closes itself after the notice is delivered (the log file preserves the
    output); a notice that fails to send leaves the session open as the
    post-mortem site.
    """
    import shlex
    import sys

    notify = background.validate_notify(notify)
    agent_id = _agent_id()
    # The session's shell TTL IS this watcher's target deadline (user ruling
    # 2026-09-14, task #3411): launch = created + timeout, cron = cron_end_at,
    # at = fires_at + grace — derived once, here, by
    # `base.daemon.schedules.watcher.session_deadline`. Written as the
    # system-side TRUE value: a 7-day standing cron is a normal watcher,
    # exempt from the 24h user-session cap. `ava.shell.sessions.renew` can
    # move this same deadline later, exactly like any other session's TTL —
    # but only the session's reclamation, never the generated script's own
    # end (the cron's `_END`, the launch watchdog, the at fire moment), and
    # a renewal call is itself capped at 24h, so it can pull a standing
    # cron's reclaim earlier than its declared end.
    now = datetime.datetime.now(datetime.UTC)
    deadline = session_deadline(
        kind,
        created_at=now,
        timeout_secs=timeout_secs,
        fires_at=fires_at,
        cron_end_at=cron_end_at,
    )
    if deadline is None or deadline <= now:
        # Callers hand a future target (cron()/at() validate theirs; launch
        # requires a positive timeout) — a missing or already-passed deadline
        # here is a call-site bug, never a reason to mount a stillborn
        # session.
        raise ValueError(
            f"watcher {kind!r} target deadline is not in the future "
            f"({deadline!r}) — refusing to spawn its session"
        )
    session_id, _session_name = _sessions.create_session(
        name, ttl=(deadline - now).total_seconds(), system=True, env_overrides=watcher_runner_env()
    )
    script_path = _watchers_dir() / f"watcher_{session_id}.py"
    # Prune files from earlier watchers before writing: a watcher reads its
    # script + bootstrap exactly once at launch, so everything already on
    # disk is dead weight (see _prune_stale_watcher_files). This keeps this
    # agent's tmp watchers dir from ever accumulating a graveyard.
    _prune_stale_watcher_files(script_path)
    # Write the agent's program verbatim — nothing prepended, so a leading
    # `from __future__` import (which must be the first statement of a file)
    # stays valid. The bootstrap lives in its own generated file.
    script_path.write_text(code)
    boot_path = _watchers_dir() / f"watcher_{session_id}_boot.py"
    boot_path.write_text(_build_boot(script_path, watchdog_secs, agent_id))
    output_path = background.allocate_output_path(session_id, name)
    cmd = (
        f"{_SESSION_ID_ENV}={session_id} "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(boot_path))}"
    )
    line = background.notified_line(
        cmd,
        agent_id=agent_id,
        label=f"Watcher '{name}'",
        source=f"watcher:{session_id}",
        output_path=output_path,
        keep=False,
        notify=notify,
    )
    # A fresh PTY may still be in canonical mode while the login shell reads
    # its profile. That input buffer can truncate a long line before readline
    # takes over (macOS: observed at 1024 bytes). Keep repeated absolute paths
    # and the notification pipeline on disk, not in the terminal input stream.
    # Source in the SAME shell: PIPESTATUS, session exit and the Python child's
    # orphan-guard parent retain their existing semantics.
    launch_path = script_path.with_name(f"watcher_{session_id}_launch.sh")
    try:
        launch_line = background.write_launch_file(line, launch_path)
        _sessions.send(session_id, launch_line)
    except Exception:
        # A session whose launch command never sent is not a watcher — it is
        # an idle login shell sitting under the watcher's name until its TTL
        # (up to 7 days for a default cron). Kill it now rather than leaking
        # it; there is no registry row to compensate for any more, but the
        # session itself still must not linger.
        with contextlib.suppress(OSError):
            launch_path.unlink(missing_ok=True)
        logger.error(
            "[watcher] failed to start session %s — killing it",
            session_id,
            exc_info=True,
        )
        try:
            _sessions.kill(session_id)
        except Exception:
            logger.warning(
                "[watcher] killing session %s after the failed start also failed; "
                "it stays until its TTL expires",
                session_id,
                exc_info=True,
            )
        raise
    return session_id


def launch(code: str, timeout: WatcherTimeout, *, name: str, notify: str | None = None) -> int:
    """Run `code` as a background watcher, bounded by `timeout`.

    `code` calls `ava.agents.send_message(ava.self.AGENT_ID, content)` to wake you;
    the watcher runs until its exit, your kill, or `timeout`, then reports its exit code and output.

    Args:
        timeout: seconds, a `timedelta`, or `"<n>{s,m,h,d}"` (e.g. `"30m"`).
        name: a lowercase slug like `"ci-monitor"`.
        notify: omit to use the agent policy; `"always"` / `"failure"` override it.
    Returns:
        The watcher's session id — it is one of your shell sessions while running.
    """
    code = coerce_str(code, "code")
    timeout = coerce_str(timeout, "timeout", allow_types=(int, float, datetime.timedelta))
    name = coerce_str(name, "name")
    return _spawn(
        code,
        parse_timeout(timeout),
        name,
        kind="launch",
        timeout_secs=parse_timeout(timeout),
        notify=coerce_str(notify, "notify", allow_none=True),
    )


def cron(
    expr: str,
    message: str,
    *,
    timezone: str | None = None,
    end_time: datetime.datetime | datetime.timedelta | str | None = None,
    name: str,
    notify: str | None = None,
) -> int:
    """Runs until `end_time`, or until you kill its session.

    `end_time` defaults to now + 7 days; pass an explicit one for a longer
    schedule. Calling `cron()` again with the same expression + timezone does
    NOT replace anything — it starts another, independent session; if you
    want to renew or replace a schedule, kill the old session yourself
    (`ava.shell.sessions.kill`) before or after registering the new one.

    Args:
        expr: 5-field cron expression (`minute hour day-of-month month
            day-of-week`).
        timezone: IANA name; defaults to your configured timezone.
        end_time: same accepted types as `at()`'s `when`; must be in the future.
        name: a lowercase slug like `"daily-check-in"`.
        notify: omit to use the agent policy; `"always"` / `"failure"` to override.
    Returns:
        The watcher's session id; kill it to stop the schedule.
    """
    from base.clock import Clock
    from base.config import host_tz_name

    expr = coerce_str(expr, "expr")
    message = coerce_str(message, "message")
    timezone = coerce_str(timezone, "timezone", allow_none=True)
    end_time = coerce_str(
        end_time, "end_time", allow_none=True, allow_types=(datetime.datetime, datetime.timedelta)
    )
    name = coerce_str(name, "name")
    _validate_message(message)
    validate_cron(expr)
    # Default to the cluster clock when authoritative; a settings-lite
    # process (no authoritative cluster timezone) falls back to this host's
    # own zone — the same wall clock its other displays use.
    tz = (
        timezone
        if timezone is not None
        else (Clock.from_settings().authoritative_timezone or host_tz_name())
    )
    validate_timezone(tz)
    if end_time is None:
        # Standing-cron cap (task #2617): no end_time means the DEFAULT
        # window — 7 days, counted from the current minute — not forever. A
        # longer schedule must pass an explicit end_time.
        now = datetime.datetime.now(datetime.UTC)
        et = now.replace(second=0, microsecond=0) + datetime.timedelta(
            seconds=DEFAULT_STANDING_CRON_MAX_SECONDS
        )
    else:
        # normalize_when (not normalize_end_time): this branch excludes None,
        # so the result is a non-optional datetime for the type checker.
        et = normalize_when(end_time)
        # Align with at(): a past end must not register.
        if et < datetime.datetime.now(datetime.UTC):
            raise ValueError(
                f"end_time is in the past: {et.isoformat()}. "
                "Provide a future time, or use a positive timedelta."
            )
    code = build_cron_script(
        expr=expr,
        message=message,
        timezone=tz,
        end_time_iso=et.isoformat(),
    )
    # The generated script self-terminates (it stops looping past end_time —
    # which every registration now carries, the default being now + 7 days),
    # so no watchdog.
    return _spawn(
        code,
        None,
        name,
        kind="cron",
        cron_end_at=et,
        notify=coerce_str(notify, "notify", allow_none=True),
    )


def at(
    when: datetime.datetime | datetime.timedelta | str,
    message: str,
    *,
    name: str,
    notify: str | None = None,
) -> int:
    """
    Args:
        when: a TZ-aware datetime, a timedelta from now (UTC), or an ISO-8601
            string with timezone. Must be in the future.
        name: a lowercase slug like `"stand-up-reminder"`.
        notify: omit to use the agent policy; `"always"` or `"failure"` overrides it.

    Returns:
        The watcher's session id; kill that session to cancel.
    """
    from base.clock import Clock

    when = coerce_str(when, "when", allow_types=(datetime.datetime, datetime.timedelta))
    message = coerce_str(message, "message")
    name = coerce_str(name, "name")
    _validate_message(message)
    due_at = normalize_when(when)
    if due_at < datetime.datetime.now(datetime.UTC):
        raise ValueError(
            f"when is in the past: {due_at.isoformat()}. "
            "Provide a future time, or use a positive timedelta."
        )
    # The announcement's wall clock follows the cluster timezone (user ruling
    # 2026-08-27: one cluster clock); the sleep is UTC-based regardless. A
    # settings-lite process (no authoritative cluster timezone) passes None so
    # the announcement renders in the watcher's own wall clock — the same
    # degradation every other display path uses.
    code = build_at_script(
        when_iso=due_at.isoformat(),
        message=message,
        timezone=Clock.from_settings().authoritative_timezone,
    )
    # The one-shot script sleeps until `when`, wakes you once, and exits — it
    # ends itself, so no watchdog.
    return _spawn(
        code,
        None,
        name,
        kind="at",
        fires_at=due_at,
        notify=coerce_str(notify, "notify", allow_none=True),
    )


logger = logging.getLogger(__name__)
