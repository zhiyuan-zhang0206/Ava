"""Machine-local process evidence that a legacy hosted predecessor is dead.

`agents_meta.incarnation_resources` is NULL for legacy rows and stays unknown
under current publication, so a hosted successor has no stored host process
identity to prove its predecessor exited (issue #2156). This module supplies
the replacement evidence for the same-machine hosted handoff: one scan of this
machine's process table reporting every live process that could still be
serving this home's hosted work.

Only same-user processes are comparable — a foreign-user process cannot be
this unit's daemon or exec child — and two shapes are checked:

- another agent-host daemon of this exact home (``-m services.agent_host.daemon``,
  home from its own environment; a daemon whose home cannot be read counts as
  this home, the rule the ops-side local host probe already uses);
- a live managed exec child of the requested agent (``-m agent.exec_child``,
  attributed through its ``AVA_EXEC_REQUEST_FILE`` envelope; ``AVA_AGENT_ID``
  is the fallback only when no envelope path is present).

Anything a probe cannot read is reported as blocking, never guessed away. The
caller only treats an empty report as evidence; a blocking report costs it
nothing but today's fallback (wait for the lease). Every field is a fact
about live processes, not a policy decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import psutil

from shared.paths import exec_run_dir

_DAEMON = "agent-host daemon"
_EXEC_CHILD = "exec child"
_DAEMON_ARGV = ("-m", "services.agent_host.daemon")
_EXEC_CHILD_ARGV = ("-m", "agent.exec_child")


@dataclass(frozen=True)
class LocalHostEvidence:
    """What one process-table scan found for a legacy-host adoption decision."""

    live_hosts: tuple[int, ...] = ()
    live_exec_children: tuple[int, ...] = ()
    unreadable: tuple[str, ...] = ()
    scanned: int = 0

    @property
    def clean(self) -> bool:
        """True only when every probe ran and found nothing alive."""
        return not self.live_hosts and not self.live_exec_children and not self.unreadable

    def blocking_reasons(self) -> list[str]:
        """Human-readable refusal reasons, for logs and audit context."""
        reasons: list[str] = []
        if self.live_hosts:
            reasons.append(f"live same-home agent-host daemon(s) {list(self.live_hosts)}")
        if self.live_exec_children:
            reasons.append(f"live exec child(ren) of this agent {list(self.live_exec_children)}")
        reasons.extend(self.unreadable)
        return reasons


def local_host_evidence(agent_id: int, home: Path, *, exclude_pid: int) -> LocalHostEvidence:
    """Scan this machine for live processes that could still serve ``agent_id``.

    ``exclude_pid`` is the scanning process itself — the one agent-host daemon
    of this home that is expected to exist.
    """
    resolved_home = home.resolve()
    exec_root = (exec_run_dir() / str(agent_id)).resolve()
    live_hosts: list[int] = []
    live_exec_children: list[int] = []
    unreadable: list[str] = []
    scanned = 0
    for process in psutil.process_iter(["pid", "cmdline"]):
        pid = process.info["pid"]
        argv = process.info["cmdline"]
        if pid == exclude_pid or not argv:
            continue
        shape = _shape(argv)
        if shape is None:
            continue
        try:
            if os.name == "posix" and process.uids().real != os.getuid():
                # A foreign-user process cannot be this unit's daemon or exec
                # child. Windows has no comparable uid (placeholder ids); the
                # environment read below decides ownership there.
                continue
        except psutil.AccessDenied:
            unreadable.append(f"pid {pid}: owner unreadable for a {shape} shape")
            continue
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        scanned += 1
        try:
            env = process.environ()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            unreadable.append(f"pid {pid}: environment unreadable for a {shape} shape")
            continue
        if shape == _DAEMON:
            verdict = _daemon_verdict(env.get("AVA_HOME"), resolved_home)
        else:
            verdict = _exec_child_verdict(env, exec_root, agent_id)
        if verdict is None:
            unreadable.append(f"pid {pid}: {shape} identity unreadable")
        elif verdict:
            (live_hosts if shape == _DAEMON else live_exec_children).append(pid)
    return LocalHostEvidence(
        live_hosts=tuple(live_hosts),
        live_exec_children=tuple(live_exec_children),
        unreadable=tuple(unreadable),
        scanned=scanned,
    )


def _shape(argv: list[str]) -> str | None:
    pairs = set(pairwise(argv))
    if _DAEMON_ARGV in pairs:
        return _DAEMON
    if _EXEC_CHILD_ARGV in pairs:
        return _EXEC_CHILD
    return None


def _daemon_verdict(raw_home: str | None, home: Path) -> bool | None:
    """True when this daemon serves ``home``; None when its home is unreadable."""
    if raw_home is None:
        # A daemon without a readable home is treated as this home, exactly
        # like the ops-side local host probe (ops/agent_pause_probe.py).
        return True
    try:
        return Path(raw_home).resolve() == home
    except (OSError, ValueError):
        return None


def _exec_child_verdict(env: dict[str, str], exec_root: Path, agent_id: int) -> bool | None:
    """True when this exec child serves ``agent_id``; None when unattributable.

    The request envelope locates its agent by construction, so it is the
    authoritative signal; ``AVA_AGENT_ID`` is only consulted when no envelope
    path is present (an id alone cannot distinguish another home's child).
    """
    request = env.get("AVA_EXEC_REQUEST_FILE")
    if request is not None:
        try:
            return Path(request).resolve().is_relative_to(exec_root)
        except (OSError, ValueError):
            return None
    env_agent = env.get("AVA_AGENT_ID")
    if env_agent is None:
        return None
    return env_agent == str(agent_id)
