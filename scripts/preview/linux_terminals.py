"""Read-only native receipts for durable Linux preview terminals.

Session listing APIs can reap records and processes, so this observer reads the
receipts directly. A live terminal requires its recorded host, shell, and bound
control peer to agree. It never adopts a process by its name or command alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import struct
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil

from shared.native_process.ownership import OwnedProcess, capture_tree

Evidence = dict[str, Any]
STABLE_FIELDS = ("host", "shell", "generation", "record_sha256")


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def process_observation(process: psutil.Process) -> Evidence:
    """Retain native facts even when a short-lived process exits during capture."""
    row: Evidence = {"pid": process.pid}
    try:
        identity = OwnedProcess.capture(process)
        row["identity"] = asdict(identity)
        row.update(
            name=process.name(), argv=process.cmdline(), cwd=process.cwd(), ppid=process.ppid()
        )
        row["identity_live"] = identity.live()
    except psutil.NoSuchProcess:
        row["exited_during_observation"] = True
    return row


def _identity(record: Evidence, *, host: bool) -> OwnedProcess:
    pid, birth, ticks = (
        (record["host_pid"], record["host_create_time"], record["host_starttime"])
        if host
        else (record["pid"], record["create_time"], record["starttime"])
    )
    _require(
        type(pid) is int and pid > 0 and type(ticks) is int and ticks > 0,
        "terminal receipt lacks exact Linux PID/start ticks",
    )
    _require(
        type(birth) in (int, float) and math.isfinite(birth) and birth > 0,
        "terminal receipt lacks a native birth timestamp",
    )
    return OwnedProcess(pid, float(birth), ticks)


def _control_path(path: Path, record: Evidence, host: OwnedProcess) -> Path:
    argv = psutil.Process(host.pid).cmdline()
    _require(
        len(argv) in (10, 11) and argv[1:3] == ["-m", "shared.sessions.pty.host"],
        "recorded terminal host has another native command",
    )
    _require(
        argv[3] == path.stem
        and argv[4] == record["cwd"]
        and argv[6] == str(path)
        and argv[9] == (record["generation"] or ""),
        "terminal host command does not bind this receipt",
    )
    # The host argv contains the actual socket, including the long-home hashed
    # path. Do not invoke _paths.socket_path(), which creates directories.
    return Path(argv[7])


def _read_reply(connection: socket.socket) -> Evidence:
    deadline = time.monotonic() + 2.0
    raw = bytearray()
    while b"\n" not in raw:
        remaining = deadline - time.monotonic()
        _require(remaining > 0 and len(raw) < 65536, "terminal ping exceeded its read budget")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, 65536 - len(raw)))
        _require(chunk, "terminal control closed before replying")
        raw.extend(chunk)
    reply: Evidence = json.loads(raw.split(b"\n", 1)[0])
    _require(isinstance(reply, dict), "terminal ping did not return an object")
    return reply


def _ping(path: Path, host: OwnedProcess, shell: OwnedProcess) -> Evidence:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2.0)
        connection.connect(str(path))
        peer_option = int(getattr(socket, "SO_PEERCRED"))  # noqa: B009 — Linux constant absent from macOS type stubs
        peer_pid, peer_uid, peer_gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, peer_option, struct.calcsize("3i")),
        )
        _require(
            peer_pid == host.pid and host.live(), "terminal socket belongs to another native host"
        )
        connection.sendall(b'{"op":"ping"}\n')
        reply = _read_reply(connection)
    _require(
        reply["ok"] is True
        and reply["code"] == 0
        and reply["data"] == {"pid": shell.pid, "host_pid": host.pid},
        "terminal ping does not confirm the recorded host and shell",
    )
    return {"peer_pid": peer_pid, "peer_uid": peer_uid, "peer_gid": peer_gid, "reply": reply}


def _descendants(host: OwnedProcess, shell: OwnedProcess) -> list[Evidence]:
    members = capture_tree(host)
    _require(
        shell.birth_key() in {item.birth_key() for item in members},
        "recorded shell is outside its terminal host tree",
    )
    rows: list[Evidence] = []
    for identity in sorted(members, key=lambda member: member.pid):
        try:
            row = process_observation(psutil.Process(identity.pid))
        except psutil.NoSuchProcess:
            continue
        if not row.get("identity_live", False):
            continue
        _require(
            identity.same_birth(OwnedProcess(**row["identity"])),
            "terminal descendant PID changed during capture",
        )
        rows.append(row)
    return rows


def _observe_receipt(path: Path, raw: bytes, row: Evidence) -> bool:
    record: Evidence = json.loads(raw)
    row.update(record=record, record_sha256=hashlib.sha256(raw).hexdigest())
    _require(re.fullmatch(r"[a-z][a-z0-9-]*", path.stem), "invalid terminal receipt name")
    generation = record["generation"]
    _require(generation is None or isinstance(generation, str), "invalid terminal generation")
    host, shell = _identity(record, host=True), _identity(record, host=False)
    row.update(host=asdict(host), shell=asdict(shell), generation=generation)
    host_live, shell_live = host.live(), shell.live()
    if not host_live and not shell_live:
        row["state"] = "closed"
        return False
    _require(host_live and shell_live, "terminal receipt has lost its live host or shell")
    row["host_native"] = process_observation(psutil.Process(host.pid))
    row["shell_native"] = process_observation(psutil.Process(shell.pid))
    _require(row["shell_native"]["ppid"] == host.pid, "terminal shell has another native parent")
    row["socket"] = str(control := _control_path(path, record, host))
    row.update(_ping(control, host, shell))
    row["descendants"] = _descendants(host, shell)
    _require(
        host.live() and shell.live() and psutil.Process(shell.pid).ppid() == host.pid,
        "terminal owner changed during observation",
    )
    _require(path.read_bytes() == raw, "terminal receipt changed during observation")
    row["state"] = "live"
    return True


def observe_terminals(home: Path, result: Evidence) -> None:
    """Fill live terminals and every examined receipt, retaining failed evidence."""
    terminals: Evidence = {}
    receipts: Evidence = {}
    result.update(terminals=terminals, terminal_receipts=receipts)
    for path in sorted((home / "run/pty").glob("*.json")):
        row: Evidence = {}
        receipts[path.stem] = row
        _require(not path.is_symlink(), "terminal receipt is a symbolic link")
        if _observe_receipt(path, path.read_bytes(), row):
            terminals[path.stem] = row


def retained_members(current: Evidence, previous: Evidence) -> set[OwnedProcess]:
    """Authorize only current trees of unchanged, previously observed resources."""
    allowed: set[OwnedProcess] = set()
    for name, row in current.items():
        _require(name in previous, f"terminal lacks a pre-stop receipt: {name}")
        _require(
            all(row[key] == previous[name][key] for key in STABLE_FIELDS),
            f"terminal resource identity changed: {name}",
        )
        host = OwnedProcess(**row["host"])
        shell = OwnedProcess(**row["shell"])
        members = capture_tree(host)
        _require(
            host.live()
            and shell.live()
            and shell.birth_key() in {item.birth_key() for item in members},
            f"terminal custody was lost: {name}",
        )
        allowed.update(members)
    for name, row in previous.items():
        for identity in recorded_members(row):
            _require(
                not identity.live()
                or identity.birth_key() in {item.birth_key() for item in allowed},
                f"captured terminal process lost its resource custody: {name}, PID {identity.pid}",
            )
    return allowed


def recorded_members(row: Evidence) -> set[OwnedProcess]:
    """All exact births already observed under one resource's custody."""
    return {
        OwnedProcess(**row["host"]),
        OwnedProcess(**row["shell"]),
        *(OwnedProcess(**member["identity"]) for member in row["descendants"]),
    }
