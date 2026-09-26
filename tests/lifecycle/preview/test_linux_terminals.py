"""Resource continuity needs native receipt, ancestry, and control-peer evidence."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from scripts.preview import linux_terminals as terminals


def _live(_identity: terminals.OwnedProcess) -> bool:
    return True


@pytest.fixture
def receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, terminals.Evidence]:
    directory = tmp_path / "run/pty"
    directory.mkdir(parents=True)
    path = directory / "fixture.json"
    record = {
        "host_pid": 40,
        "host_create_time": 100.0,
        "host_starttime": 400,
        "pid": 41,
        "create_time": 101.0,
        "starttime": 401,
        "generation": "receipt-generation",
        "cwd": str(tmp_path),
    }
    path.write_text(json.dumps(record))
    host = terminals.OwnedProcess(40, 100.0, 400)
    shell = terminals.OwnedProcess(41, 101.0, 401)
    identities = {40: host, 41: shell}

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def cmdline(self) -> list[str]:
            if self.pid == shell.pid:
                return ["/bin/bash", "-l", "-i"]
            return [
                "python",
                "-m",
                "shared.sessions.pty.host",
                "fixture",
                str(tmp_path),
                "/unused-env",
                str(path),
                str(tmp_path / "fixture.sock"),
                "/unused-log",
                "receipt-generation",
            ]

        def ppid(self) -> int:
            return host.pid if self.pid == shell.pid else 1

        def name(self) -> str:
            return "python" if self.pid == host.pid else "bash"

        def cwd(self) -> str:
            return str(tmp_path)

    def capture(process: Process) -> terminals.OwnedProcess:
        return identities[process.pid]

    def live(identity: terminals.OwnedProcess) -> bool:
        return any(identity.same_birth(item) for item in identities.values())

    def tree(_host: terminals.OwnedProcess) -> set[terminals.OwnedProcess]:
        return set(identities.values())

    def ping(*_args: object) -> terminals.Evidence:
        return {"peer_pid": host.pid}

    monkeypatch.setattr(terminals.psutil, "Process", Process)
    monkeypatch.setattr(terminals.OwnedProcess, "capture", capture)
    monkeypatch.setattr(terminals.OwnedProcess, "live", live)
    monkeypatch.setattr(terminals, "capture_tree", tree)
    monkeypatch.setattr(terminals, "_ping", ping)
    return path, record


def test_live_receipt_retains_births_native_facts_and_generation(
    receipt: tuple[Path, terminals.Evidence],
) -> None:
    path, record = receipt
    result: terminals.Evidence = {}
    terminals.observe_terminals(path.parents[2], result)
    observed = result["terminals"]["fixture"]
    assert observed["generation"] == record["generation"]
    assert observed["host"]["starttime"] == 400
    assert observed["shell"]["starttime"] == 401
    assert observed["peer_pid"] == record["host_pid"]
    assert observed["shell_native"]["ppid"] == record["host_pid"]
    assert observed["shell_native"]["argv"] == ["/bin/bash", "-l", "-i"]
    assert observed["shell_native"]["cwd"] == record["cwd"]
    assert len(observed["descendants"]) == 2
    assert terminals.retained_members(result["terminals"], result["terminals"])


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"host_starttime": 999}, "lost its live host"),
        ({"starttime": None}, "exact Linux"),
        ({"generation": "replaced"}, "does not bind this receipt"),
        ({"cwd": "/another"}, "does not bind this receipt"),
    ],
)
def test_receipt_cannot_adopt_reused_pid_or_another_native_host(
    receipt: tuple[Path, terminals.Evidence],
    change: terminals.Evidence,
    error: str,
) -> None:
    path, record = receipt
    path.write_text(json.dumps(record | change))
    result: terminals.Evidence = {}
    with pytest.raises(RuntimeError, match=error):
        terminals.observe_terminals(path.parents[2], result)
    assert result["terminals"] == {}
    assert result["terminal_receipts"]["fixture"]["record"] == record | change


def test_shell_native_parent_must_be_recorded_host(
    receipt: tuple[Path, terminals.Evidence],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = receipt

    def foreign_parent(_process: object) -> int:
        return 999

    monkeypatch.setattr(terminals.psutil.Process, "ppid", foreign_parent)
    with pytest.raises(RuntimeError, match="another native parent"):
        terminals.observe_terminals(path.parents[2], {})


def test_receipt_change_during_control_probe_is_not_accepted(
    receipt: tuple[Path, terminals.Evidence],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, record = receipt

    def change_record(*_args: object) -> terminals.Evidence:
        path.write_text(json.dumps(record | {"generation": "next"}))
        return {"peer_pid": 40}

    monkeypatch.setattr(terminals, "_ping", change_record)
    with pytest.raises(RuntimeError, match="receipt changed"):
        terminals.observe_terminals(path.parents[2], {})


@pytest.mark.parametrize("change", ["new", "generation", "host", "record_sha256"])
def test_manager_stop_rejects_unrecorded_or_replaced_resource(
    receipt: tuple[Path, terminals.Evidence],
    change: str,
) -> None:
    path, _ = receipt
    result: terminals.Evidence = {}
    terminals.observe_terminals(path.parents[2], result)
    current = result["terminals"]
    previous: terminals.Evidence = json.loads(json.dumps(current))
    if change == "new":
        previous = {}
    else:
        previous["fixture"][change] = "another generation or birth"
    with pytest.raises(RuntimeError, match=r"pre-stop receipt|identity changed"):
        terminals.retained_members(current, previous)


def test_missing_receipt_cannot_hide_live_shell_even_after_cwd_escape(
    receipt: tuple[Path, terminals.Evidence],
) -> None:
    path, _ = receipt
    result: terminals.Evidence = {}
    terminals.observe_terminals(path.parents[2], result)
    with pytest.raises(RuntimeError, match="lost its resource custody"):
        terminals.retained_members({}, result["terminals"])


def test_naturally_completed_terminal_need_not_survive_manager_stop(
    receipt: tuple[Path, terminals.Evidence],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = receipt
    result: terminals.Evidence = {}
    terminals.observe_terminals(path.parents[2], result)

    def closed(_identity: terminals.OwnedProcess) -> bool:
        return False

    monkeypatch.setattr(terminals.OwnedProcess, "live", closed)
    assert terminals.retained_members({}, result["terminals"]) == set()


@pytest.mark.parametrize("foreign_peer", [False, True])
def test_control_peer_is_kernel_bound_before_ping(
    monkeypatch: pytest.MonkeyPatch,
    foreign_peer: bool,
    tmp_path: Path,
) -> None:
    host = terminals.OwnedProcess(40, 100.0, 400)
    shell = terminals.OwnedProcess(41, 101.0, 401)
    sent: list[bytes] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _path: str) -> None:
            pass

        def getsockopt(self, *_args: object) -> bytes:
            return struct.pack("3i", 999 if foreign_peer else host.pid, 1000, 1000)

        def sendall(self, raw: bytes) -> None:
            sent.append(raw)

        def recv(self, _limit: int) -> bytes:
            return (
                json.dumps(
                    {"ok": True, "code": 0, "data": {"pid": shell.pid, "host_pid": host.pid}}
                ).encode()
                + b"\n"
            )

    def connection(*_args: object) -> Connection:
        return Connection()

    monkeypatch.setattr(terminals.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(terminals.socket, "socket", connection)
    monkeypatch.setattr(terminals.OwnedProcess, "live", _live)
    if foreign_peer:
        with pytest.raises(RuntimeError, match="another native host"):
            terminals._ping(tmp_path / "unused", host, shell)
        assert not sent
    else:
        assert terminals._ping(tmp_path / "unused", host, shell)["peer_pid"] == host.pid
        assert sent == [b'{"op":"ping"}\n']


def test_terminal_recapture_keeps_native_custody_across_clock_shift(
    receipt: tuple[Path, terminals.Evidence], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = receipt
    original = terminals.capture_tree

    def moved(host: terminals.OwnedProcess) -> set[terminals.OwnedProcess]:
        return {
            terminals.OwnedProcess(item.pid, item.birth + 3600, item.starttime)
            for item in original(host)
        }

    monkeypatch.setattr(terminals, "capture_tree", moved)
    result: terminals.Evidence = {}
    terminals.observe_terminals(path.parents[2], result)
    assert terminals.retained_members(result["terminals"], result["terminals"])
