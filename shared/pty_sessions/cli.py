"""PTY sessions CLI — the transport contract the SDK (W1b) consumes.

Invocation shape::

    python -m shared.pty_sessions.cli <name> <op> [args]
    python -m shared.pty_sessions.cli list [prefix]
    python -m shared.pty_sessions.cli list-started-at [prefix]

Ops (session ops take the session name first):

- ``has <name>``            exit 0 if the session is alive, 1 otherwise.
                            Answered from the on-disk record (shell pid +
                            start-time) — no process to dial, so it is
                            truthful unconditionally.
- ``new <name> <cwd> <envfile>``   spawn the session's own detached host
                            process (``host.py``), which allocates the pty
                            and runs ``bash -l -i`` with env loaded from the
                            0600 envfile (see ``write_env_file``).
                            Idempotent only within its allocation generation.
- ``send <name> <b64>``     write text (no Enter) — base64 single argument
                            so arbitrary text survives argv quoting.
- ``send_keys <name> <key>...``    translate screen-vocabulary key names to
                            bytes and write them (C-c, Up, Escape, ...).
- ``capture <name> [lines] [--scrollback|--no-scrollback]``
                            print captured text to stdout (pyte render;
                            scrollback defaults to True).
- ``resize <name> <cols> <rows>``  TIOCSWINSZ + SIGWINCH to the group.
- ``kill <name> [--graceful]``     kill the session's process tree
                            (graceful: SIGTERM first); idempotent noop.
                            Falls back to record-pid kills when the host
                            itself is wedged, so a kill is authoritative
                            even against a broken host. On success prints
                            `interrupted` when the session carried live
                            processes (a running foreground/background job)
                            at kill time, `idle` otherwise — the TTL
                            reaper's interrupt verdict (a record-based kill
                            of a wedged host answers `interrupted`,
                            fail-open).
- ``list [prefix]``         live session names, one per line (record scan —
                            no process to dial; sweeps dead records).
- ``list-started-at [prefix]``     every live session's launch epoch.

Exit codes: 0 success; 1 operational error; 2 usage error; 3 no such
session. ``has`` is the exception: 0 = alive, 1 = not alive. Errors go to
stderr, payloads to stdout, both empty unless stated.

Transport: session ops are one JSON line over the session's own unix socket
(``$AVA_HOME/run/pty/<name>.sock``), answered by that session's host. There
is no supervisor daemon — creation spawns a host, enumeration reads records
— so no infra process stands between a live session and its caller.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

import psutil

from shared.log import logger
from shared.paths import run_dir
from shared.platform import LockTimeoutError
from shared.pty_sessions._paths import (
    host_identity,
    host_log_path,
    host_starttime,
    pty_dir,
    record_path,
    socket_path,
    transcript_path,
)
from shared.pty_sessions.allocation_freeze import locked_freeze_state, state_path
from shared.pty_sessions.orphan_reaper import _reap_orphaned_hosts
from shared.session_record import SessionRecord, pid_starttime_ticks

# ---------------------------------------------------------------------------
# Key translation — the classic send-keys vocabulary (prototype _KEYMAP, with
# the canonical names added: BSpace/DC/IC/PPage/NPage/BTab, M-<x>,
# C-Space/C-/).
# ---------------------------------------------------------------------------

_KEYMAP = {
    "Enter": b"\r",
    "Space": b" ",
    "Tab": b"\t",
    "BTab": b"\x1b[Z",
    "Escape": b"\x1b",
    "Esc": b"\x1b",
    "Backspace": b"\x7f",
    "BSpace": b"\x7f",
    "Delete": b"\x1b[3~",
    "DC": b"\x1b[3~",
    "Insert": b"\x1b[2~",
    "IC": b"\x1b[2~",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PPage": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "NPage": b"\x1b[6~",
    "S-Up": b"\x1b[1;2A",
    "S-Down": b"\x1b[1;2B",
    "S-Right": b"\x1b[1;2C",
    "S-Left": b"\x1b[1;2D",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    "F5": b"\x1b[15~",
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
}

# C-<x> for every printable control char spelled that way (C-a .. C-z,
# C-@ C-[ C-] C-^ C-_ C-?); the rest are explicit below.
_CTRL_RE = re.compile(r"^C-([a-zA-Z@\[\]^_?])$")
_META_RE = re.compile(r"^M-(.)$")
_CTRL_EXTRA = {
    "C-Space": b"\x00",
    "C-@": b"\x00",
    "C-/": b"\x1f",
    "C-\\": b"\x1c",
}


def keys_to_bytes(keys: tuple[str, ...]) -> bytes:
    """Translate screen send-keys key names to the bytes to write to the pty.

    screen semantics: a single character is typed literally; a known key name
    (C-c, Up, Escape, ...) translates to its control/escape bytes; an
    unknown name is typed as literal text (the screen treats unrecognized keys as
    strings of characters).
    """
    out = b""
    for key in keys:
        if len(key) == 1:
            out += key.encode("utf-8")
            continue
        if key in _CTRL_EXTRA:
            out += _CTRL_EXTRA[key]
            continue
        m = _CTRL_RE.match(key)
        if m:
            ch = m.group(1)
            code = ord(ch.upper()) - ord("@") if ch != "?" else 0x7F
            out += bytes([code])
            continue
        m = _META_RE.match(key)
        if m:
            out += b"\x1b" + m.group(1).encode("utf-8")
            continue
        out += _KEYMAP.get(key, key.encode("utf-8"))
    return out


# ---------------------------------------------------------------------------
# Envfile mechanism (0600 file, values never on argv — issue #974).
# ---------------------------------------------------------------------------

# What a POSIX shell can assign to (mirrors shared.session_env): keys outside
# this cannot ride a sourced envfile.
_SHELL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Handoff files are consumed (unlinked) by the host at startup; anything
# still around after this is debris from a session that never started.
_ENV_FILE_MAX_AGE_S = 3600.0


def _session_env_dir() -> Path:
    """$AVA_HOME/run/session-env/ — the env-handoff dir, so the
    shared stale-file sweep and one mechanism serve both backends."""
    target = run_dir() / "session-env"
    target.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # a foreign-owned dir is not ours to re-mode
        target.chmod(0o700)
    return target


def write_env_file(env: dict[str, str]) -> Path:
    """Write `env` to a fresh 0600 file under $AVA_HOME/run/session-env/ and
    return its path — the ``new`` op's envfile argument.

    Same on-disk format as ``shared.session_env.env_load_prefix`` (KEY=shlex-
    quoted values), so files from either writer load in the host; only
    shell-identifier keys ride (a child env can hold names a shell cannot
    assign). The host consumes the file at startup.
    """
    directory = _session_env_dir()
    cutoff = time.time() - _ENV_FILE_MAX_AGE_S
    with contextlib.suppress(OSError):
        for stale in directory.glob("*.env.sh"):
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
    body = ""
    for key, value in sorted(env.items()):
        if not _SHELL_IDENTIFIER_RE.fullmatch(key):
            continue
        if "\0" in value:
            raise RuntimeError(f"env {key} value contains \0 and cannot be forwarded")
        body += f"{key}={shlex.quote(value)}\n"
    path = directory / f"{uuid.uuid4().hex}.env.sh"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


# ---------------------------------------------------------------------------
# Record liveness + enumeration (no process to dial — the records ARE the
# session listing; a dead record is swept as it is discovered).
# ---------------------------------------------------------------------------

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance (mirrors posixproc).
_CREATE_TIME_TOLERANCE_S = 2.0


def _record_alive(rec: SessionRecord) -> bool:
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running():
            return False
        if rec.starttime is not None:
            return rec.identifies(rec.pid) is True
        return abs(proc.create_time() - rec.create_time) <= _CREATE_TIME_TOLERANCE_S
    except psutil.Error:
        return False


def has_session(name: str) -> bool:
    """True if the named pty session's record points at a live, matching
    shell process — the same liveness rule posixproc applies to its records."""
    rec = SessionRecord.read(record_path(name))
    return rec is not None and _record_alive(rec)


def session_started_at(name: str) -> float | None:
    """Epoch seconds the named pty session was launched, or None when it is
    not alive. Same record + pid liveness rule as `has_session`."""
    rec = SessionRecord.read(record_path(name))
    if rec is None or not _record_alive(rec):
        return None
    return rec.started_at


def session_generation(name: str) -> str | None:
    """The live PTY session's persisted flip generation, if one exists."""
    rec = SessionRecord.read(record_path(name))
    if rec is None or not _record_alive(rec):
        return None
    return rec.generation


# Last warning reason per retained record: a tick-scanning poller (the
# page-server daemon scans every ~2s pass) would otherwise flood the log with
# an identical warning every pass. Entries exist only while a record keeps
# failing liveness; a swept record's entry is dropped so a re-created record
# warns again.
_retained_warning_reasons: dict[str, str] = {}


def _sweep_dead(name: str) -> None:
    """Drop a provably dead session's record + socket (its host is gone too)."""
    rec = SessionRecord.read(record_path(name))
    if rec is not None:
        reapable, why = _record_reapable(rec)
        if not reapable:
            if _retained_warning_reasons.get(name) != why:
                logger.warning(
                    "pty retaining live session record {name}: {why}",
                    name=name,
                    why=why,
                )
                _retained_warning_reasons[name] = why
            return
    _retained_warning_reasons.pop(name, None)
    with contextlib.suppress(OSError):
        record_path(name).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        socket_path(name).unlink(missing_ok=True)


def _record_reapable(rec: SessionRecord) -> tuple[bool, str]:
    """Whether a pty record that failed liveness can safely be swept."""
    if _record_alive(rec):
        return False, "shell is still live"
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running():
            return True, "shell pid is no longer running"
    except psutil.NoSuchProcess:
        return True, "shell pid is gone"
    except (psutil.AccessDenied, OSError):
        return False, "shell pid could not be inspected"
    if rec.identifies(rec.pid) is False:
        return True, "shell pid was reused by another process"
    return False, "live shell pid did not satisfy the legacy identity check"


def live_sessions(prefix: str = "") -> dict[str, SessionRecord]:
    """Every live session's record, filtered by name prefix.

    The record scan is the session listing; a record whose shell is gone is
    a crashed host's leftover and is swept as it is discovered (the same
    lazy sweep posixproc.list_sessions performs on its dir).
    """
    out: dict[str, SessionRecord] = {}
    for rec_file in sorted(pty_dir().glob("*.json")):
        name = rec_file.stem
        if not name.startswith(prefix):
            continue
        rec = SessionRecord.read(rec_file)
        if rec is None or not _record_alive(rec):
            _sweep_dead(name)
            continue
        out[name] = rec
    return out


# ---------------------------------------------------------------------------
# Socket client + ops.
# ---------------------------------------------------------------------------

# Long enough to cover the host's longest op (graceful kill waits up to
# _KILL_WAIT_S before escalating); connect stays instant.
_HOST_TIMEOUT_S = 30.0

# How long `new` waits for the spawned host to answer a ping. The host binds
# its socket before forking the shell, so readiness is interpreter startup
# (~0.5s), not bash login.
_SPAWN_READY_TIMEOUT_S = 15.0


def session_request(name: str, req: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON request to the session's host, return its response.

    Raises:
        OSError: no host is answering on the session's socket (session dead
            or its host wedged) or the reply was malformed.
    """
    path = str(socket_path(name))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(_HOST_TIMEOUT_S)
        conn.connect(path)
        conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
    if not buf:
        raise OSError("pty session host closed the connection without answering")
    resp = cast("dict[str, Any]", json.loads(buf.split(b"\n", 1)[0].decode("utf-8")))
    if not isinstance(resp, dict):
        raise OSError("pty session host returned a malformed response")
    return resp


def _no_host(name: str, op: str, exc: OSError) -> int:
    """Map a dead socket to the exit contract: 3 when the session is gone,
    1 (with a diagnostic) when the record claims alive but the host is not
    answering — a wedged or killed host, worth a loud error not a shrug."""
    if not has_session(name):
        sys.stderr.write(f"no such pty session: {name}\n")
        return 3
    sys.stderr.write(
        f"pty session {name} exists but its host is not answering "
        f"({socket_path(name)}): {exc} (op {op})\n"
    )
    return 1


def _finish_op(resp: dict[str, Any]) -> int:
    """Map a host response to the CLI exit code, printing the error."""
    if resp.get("ok"):
        return 0
    code = int(resp.get("code") or 1)
    if resp.get("error"):
        sys.stderr.write(str(resp["error"]) + "\n")
    return code


def _op_has(name: str) -> int:
    return 0 if has_session(name) else 1


def _spawn_host(
    name: str,
    cwd: str,
    envfile: str,
    generation: str | None,
    cmd_b64: str,
) -> int:
    """Launch the session's detached host via _reparent and wait for it to
    answer. Returns the CLI exit code.

    The record/socket/transcript paths are resolved HERE and passed on argv:
    the host process never imports the Settings chain (`shared.paths`), which
    is most of a python process's avoidable RSS — one host exists per live
    session, so its import closure is the fleet's steady-state memory.
    """
    log = host_log_path(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        "shared._reparent",
        str(log),
        str(log),
        sys.executable,
        "-m",
        "shared.pty_sessions.host",
        name,
        cwd,
        envfile,
        str(record_path(name)),
        str(socket_path(name)),
        str(transcript_path(name)),
        generation or "",
    ]
    if cmd_b64:
        argv.append(cmd_b64)
    try:
        spawned = subprocess.run(argv, capture_output=True, timeout=30.0, check=True)  # noqa: S603 — argv is repo-internal (interpreter + module names + validated session args)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"cannot spawn pty session host for {name}: {exc}\n")
        return 1
    # _reparent hands the detached host's pid back on stdout — a host that
    # died pre-socket (bad envfile, pty exhaustion) fails this fast instead
    # of running out the ready wait.
    try:
        host_pid = int(spawned.stdout.strip())
    except ValueError:
        sys.stderr.write(
            f"cannot establish spawned pty host identity for {name}: {spawned.stdout!r}\n"
        )
        try:
            _reap_orphaned_hosts(name, force_unresponsive=True)
        except RuntimeError as exc:
            sys.stderr.write(f"cannot reap unidentified pty host for {name}: {exc}\n")
        return 1
    deadline = time.monotonic() + _SPAWN_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError):
            resp = session_request(name, {"op": "ping"})
            if resp.get("ok"):
                return 0
        # A concurrent `new` may have won the double-start guard; its live
        # session answers `has` even while our own spawn lost.
        if has_session(name):
            return 0
        if host_pid and not psutil.pid_exists(host_pid):
            break  # the host died before answering — report its log now
        time.sleep(0.05)
    tail = ""
    with contextlib.suppress(OSError):
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-8:])
    sys.stderr.write(f"pty session host for {name} did not come up; log tail ({log}):\n{tail}\n")
    _abort_failed_spawn(name, host_pid)
    return 1


def _abort_failed_spawn(name: str, host_pid: int) -> None:
    """Make a failed allocation terminal before its admission lock is released.

    A host that merely missed the ready deadline could otherwise write its
    record after a concurrent freeze acknowledged. Snapshot descendants while
    the known host is alive, kill the host first so it cannot fork more work,
    then kill the snapshot and remove only provably dead session artifacts.
    """
    processes: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        host = psutil.Process(host_pid)
        processes = [host, *host.children(recursive=True)]
    for proc in processes:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    if processes:
        _gone, alive = psutil.wait_procs(processes, timeout=3.0)
        survivors: list[int] = []
        for proc in alive:
            with contextlib.suppress(psutil.Error):
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    survivors.append(proc.pid)
        if survivors:
            sys.stderr.write(
                f"failed pty allocation for {name} left live process(es): {sorted(survivors)}\n"
            )
    _sweep_dead(name)
    try:
        _reap_orphaned_hosts(name, force_unresponsive=True)
    except RuntimeError as exc:
        sys.stderr.write(f"failed pty allocation for {name} left an orphaned host: {exc}\n")


def _op_new(name: str, rest: list[str]) -> int:
    if len(rest) not in (2, 3):
        sys.stderr.write(f"usage: pty_sessions.cli {name} new <cwd> <envfile> [cmd_b64]\n")
        return 2
    cwd, envfile = rest[0], rest[1]
    cmd_b64 = rest[2] if len(rest) == 3 else ""
    try:
        with locked_freeze_state() as freeze:
            try:
                reaped = _reap_orphaned_hosts(name)
            except RuntimeError as exc:
                sys.stderr.write(f"cannot reap orphan pty host for {name}: {exc}\n")
                return 1
            if reaped:
                logger.warning(
                    "pty new {name}: reaped {count} orphan host(s)", name=name, count=reaped
                )
            generation = freeze.generation
            if has_session(name):
                if (
                    freeze.status != "frozen"
                    and generation is not None
                    and session_generation(name) != generation
                ):
                    sys.stderr.write(
                        f"pty session {name} belongs to a prior generation; reap it before "
                        "rebuilding desired state\n"
                    )
                    return 1
                # Already exists = idempotent no-op, including while frozen. A
                # freeze protects absent -> live allocation, never use of an
                # existing session.
                return 0
            if freeze.status == "frozen":
                sys.stderr.write(
                    "pty allocation refused: host allocation is frozen by "
                    f"{freeze.holder!r} (generation {freeze.generation!r}): {freeze.reason}\n"
                )
                return 1
            if freeze.status == "invalid":
                sys.stderr.write(
                    f"pty allocation refused: host freeze marker {state_path()} is invalid "
                    f"({freeze.error}); repair it before allocating new sessions\n"
                )
                return 1
            # Keep the host-wide lock until ready. Once a concurrent freeze
            # returns, every allocation before it has a visible live record and
            # every allocation after it observes the marker above.
            return _spawn_host(name, cwd, envfile, generation, cmd_b64)
    except (LockTimeoutError, OSError) as exc:
        sys.stderr.write(
            f"pty allocation for {name} could not take the host allocation lock: {exc}\n"
        )
        return 1
    finally:
        # The host consumes the handoff on successful creation. Every other
        # outcome, including freeze refusal and idempotent reuse, owns cleanup.
        with contextlib.suppress(OSError):
            Path(envfile).unlink()


def _op_send(name: str, rest: list[str]) -> int:
    if len(rest) != 1:
        sys.stderr.write("usage: pty_sessions.cli <name> send <base64-text>\n")
        return 2
    try:
        resp = session_request(name, {"op": "send", "data": rest[0]})
    except OSError as exc:
        return _no_host(name, "send", exc)
    return _finish_op(resp)


def _op_send_keys(name: str, rest: list[str]) -> int:
    if not rest:
        sys.stderr.write("usage: pty_sessions.cli <name> send_keys <key>...\n")
        return 2
    data = base64.b64encode(keys_to_bytes(tuple(rest))).decode("ascii")
    try:
        resp = session_request(name, {"op": "send_keys", "data": data})
    except OSError as exc:
        return _no_host(name, "send_keys", exc)
    return _finish_op(resp)


def _op_capture(name: str, rest: list[str]) -> int:
    lines = 200
    scrollback = True
    for arg in rest:
        if arg == "--scrollback":
            scrollback = True
        elif arg == "--no-scrollback":
            scrollback = False
        elif arg.isdigit():
            lines = int(arg)
        else:
            sys.stderr.write(
                f"usage: pty_sessions.cli {name} capture [lines] [--scrollback|--no-scrollback]\n"
            )
            return 2
    if lines < 1 or lines > 100000:
        sys.stderr.write(f"capture lines out of range: {lines}\n")
        return 2
    try:
        resp = session_request(name, {"op": "capture", "lines": lines, "scrollback": scrollback})
    except OSError as exc:
        return _no_host(name, "capture", exc)
    if not resp.get("ok"):
        return _finish_op(resp)
    sys.stdout.write(resp["data"]["text"])
    return 0


def _op_resize(name: str, rest: list[str]) -> int:
    if len(rest) != 2 or not (rest[0].isdigit() and rest[1].isdigit()):
        sys.stderr.write(f"usage: pty_sessions.cli {name} resize <cols> <rows>\n")
        return 2
    try:
        resp = session_request(name, {"op": "resize", "cols": int(rest[0]), "rows": int(rest[1])})
    except OSError as exc:
        return _no_host(name, "resize", exc)
    return _finish_op(resp)


def _kill_by_record(name: str) -> int:
    """Kill a session whose host is not answering, straight from its record.

    The host owns the orderly kill; this fallback keeps `kill` authoritative
    against a wedged or SIGKILLed host: signal the shell's group and the
    host pid (both identity-checked against recorded start-times so a
    recycled pid is never signalled), then sweep the record + socket only
    when they are provably stale.
    """
    rec = SessionRecord.read(record_path(name))
    if rec is not None and _record_alive(rec):
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(rec.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(rec.pid, signal.SIGKILL)
    identity = host_identity(record_path(name))
    if identity is not None:
        host_pid, host_create = identity
        with contextlib.suppress(psutil.Error):
            proc = psutil.Process(host_pid)
            recorded_starttime = host_starttime(record_path(name))
            matches = (
                pid_starttime_ticks(host_pid) == recorded_starttime
                if recorded_starttime is not None
                else abs(proc.create_time() - host_create) <= _CREATE_TIME_TOLERANCE_S
            )
            if proc.is_running() and matches:
                proc.kill()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        rec = SessionRecord.read(record_path(name))
        if rec is None or not _record_alive(rec):
            break
        time.sleep(0.05)
    if has_session(name):
        sys.stderr.write(f"session {name} survived the record-based kill\n")
        return 1
    sys.stdout.write("interrupted\n")  # fail-open: a wedged host's kill could not be probed
    _sweep_dead(name)
    try:
        _reap_orphaned_hosts(name, force_unresponsive=True)
    except RuntimeError as exc:
        sys.stderr.write(f"session {name} left an orphaned host after record-based kill: {exc}\n")
        return 1
    return 0


def _op_kill(name: str, rest: list[str]) -> int:
    graceful = "--graceful" in rest
    unexpected = [a for a in rest if a != "--graceful"]
    if unexpected:
        sys.stderr.write(f"usage: pty_sessions.cli {name} kill [--graceful]\n")
        return 2
    try:
        resp = session_request(name, {"op": "kill", "graceful": graceful})
    except OSError:
        if not has_session(name):
            try:
                _reap_orphaned_hosts(name, force_unresponsive=True)
            except RuntimeError as exc:
                sys.stderr.write(f"cannot reap orphan pty host for {name}: {exc}\n")
                return 1
            _sweep_dead(name)
            sys.stdout.write("idle\n")  # nothing was there to interrupt
            return 0  # idempotent: killing an absent session is a noop
        return _kill_by_record(name)
    result = _finish_op(resp)
    if result != 0:
        return result
    sys.stdout.write("interrupted\n" if resp["data"].get("interrupted") else "idle\n")
    try:
        _reap_orphaned_hosts(name, force_unresponsive=True)
    except RuntimeError as exc:
        sys.stderr.write(f"session {name} left an orphaned host after kill: {exc}\n")
        return 1
    return 0


def _op_list(rest: list[str]) -> int:
    if len(rest) > 1:
        sys.stderr.write("usage: pty_sessions.cli list [prefix]\n")
        return 2
    prefix = rest[0] if rest else ""
    for name in live_sessions(prefix):
        sys.stdout.write(name + "\n")
    return 0


def _op_list_started_at(rest: list[str]) -> int:
    """`list-started-at [prefix]` — every live session's launch epoch (one
    record scan; the status snapshot consumes this). Output: `name <epoch>`
    per line."""
    if len(rest) > 1:
        sys.stderr.write("usage: pty_sessions.cli list-started-at [prefix]\n")
        return 2
    prefix = rest[0] if rest else ""
    for name, rec in live_sessions(prefix).items():
        sys.stdout.write(f"{name} {rec.started_at}\n")
    return 0


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Dispatch one CLI invocation. `python -m shared.pty_sessions.cli`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(
            "usage: pty_sessions.cli <name> <op> [args] | list [prefix] | list-started-at [prefix]\n"
        )
        return 2
    first = args[0]
    if first == "list":
        return _op_list(args[1:])
    if first == "list-started-at":
        return _op_list_started_at(args[1:])
    name = first
    if len(args) < 2:
        sys.stderr.write(f"usage: pty_sessions.cli {name} <op> [args]\n")
        return 2
    op, rest = args[1], args[2:]
    if op == "has":
        if rest:
            sys.stderr.write(f"usage: pty_sessions.cli {name} has\n")
            return 2
        return _op_has(name)
    if op == "started-at":
        if rest:
            sys.stderr.write(f"usage: pty_sessions.cli {name} started-at\n")
            return 2
        epoch = session_started_at(name)
        if epoch is None:
            return 1
        sys.stdout.write(f"{epoch}\n")
        return 0
    if op == "new":
        return _op_new(name, rest)
    if op == "send":
        return _op_send(name, rest)
    if op == "send_keys":
        return _op_send_keys(name, rest)
    if op == "capture":
        return _op_capture(name, rest)
    if op == "resize":
        return _op_resize(name, rest)
    if op == "kill":
        return _op_kill(name, rest)
    sys.stderr.write(f"unknown op {op!r} for session {name!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
