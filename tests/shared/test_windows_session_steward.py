"""The steward's decision surface and its control loop, asserted on any platform.

The loop is loopback TCP now precisely so it can run here: the old filesystem-
socket transport could not bind on Windows at all (CPython issue #77589 —
AF_UNIX is simply absent there), so the loop had never executed on any CI box.
These tests bind, listen, and exchange a real token, and pin the exit truth
table. Only the console helper stays Windows-only; its invocation shape is
pinned separately.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from shared.windows_session_steward import should_exit


@pytest.mark.parametrize(
    ("record_names_me", "target_alive", "record_exists", "expected"),
    [
        # Serve only while the record names this identity and the target lives.
        (True, True, True, False),
        (True, False, True, True),  # graceful stop landed — leave
        (True, False, False, True),
        (True, True, False, False),  # spawn race: record not written yet
        # A record naming a different identity always ends this steward.
        (False, True, True, True),  # same-name restart
        (False, False, True, True),  # reap wrote a new record
        (False, True, False, False),  # record gone while target alive: spawn race
        (False, False, False, True),
    ],
)
def test_should_exit_truth_table(
    record_names_me: bool, target_alive: bool, record_exists: bool, expected: bool
) -> None:
    assert (
        should_exit(
            record_names_me=record_names_me,
            target_alive=target_alive,
            record_exists=record_exists,
        )
        is expected
    )


def test_deliver_break_runs_the_verified_helper_with_the_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from shared import windows_session_steward as steward

    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(steward.subprocess, "run", run)
    monkeypatch.setattr(steward, "sys", SimpleNamespace(executable="/venv/python.exe"))
    accepted, detail = steward._deliver_break(Path("/home/run/sessions/s.json"), 42, 7.5)
    assert accepted and detail == "accepted"
    assert calls[0][:4] == [
        "/venv/python.exe",
        "-I",
        str(steward._HELPER),
        "/home/run/sessions/s.json",
    ]
    assert calls[0][4:6] == ["42", "7.5"]
    # A bounded deadline, passed as epoch-after-start to the helper.
    assert float(calls[0][6]) > 0


def test_deliver_break_reports_the_helpers_refusal_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from shared import windows_session_steward as steward

    def run(_argv: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr="private console delivery refused: nope\n")

    monkeypatch.setattr(steward.subprocess, "run", run)
    accepted, detail = steward._deliver_break(Path("/s.json"), 42, 7.5)
    assert not accepted
    assert "nope" in detail


# ── the control loop itself: loopback TCP + delivery token ──────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve_in_thread(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record_path: Path,
    port: int,
    nonce: str,
    alive: dict[str, bool],
    delivered: list[str],
) -> tuple[threading.Thread, dict[str, int]]:
    from shared import windows_session_steward as steward

    monkeypatch.setattr(steward.sys, "platform", "win32")
    monkeypatch.setattr(steward, "_POLL_S", 0.05)

    def _matches(*_args: object, **_kwargs: object) -> bool:
        return True

    def _alive(*_args: object, **_kwargs: object) -> bool:
        return alive["flag"]

    monkeypatch.setattr(steward, "_record_matches", _matches)
    monkeypatch.setattr(steward, "_target_alive", _alive)

    def _deliver(_record_path: Path, _pid: int, _birth: float) -> tuple[bool, str]:
        delivered.append(nonce)
        return True, "accepted"

    monkeypatch.setattr(steward, "_deliver_break", _deliver)
    result: dict[str, int] = {}

    def _run() -> None:
        result["code"] = steward.serve(record_path, 42, 7.5, port, nonce, "zz")

    thread = threading.Thread(target=_run)
    thread.start()
    return thread, result


def _wait_serving(port: int, *, deadline: float = 5.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.02)
    pytest.fail("steward never started accepting")


def _exchange(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
        conn.sendall(payload)
        return conn.recv(512)


def _stop(thread: threading.Thread, result: dict[str, int], alive: dict[str, bool]) -> None:
    alive["flag"] = False
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["code"] == 0


def test_control_loop_delivers_with_the_record_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port, nonce = _free_port(), "ab" * 16
    alive: dict[str, bool] = {"flag": True}
    delivered: list[str] = []
    thread, result = _serve_in_thread(
        monkeypatch,
        record_path=tmp_path / "record.json",
        port=port,
        nonce=nonce,
        alive=alive,
        delivered=delivered,
    )
    _wait_serving(port)
    reply = _exchange(port, f"break {nonce}\n".encode())
    assert reply.startswith(b"ok")
    assert delivered == [nonce]
    _stop(thread, result, alive)


def test_control_loop_refuses_a_bad_or_missing_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port, nonce = _free_port(), "ab" * 16
    alive: dict[str, bool] = {"flag": True}
    delivered: list[str] = []
    thread, result = _serve_in_thread(
        monkeypatch,
        record_path=tmp_path / "record.json",
        port=port,
        nonce=nonce,
        alive=alive,
        delivered=delivered,
    )
    _wait_serving(port)
    assert _exchange(port, b"break\n").startswith(b"err")
    assert _exchange(port, b"break " + b"ff" * 16 + b"\n").startswith(b"err")
    assert delivered == []
    _stop(thread, result, alive)


def test_control_loop_survives_a_dropped_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port, nonce = _free_port(), "ab" * 16
    alive: dict[str, bool] = {"flag": True}
    delivered: list[str] = []
    thread, result = _serve_in_thread(
        monkeypatch,
        record_path=tmp_path / "record.json",
        port=port,
        nonce=nonce,
        alive=alive,
        delivered=delivered,
    )
    _wait_serving(port)  # this probe itself connects and vanishes
    assert _exchange(port, f"break {nonce}\n".encode()).startswith(b"ok")
    _stop(thread, result, alive)


def test_control_loop_runs_without_af_unix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The transport must not depend on AF_UNIX: Windows CPython lacks it
    (CPython issue #77589), which is what killed the filesystem-socket design
    this replaced. Hiding the attribute changes nothing."""
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    port, nonce = _free_port(), "ab" * 16
    alive: dict[str, bool] = {"flag": True}
    delivered: list[str] = []
    thread, result = _serve_in_thread(
        monkeypatch,
        record_path=tmp_path / "record.json",
        port=port,
        nonce=nonce,
        alive=alive,
        delivered=delivered,
    )
    _wait_serving(port)
    assert _exchange(port, f"break {nonce}\n".encode()).startswith(b"ok")
    _stop(thread, result, alive)


def test_control_loop_bind_conflict_exits_with_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A port claimed between reservation and bind must not crash the steward:
    it logs and exits, and every later delivery degrades to the designed
    refusal."""
    from shared import windows_session_steward as steward

    def _alive(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(steward.sys, "platform", "win32")
    monkeypatch.setattr(steward, "_target_alive", _alive)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    try:
        code = steward.serve(tmp_path / "record.json", 42, 7.5, port, "a" * 32, "zz")
    finally:
        blocker.close()
    assert code == 1
