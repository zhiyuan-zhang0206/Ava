"""Starting one watcher: the generated files, the session, the launch line.

The namespace module `meiva/watcher/__init__.py` builds a `SpawnRequest` (see `plans.py`) and a
`SpawnDeps` (see `wiring.py`) and calls `run`. Everything `run` touches that is not its request
arrives in the deps - the agent id, the directory for generated files, the session and
background-command ports, the runner env, the interpreter and the clock - so a test hands it
fakes and reads back what they received.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import shlex
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from base.daemon.schedules.watcher import session_deadline
from meiva.watcher import files

__all__ = ["SESSION_ID_ENV", "SpawnDeps", "SpawnRequest", "build_boot", "run"]

logger = logging.getLogger(__name__)

# A watcher runs as a standalone child process, so it does NOT inherit the agent
# process's in-memory state. The generated bootstrap file establishes identity
# (agent id inlined) before running the watcher script, so every SDK call inside
# the script — including meiva.agents.send_message — knows who launched it. The
# watcher's own session id rides as an env var so the prebuilt time watchers
# (at / cron) can tag their wake-ups with which watcher fired; it names this
# watcher, not the agent. (Gateway URL / machine auth come from settings;
# cluster env is forwarded onto the session by the session machinery.)
SESSION_ID_ENV = "MEIVA_WATCHER_SESSION_ID"


@dataclass(frozen=True)
class SpawnRequest:
    """What one watcher is: its program, how it ends and how it is reported."""

    code: str
    watchdog_secs: float | None
    name: str
    kind: str
    fires_at: Any = None
    cron_end_at: Any = None
    timeout_secs: float | None = None
    notify: str | None = None


class SessionsPort(Protocol):
    """The shell-session calls a spawn makes."""

    def create_session(
        self, name: str, *, ttl: float, system: bool, env_overrides: dict[str, str]
    ) -> tuple[int, str]: ...

    def send(self, session_id: int, line: str, /) -> None: ...

    def kill(self, session_id: int, /) -> None: ...


class BackgroundPort(Protocol):
    """The background-command helpers a spawn uses to build its launch line."""

    def validate_notify(self, notify: str | None) -> str | None: ...

    def allocate_output_path(self, session_id: int, name: str) -> Path: ...

    def notified_line(
        self,
        cmd: str,
        *,
        agent_id: int,
        label: str,
        source: str,
        output_path: Path,
        keep: bool,
        notify: str | None,
    ) -> str: ...

    def write_launch_file(self, line: str, path: Path) -> str: ...


@dataclass(frozen=True)
class SpawnDeps:
    """The dependencies of `run`, nothing else."""

    agent_id: int
    watchers_dir: Path
    sessions: SessionsPort
    background: BackgroundPort
    live_session_ids: Callable[[], Collection[int] | None]
    runner_env: dict[str, str]
    python: str
    now: Callable[[], datetime.datetime]


def build_boot(script_path: Path, watchdog_secs: float | None, agent_id: int) -> str:
    """Source of the generated bootstrap file: establish the agent identity,
    arm the optional timeout watchdog, then run the watcher script as
    ``__main__``.

    Agent identity is INLINED as ``MEIVA_AGENT_ID`` into the bootstrap, because
    the session machinery deliberately does NOT forward it: the env allowlist
    ``base/host/env/registry.py`` ``child_env`` (Task #856) drops every
    agent-scope / non-modeled ``MEIVA_*`` knob from session children. Without
    the inline, a watcher child would see ``meiva.self.AGENT_ID=None`` and its
    wake-up ``send_message`` would hit ``/api/agents/None/messages`` → 422 →
    the watcher dies before ever waking its agent (Task #964 — fleet-wide
    silent loss of every scheduled wake-up). The bootstrap is per-agent
    generated code and the id is the agent's own, so nothing sensitive leaks.
    Overwrite, not setdefault: the watcher belongs to the spawning agent, so
    a stale env value (e.g. a parent-shell id accidentally forwarded) must
    not win. The child's first ``import meiva`` composes that identity
    (``owns_loop=False``), cold clients and plugin surface from one boot owner.

    The explicit ``meiva.ensure_plugins_loaded()`` remains an idempotent readiness
    check before the watcher script runs. It reuses the completed parent import
    rather than constructing another configuration or plugin installation.

    The ``meiva`` module is still imported and passed via ``init_globals`` to
    ``runpy.run_path`` so that watcher code can use ``meiva`` without an
    explicit import — this is the public contract of ``launch()``.

    runpy is wrapped in try/finally — no except anywhere: an exception still
    propagates, Python prints the traceback to stderr (teed to the watcher's
    log file and session capture) and exits non-zero, and the shell-level
    completion notice carries the tail of the log. A
    ``SystemExit(n)`` likewise becomes exit code n. The finally block deletes
    the generated script + bootstrap files: a watcher reads them exactly once
    at launch, so removing them on exit keeps the watchers dir empty instead
    of accumulating a script graveyard (the old global $MEIVA_HOME/watchers dir
    grew past 180 files). The watchdog is a daemon timer that prints its
    reason (into the log) and hard-exits with code 124 (the ``timeout(1)``
    convention), so a script stuck in a Python-level loop still dies on time
    — the one thing it cannot preempt is native code that never releases the
    GIL; a killed watcher skips the finally and leaves its pair behind, which
    the next launch prunes (files.prune_stale).

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
    (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md), so a watcher
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
        # MEIVA_AGENT_ID from session children, so without this line the child
        # would see meiva.self.AGENT_ID=None and its wake-up send_message would
        # 422 on /api/agents/None/messages (Task #964). Inline the spawning
        # agent's id — per-agent generated file, own id, nothing sensitive.
        # The inline must land BEFORE `import meiva`: importing meiva with a
        # stale MEIVA_AGENT_ID in the environment (a session backend freezes the
        # env of its first session, so a pane can carry another agent's id)
        # establishes the WRONG identity at import time, and the assignment
        # below would then be too late to move it (2026-08-09: every watcher
        # child on the shared server woke agent 2959 instead of its owner).
        f'os.environ["MEIVA_AGENT_ID"] = "{agent_id}"\n'
        # Same session-env leak as MEIVA_AGENT_ID: the child inherits
        # MEIVA_PROCESS_PROFILE from the creating process's env,
        # but the watcher is an agent subprocess and needs the agent profile
        # to import meiva without hitting the per-process config guard
        # (agent/db/__init__.py reads settings.agent at module level, and the runner
        # profile does not construct the agent domain — Task #856 fail-fast).
        f'os.environ["MEIVA_PROCESS_PROFILE"] = "agent"\n'
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
        "import meiva\n"
        "\n"
        f"{watchdog}"
        # Parent import completed the launched context and plugin surface.
        # Keep the explicit readiness check; it reuses that installation.
        "meiva.ensure_plugins_loaded()\n"
        "try:\n"
        f"    runpy.run_path({str(script_path)!r}, run_name='__main__', init_globals={{'meiva': meiva}})\n"
        "finally:\n"
        "    # Self-cleanup: delete both generated files (read once at launch).\n"
        "    # Only OSError is swallowed — a watcher exception still propagates.\n"
        f"    for _p in (__file__, {str(script_path)!r}):\n"
        "        try:\n"
        "            os.unlink(_p)\n"
        "        except OSError:\n"
        "            pass\n"
    )


def run(request: SpawnRequest, deps: SpawnDeps) -> int:
    """Start a watcher child running ``request.code``; return its watcher id.

    A watcher is nothing more than a shell session running a generated
    script — there is no separate registry or desired-state record
    (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md): list it, capture
    its output, renew its deadline, or kill it exactly like any other session
    via ``meiva.shell.sessions``. Re-registering the same schedule (`cron()`
    with the same expression/timezone, say) does not replace anything — it
    simply starts another independent session; nothing dedupes.

    The session sources a generated shell launcher, which runs the agent's
    script (written verbatim) and a generated bootstrap that inlines the agent
    identity, arms the optional
    watchdog and runs the script via runpy — so the command line typed into
    the session stays short and readable. Identity is inlined because the
    session env allowlist does not forward ``MEIVA_AGENT_ID`` (Task #856 /
    #964; see ``_build_boot``). Output is teed to a per-agent log file and
    session capture, and a completion notice (log path + output
    tail) is delivered from the shell level when the child exits, on every
    exit path — a crashed or hard-killed child cannot skip it. The session
    closes itself after the notice is delivered (the log file preserves the
    output); a notice that fails to send leaves the session open as the
    post-mortem site.
    """
    code, watchdog_secs, name, kind = (
        request.code,
        request.watchdog_secs,
        request.name,
        request.kind,
    )
    notify = deps.background.validate_notify(request.notify)
    agent_id = deps.agent_id
    # The session's shell TTL IS this watcher's target deadline (user ruling
    # 2026-09-14, task #3411): launch = created + timeout, cron = cron_end_at,
    # at = fires_at + grace — derived once, here, by
    # `base.daemon.schedules.watcher.session_deadline`. Written as the
    # system-side TRUE value: a 7-day standing cron is a normal watcher,
    # exempt from the 24h user-session cap. `meiva.shell.sessions.renew` can
    # move this same deadline later, exactly like any other session's TTL —
    # but only the session's reclamation, never the generated script's own
    # end (the cron's `_END`, the launch watchdog, the at fire moment), and
    # a renewal call is itself capped at 24h, so it can pull a standing
    # cron's reclaim earlier than its declared end.
    now = deps.now()
    deadline = session_deadline(
        kind,
        created_at=now,
        timeout_secs=request.timeout_secs,
        fires_at=request.fires_at,
        cron_end_at=request.cron_end_at,
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
    session_id, _session_name = deps.sessions.create_session(
        name, ttl=(deadline - now).total_seconds(), system=True, env_overrides=deps.runner_env
    )
    script_path = deps.watchers_dir / f"watcher_{session_id}.py"
    # Prune files from earlier watchers before writing: a watcher reads its
    # script + bootstrap exactly once at launch, so everything already on
    # disk is dead weight (see files.prune_stale). This keeps this
    # agent's tmp watchers dir from ever accumulating a graveyard.
    files.prune_stale(script_path, deps.live_session_ids())
    # Write the agent's program verbatim — nothing prepended, so a leading
    # `from __future__` import (which must be the first statement of a file)
    # stays valid. The bootstrap lives in its own generated file.
    script_path.write_text(code)
    boot_path = deps.watchers_dir / f"watcher_{session_id}_boot.py"
    boot_path.write_text(build_boot(script_path, watchdog_secs, agent_id))
    output_path = deps.background.allocate_output_path(session_id, name)
    cmd = f"{SESSION_ID_ENV}={session_id} {shlex.quote(deps.python)} {shlex.quote(str(boot_path))}"
    line = deps.background.notified_line(
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
    # meiva:ref meiva/watcher/docs/launch-line.meiva.okf.md#watcher-launch-line-sourced-from-disk
    launch_path = script_path.with_name(f"watcher_{session_id}_launch.sh")
    try:
        launch_line = deps.background.write_launch_file(line, launch_path)
        deps.sessions.send(session_id, launch_line)
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
            deps.sessions.kill(session_id)
        except Exception:
            logger.warning(
                "[watcher] killing session %s after the failed start also failed; "
                "it stays until its TTL expires",
                session_id,
                exc_info=True,
            )
        raise
    return session_id
