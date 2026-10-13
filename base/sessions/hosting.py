"""Which supervised session this process is running inside.

The question a host-transition verb asks before running in-process: the stop leg of a
stop/restart kills every service session's whole tree, so a transition launched from inside
one of those trees would be killed by its own stop. Lives with the session records and the
pty-sessions client it reads; `base.host.proc` keeps the process primitives.
"""

from __future__ import annotations

import contextlib

import psutil

from base.native_process.ownership import OwnedProcess, stable_create_time
from base.paths import run_dir
from base.sessions.pty import client
from base.sessions.record import SessionRecord

__all__ = ["hosting_supervised_session"]

# A recorded pid counts as the recorded session only while the live process's
# start time matches the record — the supervisors' own pid-recycling rule
# (`posixproc` / the pty backend use this same 2 s tolerance).
_SESSION_CREATE_TIME_TOLERANCE_S = 2.0


def hosting_supervised_session() -> str | None:
    """The supervised session this process is running INSIDE — the name of a live
    `$AVA_HOME/run/sessions/<name>.json` record, or of a persistent shell session
    of the pty-sessions service, whose pid is this process or one of its ancestors
    — or None when the lineage is clear.

    The question a host-transition verb must ask before running in-process: the
    stop leg of a stop/restart kills every service session's whole tree, and
    agents + their shells are quiesced/reaped with it, so a transition launched
    from inside one of those trees is killed by its own stop mid-flight
    (2026-08-12: a host transition run in an agent's background shell died when
    the stop force-killed that shell's whole tree, stranding the cluster paused
    with every service down).

    A record whose process is gone, or whose pid the OS recycled onto a
    different process (start-time mismatch), does not count. Neither does a
    pty-sessions service that does not answer: there is then no evidence either way.
    """
    try:
        me = psutil.Process()
        lineage = {me.pid} | {p.pid for p in me.parents()}
    except psutil.Error:  # racing our own ancestry going away — no lineage evidence
        return None
    for record_path in (run_dir() / "sessions").glob("*.json"):
        name = record_path.stem
        record = SessionRecord.read(record_path)
        if record is None or record.pid not in lineage:
            continue
        try:
            proc = psutil.Process(record.pid)
            if record.starttime is not None:
                is_record_process = record.identifies(record.pid) is True
            else:
                is_record_process = (
                    abs(stable_create_time(proc) - record.create_time)
                    <= _SESSION_CREATE_TIME_TOLERANCE_S
                )
            if is_record_process:
                return name
        except psutil.Error:
            continue
    return _hosting_pty_session(lineage)


def _hosting_pty_session(lineage: set[int]) -> str | None:
    """The persistent shell session whose shell is in `lineage`, if any.

    A stop closes every persistent terminal, so one run from a shell the
    pty-sessions service holds would end itself mid-stop.
    """
    try:
        sessions = client.list_sessions()
    except OSError:
        return None
    for info in sessions:
        if info.pid in lineage:
            with contextlib.suppress(RuntimeError, psutil.Error):
                if OwnedProcess(info.pid, info.create_time, info.starttime).live():
                    return info.name
    return None
