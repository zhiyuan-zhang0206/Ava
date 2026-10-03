"""PTY sessions CLI — the transport contract the SDK (W1b) consumes.

Invocation shape::

    python -m base.sessions.pty.cli <name> <op> [args]
    python -m base.sessions.pty.cli list [prefix]
    python -m base.sessions.pty.cli list-started-at [prefix]

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
- ``kill <name> [--graceful]``     kill every process of the session — the
                            shell's tree and its POSIX session
                            (``session_tree``; graceful: SIGTERM first);
                            idempotent noop. Falls back to a record-based
                            kill of the same membership when the host
                            itself is wedged, so a kill is authoritative
                            even against a broken host. Fails naming any
                            process that survived. On success prints
                            `interrupted` when the session carried live
                            processes (a foreground/background job, a
                            double-forked orphan) at kill time, `idle`
                            otherwise — the TTL reaper's interrupt verdict
                            (a record-based kill of a wedged host answers
                            `interrupted`, fail-open).
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
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

import psutil

from base.log import logger
from base.native_process import pid_starttime_ticks
from base.native_process.os_platform import LockTimeoutError
from base.native_process.ownership import OwnedProcess, stable_create_time
from base.paths import run_dir
from base.sessions.pty._paths import (
    CAPTURE_MAX_LINES,
    host_identity,
    host_log_path,
    host_starttime,
    record_path,
    socket_path,
    transcript_path,
)
from base.sessions.pty.allocation_freeze import locked_freeze_state, state_path
from base.sessions.pty.orphan_reaper import _reap_orphaned_hosts
from base.sessions.pty.records import (
    _CREATE_TIME_TOLERANCE_S,
    _record_alive,
    _sweep_dead,
    has_session,
    live_sessions,
    session_generation,
    session_started_at,
)
from base.sessions.pty.session_tree import TreeKill, kill_host_tree, kill_session_tree
from base.sessions.record import SessionRecord

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

# What a POSIX shell can assign to (mirrors base.sessions.env_forwarding): keys outside
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

    Same on-disk format as ``base.sessions.backend._write_session_env_file`` (sorted
    KEY=shlex-quoted lines), so files from either writer load in the host; only
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
# ---------------------------------------------------------------------------
# Record liveness + enumeration: see base.sessions.pty.records (split out
# 2026-09-09, task #2670 — the file-size ceiling). The CLI re-exports the
# same names from the module-level import above.
# ---------------------------------------------------------------------------
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

# How long a kill the CLI performs itself (a wedged host, a failed spawn)
# waits for its SIGKILLed processes to exit before reporting survivors.
_RECORD_KILL_WAIT_S = 3.0


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
    log = host_log_path(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    host_argv = [
        sys.executable,
        "-m",
        "base.sessions.pty.host",
        name,
        cwd,
        envfile,
        str(record_path(name)),
        str(socket_path(name)),
        str(transcript_path(name)),
        generation or "",
    ]
    host_argv.extend([cmd_b64] if cmd_b64 else [])
    from base.sessions.backend import helper_spawn_enabled

    if helper_spawn_enabled():
        try:
            from base.native_process import child_env
            from base.sessions import helperproc

            host_pid = helperproc.spawn_via_helper(
                f"pty-host-{name}",
                host_argv,
                Path(cwd),
                env=child_env.inherited_process_env(),
                stdout=log,
                stderr=log,
            )
        except (OSError, RuntimeError) as exc:
            sys.stderr.write(f"cannot spawn pty session host for {name}: {exc}\n")
            return 1
    else:
        argv = [sys.executable, "-m", "base._reparent", str(log), str(log), *host_argv]
        try:
            spawned = subprocess.run(argv, capture_output=True, timeout=30.0, check=True)  # noqa: S603 — argv is repo-internal (interpreter + module names + validated session args)
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(f"cannot spawn pty session host for {name}: {exc}\n")
            return 1
        try:
            host_pid = int(spawned.stdout.strip())
        except ValueError:
            sys.stderr.write(f"cannot establish pty host identity for {name}: {spawned.stdout!r}\n")
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
    record after a concurrent freeze acknowledged. The host is frozen first so
    it cannot fork more work, its shell's whole session is killed
    (`session_tree`), then the host, and only provably dead session artifacts
    are removed.
    """
    survivors: list[int] = []
    with contextlib.suppress(psutil.NoSuchProcess):
        result = kill_host_tree(psutil.Process(host_pid), wait_s=_RECORD_KILL_WAIT_S)
        survivors = sorted(identity.pid for identity in result.survivors)
    if survivors:
        sys.stderr.write(f"failed pty allocation for {name} left live process(es): {survivors}\n")
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
            # Keep the allocation lock until ready. Once a concurrent freeze
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
    lines: int | None = None
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
    if lines is None:
        # Lazy import: this CLI is spawned on the capture hot path, so the
        # config stack must not load unless the omitted argument needs it.
        # Every programmatic caller passes an explicit window; only a bare
        # `capture` reaches this branch.
        from base.config import settings

        lines = settings.display.shell_capture_default_lines
    if lines < 1 or lines > CAPTURE_MAX_LINES:
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


def _kill_recorded_host(name: str) -> None:
    """SIGKILL the record's host pid, identity-checked against its start time."""
    identity = host_identity(record_path(name))
    if identity is None:
        return
    host_pid, host_create = identity
    with contextlib.suppress(psutil.Error):
        proc = psutil.Process(host_pid)
        recorded_starttime = host_starttime(record_path(name))
        matches = (
            pid_starttime_ticks(host_pid) == recorded_starttime
            if recorded_starttime is not None
            else abs(stable_create_time(proc) - host_create) <= _CREATE_TIME_TOLERANCE_S
        )
        if proc.is_running() and matches:
            proc.kill()


def _kill_by_record(name: str) -> int:
    """Kill a session whose host is not answering, straight from its record.

    The host owns the orderly kill; this fallback keeps `kill` authoritative
    against a wedged or SIGKILLed host: kill the same membership the host's
    kill takes (`session_tree` — the shell's tree and POSIX session), then the
    host pid, each identity-checked against its recorded start time so a
    recycled pid is never signalled; then sweep the record + socket only when
    they are provably stale.
    """
    path = record_path(name)
    rec = SessionRecord.read(path)
    result = TreeKill((), ())
    if rec is not None and _record_alive(rec, path):
        shell = OwnedProcess(rec.pid, rec.create_time, rec.starttime)
        result = kill_session_tree(shell, wait_s=_RECORD_KILL_WAIT_S)
    _kill_recorded_host(name)
    deadline = time.monotonic() + _RECORD_KILL_WAIT_S
    while time.monotonic() < deadline:
        rec = SessionRecord.read(path)
        if rec is None or not _record_alive(rec, path):
            break
        time.sleep(0.05)
    if has_session(name):
        sys.stderr.write(f"session {name} survived the record-based kill\n")
        return 1
    if result.stuck:
        survivors = _pids(result.stuck)
        sys.stderr.write(f"session {name}: processes survived the record-based kill: {survivors}\n")
        return 1
    _report_denied(name, _pids(result.denied))
    sys.stdout.write("interrupted\n")  # fail-open: a wedged host's kill could not be probed
    _sweep_dead(name)
    try:
        _reap_orphaned_hosts(name, force_unresponsive=True)
    except RuntimeError as exc:
        sys.stderr.write(f"session {name} left an orphaned host after record-based kill: {exc}\n")
        return 1
    return 0


def _pids(identities: tuple[OwnedProcess, ...]) -> list[int]:
    return sorted(identity.pid for identity in identities)


def _report_denied(name: str, pids: list[int] | None) -> None:
    """Name the processes a finished kill had no permission to signal.

    The session itself is gone (its shell and everything this user may
    signal), so the kill succeeds; the leftovers belong to another user (a
    root `sudo` on the pty) and are that user's to end.
    """
    if pids:
        sys.stderr.write(
            f"session {name}: processes this user may not signal outlived the kill: {pids}\n"
        )


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
    _report_denied(name, resp["data"].get("survivors"))
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
    """Dispatch one CLI invocation. `python -m base.sessions.pty.cli`."""
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
    return _run_session_op(name, args[1], args[2:])


def _run_session_op(name: str, op: str, rest: list[str]) -> int:
    """Run one `<name> <op> [args]` invocation."""
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
    handlers = {
        "new": _op_new,
        "send": _op_send,
        "send_keys": _op_send_keys,
        "capture": _op_capture,
        "resize": _op_resize,
        "kill": _op_kill,
    }
    handler = handlers.get(op)
    if handler is None:
        sys.stderr.write(f"unknown op {op!r} for session {name!r}\n")
        return 2
    return handler(name, rest)


if __name__ == "__main__":
    raise SystemExit(main())
