"""Memory guard for exec subprocesses: relieve critical system memory pressure
by killing the one largest `execute_code` process domain, and tell its agent why.

The agent host runs one sequential loop (`ExecMemoryGuard.run_forever`). Each tick
reads the pressure level the operating system itself reports
(`base.host.memory_pressure`); only at ``critical`` does it look at the exec
domains this host owns, pick the one with the largest footprint, record the
reason next to its result file and kill its session leader. The run that owns
that domain observes the root exit exactly as for any external SIGKILL — its
`DomainCloseOwner` closes the whole process group and reaps — and then reads the
notice, so the agent's result states the cause instead of a generic "exited
without a result envelope". The loop sleeps one interval after a kill and
re-reads the level before it considers another domain, so a recovery that
follows the first kill is never answered with a second.

Domains are found by the OS, not tracked: the host's descendants that lead a
session running an exec entry module (`base.host.proc.EXEC_DOMAIN_SESSION_ENTRIES`),
with the agent id and result path read from the leader's launch environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from base.host.memory_pressure import MemorySource
from base.host.proc import EXEC_DOMAIN_SESSION_ENTRIES
from base.log import logger
from base.native_process.exec_kill_notice import write_notice

POLL_INTERVAL_S = 5.0
_GIB = 1024**3


@dataclass(frozen=True)
class ExecDomain:
    """One running exec process domain and what it holds."""

    pid: int
    create_time: float
    agent_id: int | None
    result_path: Path
    footprint: int


def kill_notice(domain: ExecDomain, running: int) -> str:
    """The text the killed exec's agent reads."""
    return (
        "[exec killed by the host memory guard: the system reached critical memory "
        f"pressure; this exec was the largest at {domain.footprint / _GIB:.1f} GiB of "
        f"{running} running. Process less data at a time: split the work into batches "
        "and stream or chunk reads instead of loading everything, then run it again.]"
    )


def _is_exec_leader(process: psutil.Process) -> bool:
    try:
        return process.pid == os.getsid(process.pid) and any(
            argument in EXEC_DOMAIN_SESSION_ENTRIES for argument in process.cmdline()
        )
    except (psutil.Error, OSError):
        return False


def find_exec_domains(host_pid: int, source: MemorySource) -> list[ExecDomain]:
    """Every exec process domain under `host_pid`, with its group's total footprint."""
    try:
        descendants = psutil.Process(host_pid).children(recursive=True)
    except psutil.Error:
        return []
    leaders = [process for process in descendants if _is_exec_leader(process)]
    members: dict[int, list[int]] = {process.pid: [] for process in leaders}
    for process in psutil.process_iter():
        with contextlib.suppress(psutil.Error, OSError):
            group = os.getpgid(process.pid)
            if group in members:
                members[group].append(process.pid)
    domains: list[ExecDomain] = []
    for leader in leaders:
        try:
            env = leader.environ()
            raw_agent_id = env.get("AVA_AGENT_ID")
            result_path = Path(env["AVA_EXEC_RESULT_FILE"])
            created = leader.create_time()
        except (psutil.Error, KeyError, OSError):
            continue
        domains.append(
            ExecDomain(
                pid=leader.pid,
                create_time=created,
                agent_id=int(raw_agent_id) if raw_agent_id else None,
                result_path=result_path,
                footprint=sum(source.footprint(pid) for pid in members[leader.pid]),
            )
        )
    return domains


def kill_domain_leader(domain: ExecDomain) -> None:
    """SIGKILL the domain's session leader if it is still the process we measured.

    The run that owns the domain closes the rest of the group when it sees the
    root exit (`DomainCloseOwner`), the same path an external SIGKILL takes.
    """
    try:
        process = psutil.Process(domain.pid)
        if process.create_time() != domain.create_time:
            return
        process.kill()
    except psutil.NoSuchProcess:
        return


class ExecMemoryGuard:
    """The sequential loop; `source`, `domains` and `kill` are the test seams."""

    def __init__(
        self,
        source: MemorySource,
        *,
        domains: Callable[[], list[ExecDomain]],
        kill: Callable[[ExecDomain], None] = kill_domain_leader,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._source = source
        self._domains = domains
        self._kill = kill
        self._interval_s = interval_s

    def check_once(self) -> ExecDomain | None:
        """Kill the largest exec domain if and only if the system is critical."""
        level = self._source.pressure()
        if level != "critical":
            return None
        candidates = self._domains()
        if not candidates:
            return None
        victim = max(candidates, key=lambda domain: (domain.footprint, domain.pid))
        notice = kill_notice(victim, len(candidates))
        write_notice(victim.result_path, notice)
        self._kill(victim)
        logger.warning(
            "[{label}] {notice}",
            label="exec-memory-guard",
            notice=notice,
            event="exec_memory_guard_killed",
            agent_id=victim.agent_id,
            pid=victim.pid,
            footprint_bytes=victim.footprint,
            running=len(candidates),
            pressure=level,
        )
        return victim

    async def run_forever(self) -> None:
        """One tick per interval; a failed tick is logged and the loop continues."""
        # quiesce-exempt: reads OS memory pressure and kills an exec domain; no database
        while True:
            try:
                await asyncio.to_thread(self.check_once)
            except Exception:
                logger.exception("[exec-memory-guard] tick failed — retrying next interval")
            await asyncio.sleep(self._interval_s)
