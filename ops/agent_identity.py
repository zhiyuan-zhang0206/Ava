"""Is the pid on an `agents_meta` row still that agent's own process?

`agents_meta.pid` is a plain integer with no lease behind it. The moment the
process it names dies, the OS is free to hand that number to anything else — and
after a reboot it does so en masse, because the pid counter restarts and walks
back over every number the previous boot's agents held. So a stale row's pid does
not merely go dead; it goes *live again* as somebody else.

Liveness cannot see that. `shared.proc.process_alive` answers "does this number
name a running process", which a recycled pid passes forever, so every consumer
that used liveness as a proxy for "the agent behind this row is still there" was
wrong in the same way: the restarter's corpse reapers left the row standing
(2026-07-30 prod, issue #1123: five rows stuck 'idling' across a dozen scans) and
the hibernation swap-out kept firing SIGUSR1 at whatever now owned the pid —
default disposition *terminate*, aimed at an uninvolved process.

The identity evidence used here is the agent's own launch argv, read back from
the OS process table. `ops/agent_launch.py` execs every agent as
`<venv python> -m agent --agent-id <id> …` (`shared/_reparent.py` `execv`s it, so
that argv is what the kernel records) and the process writes its own `os.getpid()`
onto the row (`agent/_starting.py`), so the pid and the argv describe the same
process by construction. The two identifying fragments are defined HERE and
imported by the launcher rather than the other way round, so the thing that
builds the argv and the thing that matches it cannot drift apart
(`tests/ops/test_agent_identity.py` locks the built argv against the matcher).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from shared.proc import process_alive, process_cmdline

# The two argv fragments that name an agent process to an outside observer.
#
# Deliberately the MINIMAL stable core of the launch argv, not the whole of it:
# during a rolling upgrade a new restarter probes agents the PREVIOUS version
# launched, so matching on flags that come and go (the boot-window timeouts) would
# read every not-yet-restarted agent as a stranger and reap the fleet. These two
# have been in the argv since agents had one.
AGENT_MODULE_ARGV = ("-m", "agent")
AGENT_ID_FLAG = "--agent-id"


class AgentProcessIdentity(StrEnum):
    """What the OS says about the pid an `agents_meta` row carries.

    The two failure kinds are kept apart because they license different actions:
    `FOREIGN` is positive evidence (an argv was read in full and it belongs to
    someone else), `UNREADABLE` is the absence of evidence. Only the former may
    drive a destructive decision.
    """

    OWNED = "owned"
    """The cmdline carries this agent's launch argv — the pid is the agent. The
    only verdict that authorizes acting ON the process (signalling it)."""

    FOREIGN = "foreign"
    """A live process whose full cmdline is somebody else's: the agent is gone AND
    its pid has been recycled. The row must be reconciled, and the pid must never
    be signalled — that signal would land on an uninvolved process."""

    GONE = "gone"
    """No such pid. The agent died and nothing took the number; the row is stale
    but harmless."""

    UNREADABLE = "unreadable"
    """Alive, but its argv could not be read — another user's process, or a zombie
    whose argv the kernel has already released. No evidence either way, so callers
    must treat it as still resident: a probe that cannot see must not be the thing
    that terminates an agent."""


def cmdline_identifies_agent(cmdline: Sequence[str], agent_id: int) -> bool:
    """True if `cmdline` is the argv of agent `agent_id`'s own process.

    Both fragments are matched as *consecutive* runs, not as loose membership:
    `--agent-id` and `235` sitting anywhere in the argv would also match
    `--agent-id 1 --boot-stall-seconds 235`, which is a different agent.
    """
    argv = list(cmdline)
    return _contains_run(argv, AGENT_MODULE_ARGV) and _contains_run(
        argv, (AGENT_ID_FLAG, str(agent_id))
    )


def _contains_run(argv: list[str], run: tuple[str, ...]) -> bool:
    """True if `run` appears as consecutive elements of `argv`."""
    width = len(run)
    return any(tuple(argv[i : i + width]) == run for i in range(len(argv) - width + 1))


def probe_agent_process(pid: int, agent_id: int) -> AgentProcessIdentity:
    """Ask the OS whether `pid` really is agent `agent_id`'s process.

    Only meaningful for a same-host pid: a pid carried by another machine's row is
    not ours to probe, so every caller is already machine-scoped.

    The unreadable case costs a second syscall (`process_alive`) to separate "gone"
    from "alive but opaque" — worth it only because those two verdicts lead to
    opposite actions, and it is on the rare path anyway.
    """
    cmdline = process_cmdline(pid)
    if cmdline is None:
        return AgentProcessIdentity.UNREADABLE if process_alive(pid) else AgentProcessIdentity.GONE
    if cmdline_identifies_agent(cmdline, agent_id):
        return AgentProcessIdentity.OWNED
    return AgentProcessIdentity.FOREIGN


# The verdicts that mean "leave the row alone": the agent is there, or we cannot
# prove it is not. Everything else (`GONE` / `FOREIGN`) is a row whose process no
# longer exists and which its owner must reconcile.
RESIDENT_IDENTITIES = frozenset({AgentProcessIdentity.OWNED, AgentProcessIdentity.UNREADABLE})
