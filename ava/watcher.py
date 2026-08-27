"""Background watchers that wake you when something happens, so you do not
stay in-turn polling."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import logging
import pathlib as _pl
import re as _re
import tempfile
from typing import Any

from ava.shell import _background
from ava.shell import sessions as _sessions
from shared.watcher import (
    TEMPLATE_VERSION,
    build_at_script,
    build_cron_script,
    normalize_end_time,
    normalize_when,
    validate_cron,
    validate_timezone,
)
from shared.watcher import (
    CronExprError as CronExprError,
)

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
    from shared.paths import ava_home

    home = ava_home()
    slug = home.name.lstrip(".") or "cluster"
    # sha1 is fine here: the digest is a directory-name uniquifier, not a
    # security boundary (S324).
    digest = hashlib.sha1(str(home).encode()).hexdigest()[:8]  # noqa: S324
    p = _pl.Path(tempfile.gettempdir()) / "ava" / f"{slug}-{digest}" / str(_agent_id()) / "watchers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _agent_id() -> int:
    import ava._boot

    return int(ava._boot.agent_id())


_SCRIPT_FILE_RE = _re.compile(r"^watcher_(\d+)\.py$")
_BOOT_FILE_RE = _re.compile(r"^watcher_(\d+)_boot\.py$")


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
        alive = None  # conservative: no session info, no pruning
    d = keep.parent
    for pat, exclude in ((_SCRIPT_FILE_RE, _BOOT_FILE_RE), (_BOOT_FILE_RE, _SCRIPT_FILE_RE)):
        for f in d.iterdir():
            if f == keep or not pat.match(f.name):
                continue
            if exclude.match(f.name):
                continue
            if alive is not None:
                m = pat.match(f.name)
                if m and int(m.group(1)) in alive:
                    continue  # that watcher's session still exists — possibly still launching
            f.unlink(missing_ok=True)


_DURATION_RE = _re.compile(r"^(\d+)([smhd])$")


def _parse_timeout(timeout: float | datetime.timedelta | str) -> float:
    """Coerce a timeout to a positive number of seconds.

    Accepts a number of seconds, a `timedelta`, or a `"<n>{s,m,h,d}"` duration
    string (e.g. `"30m"`, `"2h"`).
    """
    if isinstance(timeout, datetime.timedelta):
        secs = timeout.total_seconds()
    elif isinstance(timeout, bool):  # bool is an int subclass — reject explicitly
        raise TypeError("timeout must be seconds, a timedelta, or a duration string")
    elif isinstance(timeout, (int, float)):
        secs = float(timeout)
    elif isinstance(timeout, str):
        m = _DURATION_RE.match(timeout)
        if not m:
            raise ValueError(
                f"timeout={timeout!r} not recognized — use '<n>s/m/h/d' (e.g. '30m', '2h'), "
                "a number of seconds, or a timedelta"
            )
        n, unit = int(m.group(1)), m.group(2)
        secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    else:
        raise TypeError("timeout must be seconds, a timedelta, or a duration string")
    if secs <= 0:
        raise ValueError("timeout must be positive")
    return secs


def _build_boot(script_path: _pl.Path, watchdog_secs: float | None, agent_id: int) -> str:
    """Source of the generated bootstrap file: establish the agent identity,
    arm the optional timeout watchdog, then run the watcher script as
    ``__main__``.

    Agent identity is INLINED as ``AVA_AGENT_ID`` into the bootstrap, because
    the session machinery deliberately does NOT forward it: the env allowlist
    ``shared/env_registry.py`` ``child_env`` (Task #856) drops every
    agent-scope / non-modeled ``AVA_*`` knob from session children. Without
    the inline, a watcher child would see ``ava.self.AGENT_ID=None`` and its
    wake-up ``send_message`` would hit ``/api/agents/None/messages`` → 422 →
    the watcher dies before ever waking its agent (Task #964 — fleet-wide
    silent loss of every scheduled wake-up). The bootstrap is per-agent
    generated code and the id is the agent's own, so nothing sensitive leaks.
    Overwrite, not setdefault: the watcher belongs to the spawning agent, so
    a stale env value (e.g. a parent-shell id accidentally forwarded) must
    not win. ``ava._boot`` then lazily establishes identity from the env var
    on first use (``owns_loop=False``).

    Plugin namespaces are loaded explicitly (``ava._ensure_plugins_loaded()``)
    before the script runs: a fresh child's ``import ava`` is the factory module
    with none of the agent process's plugin setattrs, so without this step
    ``ava.tasks`` (and every other plugin-registered namespace) would raise
    ``AttributeError`` inside the watcher.

    The ``ava`` module is still imported and passed via ``init_globals`` to
    ``runpy.run_path`` so that watcher code can use ``ava`` without an
    explicit import — this is the public contract of ``launch()``.

    runpy is wrapped in try/finally — no except anywhere: an exception still
    propagates, Python prints the traceback to stderr (redirected into the
    watcher's log file) and exits non-zero, and the shell-level completion
    notice reports that exit code and carries the tail of the log. A
    ``SystemExit(n)`` likewise becomes exit code n. The finally block only
    deletes the generated script + bootstrap files: a watcher reads them
    exactly once at launch, so removing them on exit keeps the watchers dir
    empty instead of accumulating a script graveyard (the old global
    $AVA_HOME/watchers dir grew past 180 files). The watchdog is a daemon
    timer that prints its reason (into the log) and hard-exits with code 124
    (the ``timeout(1)`` convention), so a script stuck in a Python-level loop
    still dies on time — the one thing it cannot preempt is native code that
    never releases the GIL; a killed watcher skips the finally and leaves its
    pair behind, which the next launch prunes (_prune_stale_watcher_files).

    Every bootstrap also arms the ORPHAN GUARD (task #1726): a daemon thread
    that compares ``os.getppid()`` against the parent the process booted
    under every few seconds and hard-exits with code 125 on a mismatch. The
    pty host IS the session; when it dies (crash, SIGKILL, a reaper sweep),
    the login shell dies with it and the watcher child is reparented to init
    — still alive, still firing cron/at. The guard makes host death → child
    death within a few seconds, on every host-death path (a kill-path
    cascade cannot cover a crash or an external SIGKILL).
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
        # (shared/env_registry.py child_env, Task #856) drops
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
        # (agent/db.py reads settings.agent at module level, and the runner
        # profile does not construct the agent domain — Task #856 fail-fast).
        f'os.environ["AVA_PROCESS_PROFILE"] = "agent"\n'
        "\n"
        # Orphan guard (task #1726): a watcher child must never outlive
        # its session. The pty host IS the session; when it dies (crash,
        # SIGKILL, a reaper sweep), the login shell dies with it and this
        # process is reparented to init — still alive, still firing
        # cron/at (2026-08-26: 49 of 85 watcher processes on the fleet
        # host were multi-generation orphans of exactly this shape).
        # Every few seconds, compare getppid() against the parent we
        # booted under: a mismatch means the session chain is gone, and
        # the watcher hard-exits instead of firing forever. This covers
        # every host-death path — a kill-path cascade cannot (crash,
        # external SIGKILL, ad-hoc sweeps).
        "_parent_pid = os.getppid()\n"
        "\n"
        "def _orphan_guard() -> None:\n"
        "    while True:\n"
        "        time.sleep(5)\n"
        "        if os.getppid() != _parent_pid:\n"
        "            print(\n"
        "                '[watcher] session gone (pty host died) — exiting (orphan guard)',\n"
        "                file=sys.stderr,\n"
        "                flush=True,\n"
        "            )\n"
        "            os._exit(125)\n"
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
        "ava._ensure_plugins_loaded()\n"
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
        "    # R1 (Task #1021): a CLEAN exit ends this watcher for good, so its\n"
        "    # registry row goes too — a surviving row with a missing session is\n"
        "    # exactly what the boot reconcile reads as 'killed, should exist'.\n"
        "    # A killed watcher skips this finally and keeps its row. Fail-soft:\n"
        "    # a registry blip must not turn a clean exit into a crash.\n"
        "    try:\n"
        "        from shared.watcher_registry import delete_watcher\n"
        "        delete_watcher(\n"
        "            int(os.environ['AVA_AGENT_ID']),\n"
        "            int(os.environ['AVA_WATCHER_SESSION_ID']),\n"
        "        )\n"
        "    except Exception:\n"
        "        # Not bare: a failed delete leaves a stale row the boot\n"
        "        # reconcile then misreads (task #1858) — make it visible.\n"
        "        import logging\n"
        "        logging.getLogger('watcher').warning(\n"
        "            'watcher registry row delete failed on clean exit',\n"
        "            exc_info=True,\n"
        "        )\n"
    )


def _spawn(
    code: str,
    watchdog_secs: float | None,
    name: str,
    *,
    kind: str,
    message: str | None = None,
    fires_at: Any = None,
    cron_expr: str | None = None,
    cron_timezone: str | None = None,
    cron_end_at: Any = None,
    timeout_secs: float | None = None,
    _exclude_session: int | None = None,
) -> int:
    """Start a watcher child running ``code``; return its watcher id.

    The session runs two files: the agent's script (written verbatim) and a
    generated bootstrap that inlines the agent identity, arms the optional
    watchdog and runs the script via runpy — so the command line typed into
    the session stays short and readable. Identity is inlined because the
    session env allowlist does not forward ``AVA_AGENT_ID`` (Task #856 /
    #964; see ``_build_boot``). Output is
    redirected to a per-agent log file and a completion notice (exit code +
    log path + output tail) is delivered from the shell level when the child
    exits, on every exit path — a crashed or hard-killed child cannot skip
    it. The session closes itself after the notice is delivered (the log file
    preserves the output); a notice that fails to send leaves the session open
    as the post-mortem site.
    """
    import shlex
    import sys

    agent_id = _agent_id()
    session_id, _name = _sessions._create_session(name)
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
    output_path = _background.allocate_output_path(session_id, name)
    cmd = (
        f"{_SESSION_ID_ENV}={session_id} "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(boot_path))}"
    )
    line = _background.notified_line(
        cmd,
        agent_id=agent_id,
        label=f"Watcher '{name}'",
        source=f"watcher:{session_id}",
        output_path=output_path,
        keep=False,
    )
    # R1 (Task #1021): the watcher registry — this row is what the agent's boot
    # reconcile reads to rebuild a watcher whose session a stop/rollout reaped
    # (#1014). The row is written BEFORE the child starts, never after: a child
    # that exits before its row lands would leave the row inserted after its
    # clean-exit cleanup already ran — a permanent ghost "running" row the boot
    # reconcile would read as "killed, should exist". Registering first closes
    # that race, and a registry failure now FAILS the spawn instead of being
    # swallowed (the old fail-soft left a live watcher the boot reconcile can
    # never rebuild — the registry is the only record of "should exist").
    try:
        if kind == "cron":
            # Task #1825 dedupe, atomic (N2): one transaction — xact lock,
            # re-check, insert (shared.watcher_registry.register_cron_atomic).
            # The re-check needs a FRESH session list (a just-registered
            # winner must be visible), fetched INSIDE the lock via the
            # provider — a snapshot taken here would have a window where a
            # concurrent winner's session does not yet exist (QA nit,
            # #794 delta2).
            from shared.watcher_registry import register_cron_atomic

            def _fresh_alive() -> set[int] | None:
                try:
                    return set(_sessions.list())
                except Exception:
                    # Session list unavailable — cannot verify liveness, so
                    # no dedupe (spawning is the fail-safe: a duplicate is
                    # recoverable, a reused dead session would silently lose
                    # the schedule).
                    return None

            # kind == 'cron' guarantees the schedule payload (cron() always
            # passes it); narrow for register_cron_atomic's str contract.
            from typing import cast

            reused = register_cron_atomic(
                agent_id,
                session_id,
                name=name,
                message=message,
                cron_expr=cast(str, cron_expr),
                cron_timezone=cast(str, cron_timezone),
                cron_end_at=cron_end_at,
                alive_provider=_fresh_alive,
                exclude_session=_exclude_session,
                template_version=TEMPLATE_VERSION,
            )
            if reused is not None:
                # A concurrent registration won the race — the schedule is
                # already live under `reused`. Dispose the session we created
                # and hand the caller the winner's id.
                logger.info(
                    "[watcher] cron %r already live as session %s — reusing (dedupe)",
                    cron_expr,
                    reused,
                )
                with contextlib.suppress(Exception):
                    _sessions.kill(session_id)
                return reused
        else:
            from shared.watcher_registry import register_watcher

            register_watcher(
                agent_id,
                session_id,
                kind=kind,
                name=name,
                message=message,
                fires_at=fires_at,
                cron_expr=cron_expr,
                cron_timezone=cron_timezone,
                cron_end_at=cron_end_at,
                timeout_secs=timeout_secs,
                template_version=TEMPLATE_VERSION,
            )
    except Exception:
        logger.error(
            "[watcher] registry write failed for session %s — refusing to start "
            "the watcher (the boot reconcile could never rebuild it)",
            session_id,
            exc_info=True,
        )
        # Dispose the session we created but will not start; kill() also drops
        # any registry row, so this is safe on every failure path.
        with contextlib.suppress(Exception):
            _sessions.kill(session_id)
        raise
    try:
        _sessions.send(session_id, line)
    except Exception:
        # Compensating delete: a registered row whose child never started would
        # be read by the next boot reconcile as "killed, should exist" and
        # rebuilt forever. The row only exists because the spawn failed after
        # registration, so it must not survive the failed spawn.
        logger.error(
            "[watcher] failed to start session %s after registering it — dropping the registry row",
            session_id,
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            from shared.watcher_registry import delete_watcher

            delete_watcher(agent_id, session_id)
        with contextlib.suppress(Exception):
            _sessions.kill(session_id)
        raise
    return session_id


def launch(code: str, timeout: float | datetime.timedelta | str, *, name: str) -> int:
    """Run `code` as a background watcher, bounded by `timeout`.

    `code` calls `ava.agents.send_message(ava.self.AGENT_ID, content)`
    whenever it wants to wake you. The watcher runs until it exits, you kill
    its session, or `timeout` elapses; when it stops you get a message with
    its exit code and a pointer to its full output.

    Args:
        timeout: seconds, a `timedelta`, or a `"<n>{s,m,h,d}"` duration
            string (e.g. `"30m"`).
        name: a lowercase slug like `"ci-monitor"`.

    Returns:
        The watcher's session id — while it runs, the watcher is one of your
        shell sessions, managed like any other.
    """
    return _spawn(
        code,
        _parse_timeout(timeout),
        name,
        kind="launch",
        timeout_secs=_parse_timeout(timeout),
    )


def cron(
    expr: str,
    message: str,
    *,
    timezone: str | None = None,
    end_time: datetime.datetime | datetime.timedelta | str | None = None,
    name: str,
    _exclude_session: int | None = None,
) -> int:
    """Wake yourself with `message` on a recurring schedule. Runs until
    `end_time`, or until you kill its session.

    A schedule already live under the same (agent, expression, timezone,
    end time) is REUSED instead of registered twice: two identical-schedule
    crons fire the same wake-ups, and a kill/rebuild cycle that stacked them
    made both fire concurrently (Task #1825). The returned session id is the
    existing watcher's when reused; kill it to stop the schedule.

    Args:
        expr: 5-field cron expression (`minute hour day-of-month month
            day-of-week`).
        timezone: IANA name (e.g. `"America/Los_Angeles"`); defaults to your
            configured timezone — the same wall clock your message timestamps
            are shown in.
        end_time: a TZ-aware datetime, a timedelta from now (UTC), or an
            ISO-8601 string with timezone.
        name: a lowercase slug like `"daily-check-in"`.

    Returns:
        The watcher's session id; kill that session to stop the schedule.
    """
    from shared.config import cluster_tz_name, host_tz_name

    _validate_message(message)
    validate_cron(expr)
    # Default to the cluster clock when authoritative; a settings-lite
    # process (no authoritative cluster timezone) falls back to this host's
    # own zone — the same wall clock its other displays use.
    tz = timezone if timezone is not None else (cluster_tz_name() or host_tz_name())
    validate_timezone(tz)
    et = normalize_end_time(end_time)
    code = build_cron_script(
        expr=expr,
        message=message,
        timezone=tz,
        end_time_iso=et.isoformat() if et is not None else None,
    )
    # The generated script self-terminates (it stops looping past end_time, or
    # recurs indefinitely by design for a standing reminder), so no watchdog.
    # Registration carries the Task #1825 dedupe — atomically: one transaction
    # (pg_advisory_xact_lock + re-check + insert, shared.watcher_registry.
    # register_cron_atomic), so a concurrent registration of the same schedule
    # can never slip between the check and the insert (N2). `_exclude_session`
    # lets the stale-template rebuild skip the very session it is replacing.
    return _spawn(
        code,
        None,
        name,
        kind="cron",
        message=message,
        cron_expr=expr,
        cron_timezone=tz,
        cron_end_at=et,
        _exclude_session=_exclude_session,
    )


def at(
    when: datetime.datetime | datetime.timedelta | str,
    message: str,
    *,
    name: str,
) -> int:
    """Wake yourself once with `message` at `when`.

    Args:
        when: a TZ-aware datetime, a timedelta from now (UTC), or an ISO-8601
            string with timezone. Must be in the future.
        name: a lowercase slug like `"stand-up-reminder"`.

    Returns:
        The watcher's session id; kill that session to cancel.
    """
    from shared.config import cluster_tz_name

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
        timezone=cluster_tz_name(),
    )
    # The one-shot script sleeps until `when`, wakes you once, and exits — it
    # ends itself, so no watchdog.
    return _spawn(code, None, name, kind="at", message=message, fires_at=due_at)


logger = logging.getLogger(__name__)


def _kill_watcher_orphan_processes(session_id: int) -> None:
    """SIGKILL any live process still running this watcher's generated script.

    A watcher whose session is gone but whose process is alive is an orphan:
    its pty host died (crash / SIGKILL / a reaper sweep) and the child was
    reparented to init — still alive, still firing cron/at (task #1726:
    49 of 85 watcher processes on the fleet host were multi-generation
    orphans of exactly this shape). The boot reconcile rebuilds the schedule
    from the registry; without this kill, each rebuild stacks a NEW
    generation on the orphan and both fire. The generated boot script path is
    unique per (agent, session), so matching it in the process's argv is
    precise; SIGKILL matches the pty reaper's convention for detached orphans
    (shared/pty_sessions/orphan_reaper.py) and skips the child's finally, so
    its registry row survives for the transition below. Fail-soft: a scan
    error must not block the boot reconcile it serves.
    """
    import psutil

    boot_path = str(_watchers_dir() / f"watcher_{session_id}_boot.py")
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if boot_path in (proc.info.get("cmdline") or []):
                proc.kill()
        except (psutil.Error, OSError):
            continue


def _live_cron_session(
    agent_id: int,
    *,
    cron_expr: str,
    cron_timezone: str,
    cron_end_at: datetime.datetime | None,
    alive: set[int] | None,
    exclude_session: int | None = None,
) -> int | None:
    """Return the session id of an existing LIVE cron watcher with the same
    schedule (Task #1825), or None.

    Two watchers with the same (agent, kind, schedule) — expression,
    timezone, end time — are duplicates: they fire the same wake-ups, and a
    kill/rebuild cycle that stacked them (a killed cron resurrected as a new
    session while the old registration survived) made both fire concurrently
    (observed twice: #2811's triple instance, CEO #228's escalation-check-6h
    sessions 52+53). The reconcile and cron() dedupe through this: a live
    duplicate is reused instead of stacking a new generation.

    "Live" means the registry row is still `running` AND its session is in
    the caller's session list. A row whose session is gone is exactly what
    the reconcile is rebuilding — it is not a duplicate to reuse. `alive=None`
    (session list unavailable) deliberately finds nothing: returning a session
    that might be dead would silently lose the schedule, and a duplicate is
    recoverable while a lost wake-up is not. `exclude_session` skips one live
    row — the stale-template rebuild must not dedupe against the very session
    it is replacing.

    Fail-soft: a registry read failure logs and yields no duplicate — the
    dedupe is advisory and must never block a registration.
    """
    from shared.watcher_registry import watcher_rows

    if alive is None:
        return None
    try:
        rows = watcher_rows(agent_id)
    except Exception:
        logger.warning(
            "watcher dedupe: registry read failed for agent %s — proceeding without dedupe",
            agent_id,
            exc_info=True,
        )
        return None
    for row in rows:
        if row["kind"] != "cron" or row["status"] != "running":
            continue
        if row["session_id"] == exclude_session:
            continue
        if row["session_id"] not in alive:
            continue
        if (
            row["cron_expr"] == cron_expr
            and row["cron_timezone"] == cron_timezone
            and row["cron_end_at"] == cron_end_at
        ):
            return row["session_id"]
    return None


def _reconcile_missing(row: dict[str, Any], now: datetime.datetime, alive: set[int]) -> str | None:
    """Handle one registry row whose session is gone: rebuild a standing
    schedule / future one-shot, mark a passed one-shot or launch watcher missed,
    drop an ended schedule. Returns the action sentence (None when nothing was
    done — an ended cron schedule deletes its row without an action).

    `alive` is the caller's live-session set, used to spot a live duplicate
    before rebuilding a cron (Task #1825)."""
    import contextlib

    from ava import agents as _agents
    from shared.watcher_registry import delete_watcher, mark_status, wake_delivered

    agent_id = _agent_id()
    session_id = row["session_id"]
    name = row["name"]
    try:
        # The session is gone by definition here — any live process still
        # running this watcher's script is an orphan of a dead host, still
        # firing. Kill it before the rebuild/mark below: a rebuild that does
        # not kill the orphan stacks a new generation on it and both fire
        # (task #1726). No-op when no such process exists.
        _kill_watcher_orphan_processes(session_id)
        if row["kind"] == "cron":
            end_at = row["cron_end_at"]
            if end_at is not None and end_at < now:
                delete_watcher(agent_id, session_id)
                return f"cron watcher '{name}': schedule ended; row dropped"
            # Dedupe (Task #1825): the schedule may already be live under
            # another session (a duplicate registration survived a
            # kill/restart cycle — #2811, CEO #228). Rebuild would stack a
            # second generation on it and both would fire; reuse it instead
            # and drop this dead duplicate row.
            existing = _live_cron_session(
                agent_id,
                cron_expr=row["cron_expr"],
                cron_timezone=row["cron_timezone"],
                cron_end_at=end_at,
                alive=alive,
                exclude_session=session_id,
            )
            if existing is not None:
                delete_watcher(agent_id, session_id)
                return (
                    f"cron watcher '{name}': duplicate of live session {existing}; dead row dropped"
                )
            new_id = cron(
                row["cron_expr"],
                row["message"] or "",
                timezone=row["cron_timezone"],
                end_time=row["cron_end_at"],
                name=name,
            )
            mark_status(agent_id, session_id, "rebuilt")
            return f"cron watcher '{name}' rebuilt as session {new_id}"
        if row["kind"] == "at":
            if row["fires_at"] is not None and row["fires_at"] > now:
                new_id = at(row["fires_at"], row["message"] or "", name=name)
                mark_status(agent_id, session_id, "rebuilt")
                return f"one-shot watcher '{name}' rebuilt as session {new_id}"
            # Delivery check (task #1858): the child deletes its own row on a
            # clean exit, but that delete can fail or race the reconcile, and
            # a one-shot that already fired and delivered its wake must not be
            # reported as missed (observed 2026-08-27: the wake arrived at
            # 18:30:00 and the same session was alerted "marked missed" two
            # seconds later). A delivered wake means the row is stale, not a
            # lost moment: drop it silently.
            if wake_delivered(agent_id, session_id, row["created_at"]):
                delete_watcher(agent_id, session_id)
                return f"one-shot watcher '{name}' already fired; row dropped"
            mark_status(agent_id, session_id, "missed")
            with contextlib.suppress(Exception):
                _agents.send_message(
                    agent_id,
                    f"[watcher] '{name}' was not running at boot and its "
                    f"moment ({row['fires_at']}) has passed — marked missed.",
                )
            return f"one-shot watcher '{name}' marked missed"
        mark_status(agent_id, session_id, "missed")
        with contextlib.suppress(Exception):
            _agents.send_message(
                agent_id,
                f"[watcher] '{name}' (one-shot launch watcher) was not "
                "running at boot — marked missed.",
            )
        return f"launch watcher '{name}' marked missed"
    except Exception:
        logger.warning(
            "watcher reconcile: failed for session %s (%s)",
            session_id,
            name,
            exc_info=True,
        )
        return None


def _rebuild_stale_cron_watcher(row: dict[str, Any]) -> str | None:
    """Rebuild one live cron watcher whose spawn template version is behind the
    current template; return the action sentence (None when the rebuild failed).

    The generated script is frozen at launch, so a template fix (issue #182)
    never reaches a running session — it keeps the old loop (and the double-fire
    at a stepped boundary, issue #1330) until rebuilt. The replacement is
    spawned first (its own registry row carries the rebuild duty from here),
    then the stale session is killed (dropping its old row — the
    deliberate-kill semantics).
    """
    from ava.shell import sessions as _sessions_mod

    session_id = row["session_id"]
    name = row["name"]
    try:
        new_id = cron(
            row["cron_expr"],
            row["message"] or "",
            timezone=row["cron_timezone"],
            end_time=row["cron_end_at"],
            name=name,
            # The session being replaced is LIVE (this is a template upgrade,
            # not a death recovery) — the dedupe must not reuse it, or the
            # rebuild would kill the only live copy and leave nothing.
            _exclude_session=session_id,
        )
        _sessions_mod.kill(session_id)
        return (
            f"cron watcher '{name}' rebuilt as session {new_id} "
            f"(stale template v{row.get('template_version') or 0} "
            f"-> v{TEMPLATE_VERSION})"
        )
    except Exception:
        logger.warning(
            "watcher reconcile: stale cron watcher '%s' (session %s) rebuild failed",
            name,
            session_id,
            exc_info=True,
        )
        return None


def reconcile() -> list[str]:
    """Rebuild / mark watchers whose sessions died — the #1014 fix (R1-8).

    Called from the agent boot (`agent/loop.py`): every row in this agent's
    watcher registry whose session is gone is either rebuilt or marked:

    - `cron` — re-spawned from the stored expression (the standing schedule is
      the whole point of the registry: a rollout reaped its session and nothing
      else knew it should exist). A schedule whose `end_time` has passed is
      deleted instead — it ended, just not cleanly. The old row is marked
      `rebuilt`; the new session gets its own `running` row.
    - `at` — re-spawned while its moment is still in the future; once the
      moment has passed the wake is lost, so the row is marked `missed` and the
      agent is told (it created the one-shot; it should know it never fired).
      A passed one-shot whose wake WAS delivered (its child fired, but the
      clean-exit row delete failed or raced) is dropped silently instead — the
      moment was not lost, so no alert (task #1858).
    - `launch` — one-shot scripts are not re-run at boot (their work is
      time-bound and probably stale); marked `missed` + alerted.

    A row whose session is alive is left alone — except a live cron watcher
    whose spawn template version is behind the current one, which is rebuilt so
    a template fix reaches sessions that were already running when it landed
    (issue #1330). Fail-soft: a registry or spawn failure is logged and
    skipped, never allowed to block the boot it runs in.

    Returns the action sentences (empty when nothing needed doing).
    """

    from ava.shell import sessions as _sessions_mod
    from shared.watcher_registry import (
        watcher_rows,
    )

    agent_id = _agent_id()
    try:
        rows = watcher_rows(agent_id)
    except Exception:
        logger.warning("watcher reconcile: registry read failed", exc_info=True)
        return []
    if not rows:
        return []
    try:
        alive = set(_sessions_mod.list())
    except Exception:
        logger.warning("watcher reconcile: session list failed", exc_info=True)
        return []

    actions: list[str] = []
    now = datetime.datetime.now(datetime.UTC)
    for row in rows:
        if row["status"] != "running":
            # rebuilt / missed are terminal history: a rebuilt row's live
            # replacement is its own new running row (which carries the
            # rebuild duty from here), and a missed one-shot was already
            # marked + alerted. Re-processing them would re-spawn a duplicate
            # on every boot — observed 2026-08-09: after one rollout each
            # cron came back TWICE (rebuilt rows kept re-rebuilding).
            continue
        session_id = row["session_id"]
        if session_id in alive:
            # Live but stale: a generated watcher script is frozen at launch, so
            # a template fix (issue #182 / #1330) never reaches a session that
            # was already running when it landed — it keeps the old loop until
            # rebuilt. Rebuild live cron watchers whose spawn version is behind
            # the current template: spawn the replacement first (its own row
            # carries the rebuild duty from here), then kill the stale session
            # (which drops its old row — the deliberate-kill semantics).
            if row["kind"] == "cron" and (row.get("template_version") or 0) < TEMPLATE_VERSION:
                action = _rebuild_stale_cron_watcher(row)
                if action:
                    actions.append(action)
            continue
        action = _reconcile_missing(row, now, alive)
        if action:
            actions.append(action)
    return actions
