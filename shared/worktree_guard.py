"""Live-anchor scan that guards `git worktree remove` (issue #194).

A worktree removal is silent about what it kills: any live session or process
whose cwd (or interpreter) lies under the worktree loses its floor the moment
the checkout's `.venv` disappears. The prod instance (issue #194): schedule 1
was running from `~/.ava/.worktrees/u1a-dark-tokens` on a runner host — the
gateway that launched it had been started from the worktree, so the schedule
session anchored there; routine post-merge cleanup would have killed a
production schedule with no warning and no obviously-broken signal afterwards
(the runner's DB row kept saying `running`).

`find_live_anchors` reports the two surfaces that can be checked cheaply:

- PTY session records (`$AVA_HOME/run/pty/*.json`): the session host's cwd —
  exactly the surface from the incident, since every cluster-owned session
  (schedules, agent shells) records its cwd there.
- Live processes whose cwd / executable / command line is anchored under the
  path (psutil), excluding the invoking process's own chain (issue #3685 —
  cleanup is normally run from inside the target: `cd <worktree> && check`).

The guard lives in `scripts/check_worktree_remove.py` as the step the
`ship-a-change` skill runs before `git worktree remove`; git itself offers no
hook for that operation, so the check has to sit in the tooling.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import cast

import psutil

from shared.paths import ava_home


def _under(path: str | None, target: Path) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(target)
    except OSError:
        return False


def _caller_chain() -> set[int]:
    """This process plus its ancestors — the pids the anchor scan skips.

    The check is normally invoked BY the cleanup that wants the removal, and
    that habit runs it from inside the target (`cd <worktree> && check`): the
    invoking shell's cwd then lies under the path and the scan would REFUSE a
    clean worktree (issue #3685) — tempting the caller into `--force`, which
    is exactly how a real anchor gets missed. The remover's own invocation is
    not what the removal would kill; a session that genuinely anchors the
    target is still reported by the pty-records arm. A transient gap in the
    chain (a parent reaped mid-walk) must not fail the scan.
    """
    chain = {os.getpid()}
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        chain.update(parent.pid for parent in psutil.Process().parents())
    return chain


def find_live_anchors(path: Path, *, records_dir: Path | None = None) -> list[str]:
    """Human-readable list of live things anchored under `path`.

    `records_dir` overrides the pty records location (tests); it defaults to
    `$AVA_HOME/run/pty`. Process scanning excludes the invoking chain (this
    process and its ancestors): the check is normally run by the cleanup from
    inside the target, and the remover's own invocation is not an anchor
    (issue #3685).
    """
    target = path.resolve()
    hits: list[str] = []
    skip = _caller_chain()

    pty_dir = records_dir if records_dir is not None else ava_home() / "run" / "pty"
    if pty_dir.is_dir():
        for rec in sorted(pty_dir.glob("*.json")):
            try:
                data = json.loads(rec.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            cwd = data.get("cwd")
            if isinstance(cwd, str) and _under(cwd, target):
                hits.append(f"pty session {rec.stem!r} (pid {data.get('pid')}) cwd={cwd!r}")

    for proc in psutil.process_iter(["pid", "cwd", "exe", "cmdline"]):
        pid = proc.info["pid"]
        if pid in skip:
            continue
        cwd = proc.info["cwd"]
        exe = proc.info["exe"]
        cmdline: list[str] = cast("list[str]", proc.info["cmdline"]) or []
        if (
            _under(cwd, target)
            or _under(exe, target)
            or any(tok.startswith(str(target)) for tok in cmdline)
        ):
            hits.append(f"process {pid} cwd={cwd!r} exe={exe!r}")

    return hits
