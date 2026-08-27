"""End-to-end tests for shared.pty_sessions — the CLI contract against REAL
detached per-session hosts and REAL pty shells (bash -l -i).

There is no supervisor daemon: each `new` spawns the session's own host
process, double-forked to init. Each test runs under its own tmp $AVA_HOME
(the unit_home fixture); the sessions fixture kills every session still
alive under that home at teardown — hosts detach from the test process, so
an unkilled session would outlive the test run. All CLI invocations are real
subprocesses with AVA_HOME pinned, exactly the shape the SDK uses.

These are POSIX-only (pty.fork + bash).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from shared.platform import IS_LINUX, IS_WINDOWS
from shared.pty_sessions import cli as pty_cli
from shared.pty_sessions import host as pty_host
from shared.pty_sessions._paths import host_identity, record_path, socket_path
from shared.pty_sessions.cli import write_env_file
from shared.pty_sessions.host import PtySession, _parse_request
from shared.session_record import SessionRecord, pid_starttime_ticks

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only")

REPO = Path(__file__).resolve().parents[2]


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI exactly as the SDK will: a subprocess, AVA_HOME pinned."""
    return subprocess.run(  # noqa: S603 — repo-internal argv; check=False, rc asserted by callers
        [sys.executable, "-m", "shared.pty_sessions.cli", *args],
        cwd=REPO,
        env={**os.environ, "AVA_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wait(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    # 30s default (2026-08-09): under CI runner CPU oversubscription a host's
    # reader thread can lag its reap well past 10s.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()  # pyright: ignore[reportUnknownVariableType]


@pytest.fixture
def sessions(unit_home: Path) -> Iterator[Path]:
    """The test home; every session still alive under it is killed after.

    Hosts are detached to init — a leaked one would outlive the whole test
    run, so the teardown kill is load-bearing, not hygiene.
    """
    yield unit_home
    # In-process (no subprocess.run): a test may leave a global
    # subprocess.run monkeypatch in place at teardown time.
    from shared.pty_sessions import cli as pty_cli

    for name in list(pty_cli.live_sessions()):
        try:
            pty_cli.session_request(name, {"op": "kill"})
        except OSError:
            pty_cli._kill_by_record(name)


def _new(
    home: Path, name: str, *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    envfile = write_env_file(env or {})
    result = _run_cli(home, name, "new", str(cwd or home), str(envfile))
    assert result.returncode == 0, f"new {name} failed: {result.stderr}"
    assert not envfile.exists(), "envfile must be consumed by the host"


def _send(home: Path, name: str, text: str, *, enter: bool = True) -> None:
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    result = _run_cli(home, name, "send", b64)
    assert result.returncode == 0, f"send failed: {result.stderr}"
    if enter:
        _keys(home, name, "Enter")


def _keys(home: Path, name: str, *keys: str) -> None:
    result = _run_cli(home, name, "send_keys", *keys)
    assert result.returncode == 0, f"send_keys failed: {result.stderr}"


def _capture(home: Path, name: str, *, lines: int = 200, scrollback: bool = True) -> str:
    args = ["capture", str(lines)]
    if not scrollback:
        args.append("--no-scrollback")
    result = _run_cli(home, name, *args)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    return result.stdout


def _capture_until(home: Path, name: str, needle: str, *, scrollback: bool = True) -> str:
    def _seen() -> bool:
        return needle in _capture(home, name, scrollback=scrollback)

    assert _wait(_seen, timeout=10.0), f"capture never contained {needle!r}"
    return _capture(home, name, scrollback=scrollback)


def _output_until(home: Path, name: str, word: str, *, timeout: float = 10.0) -> str:
    """Wait until a BARE output line equal to `word` appears in the capture.

    The typed command line is echoed onto the screen, so a substring wait is
    satisfied by the echo before the command has run (a flake on loaded
    boxes). Waiting for the bare line — the command's own output — pins the
    shell to "command executed".
    """

    def _seen() -> bool:
        return word in [ln.strip() for ln in _capture(home, name).split("\n")]

    assert _wait(_seen, timeout=timeout), f"output line {word!r} never appeared"
    return _capture(home, name)


def _has(home: Path, name: str) -> bool:
    return _run_cli(home, name, "has").returncode == 0


def _kill(home: Path, name: str, *, graceful: bool = False) -> int:
    args = ["kill"] + (["--graceful"] if graceful else [])
    result = _run_cli(home, name, *args)
    assert result.returncode == 0, f"kill failed: {result.stderr}"
    return result.returncode


def _record(home: Path, name: str) -> SessionRecord | None:
    del home  # the record path resolver reads AVA_HOME via settings, pinned by unit_home
    return SessionRecord.read(record_path(name))


def _make_session(home: Path, name: str, *, log_cap: int = 1024) -> PtySession:
    """A PtySession in isolation (no host process): a real fd for the master
    and a throwaway record — for unit-testing teardown claim + transcript cap."""
    r, w = os.pipe()
    os.close(w)
    rec = SessionRecord(pid=os.getpid(), create_time=0.0, cmd="", cwd="", started_at=0.0)
    return PtySession(
        name,
        os.getpid(),
        r,
        120,
        40,
        rec,
        home / f"{name}.json",
        home / f"{name}.out.log",
        log_cap=log_cap,
    )


def test_begin_finish_has_single_winner(tmp_path: Path) -> None:
    """Concurrent teardown claims must have exactly one winner: a double
    winner would double-close the master fd (the _finish race class)."""
    s = _make_session(tmp_path, "ava-test-win-1")
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(s.begin_finish())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, results
    assert s.dead
    assert not s.begin_finish(), "later claims must lose"


def test_transcript_log_is_capped(tmp_path: Path) -> None:
    """The byte transcript stops growing at the per-session cap (the D6
    unbounded-growth guard): past the cap the host keeps the session but
    stops appending."""
    s = _make_session(tmp_path, "ava-test-logcap-1", log_cap=100)
    s.log_write(b"a" * 60)
    s.log_write(b"b" * 60)
    log_file = tmp_path / "ava-test-logcap-1.out.log"
    assert len(log_file.read_bytes()) == 100
    assert log_file.read_bytes().startswith(b"a" * 60)
    s.log_write(b"c" * 50)
    assert s._log_written == 100
    assert len(log_file.read_bytes()) == 100, "log must not grow past the cap"


def test_host_startup_leaves_log_retention_to_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "logs"
    transcript = logs / "ava-test-retention-startup.out.log"
    logs.mkdir()
    transcript.write_text("old transcript", encoding="utf-8")
    old_mtime = time.time() - 15 * 24 * 60 * 60
    os.utime(transcript, (old_mtime, old_mtime))

    def _ignore_call(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(pty_host.signal, "signal", _ignore_call)
    monkeypatch.setattr(pty_host.logger, "add", _ignore_call)

    result = pty_host.main(
        [
            "ava-test-retention-startup",
            str(tmp_path),
            str(tmp_path / "missing.env.sh"),
            str(tmp_path / "record.json"),
            str(tmp_path / "session.sock"),
            str(transcript),
        ]
    )

    assert result == 1
    assert transcript.exists()
    assert not (logs / ".transcript-retention.stamp").exists()


def test_parse_request_rejects_non_object() -> None:
    """A valid-JSON non-object request (list / string / op-less dict) must
    raise so the host answers `bad request` instead of dying silently."""
    with pytest.raises(TypeError):
        _parse_request(b'["kill", "x"]')
    with pytest.raises(TypeError):
        _parse_request(b'"ping"')
    with pytest.raises(TypeError):
        _parse_request(b'{"noop": 1}')
    req = _parse_request(b'{"op": "ping"}')
    assert req["op"] == "ping"


# ---------------------------------------------------------------------------
# THE persistence invariant — the reason per-session hosts exist
# ---------------------------------------------------------------------------


def test_host_is_detached_to_init(sessions: Path) -> None:
    """The session host must be reparented to init (ppid 1): it is nobody's
    child, so NO infra process tree — an update's service stop, `ava stop`'s
    force kill, a watchdog respawn — can reach it by killing a parent. This
    single assertion is what makes "sessions persist across
    terminate/restart/update" structural rather than aspirational."""
    home = sessions
    name = "ava-test-detach-1"
    _new(home, name)
    identity = host_identity(record_path(name))
    assert identity is not None, "record must carry the host identity"
    host_pid, _ = identity
    assert psutil.Process(host_pid).ppid() == 1, "host must be reparented to init"
    # Every process that took part in creating it is already gone (the CLI
    # subprocess exited); the session still answers.
    _send(home, name, "echo survived-creators")
    _output_until(home, name, "survived-creators")


def test_session_survives_a_sigterm_storm(sessions: Path) -> None:
    """A stray SIGTERM/SIGHUP aimed at the host — the class of signal every
    infra teardown broadcasts — must not take the session down. Ending a
    session is the kill op's job; only SIGKILL (or its shell exiting) ends a
    host."""
    home = sessions
    name = "ava-test-storm-1"
    _new(home, name)
    identity = host_identity(record_path(name))
    assert identity is not None
    host_pid, _ = identity
    os.kill(host_pid, signal.SIGTERM)
    os.kill(host_pid, signal.SIGHUP)
    time.sleep(0.5)
    assert psutil.pid_exists(host_pid), "host must ignore TERM/HUP"
    assert _has(home, name)
    _send(home, name, "echo survived-signals")
    _output_until(home, name, "survived-signals")


def test_crashed_host_is_swept_lazily(sessions: Path) -> None:
    """SIGKILL on a host (the one thing that ends it uncleanly) hangs up its
    shell and leaves the record behind; the next enumeration must sweep the
    dead record + socket instead of listing a ghost."""
    home = sessions
    name = "ava-test-crash-1"
    _new(home, name)
    identity = host_identity(record_path(name))
    assert identity is not None
    host_pid, _ = identity
    rec = _record(home, name)
    assert rec is not None
    os.kill(host_pid, signal.SIGKILL)
    assert _wait(lambda: not psutil.pid_exists(rec.pid)), "shell must HUP when its host dies"
    assert not _has(home, name), "a dead shell reads dead regardless of the leftover record"
    assert _run_cli(home, "list").stdout.strip() == ""
    assert not record_path(name).exists(), "list must sweep the dead record"
    assert not socket_path(name).exists(), "list must sweep the dead socket"


# ---------------------------------------------------------------------------
# has / list / list-started-at
# ---------------------------------------------------------------------------


def test_has_reflects_record_liveness(sessions: Path) -> None:
    home = sessions
    name = "ava-test-has-1"
    assert _run_cli(home, name, "has").returncode == 1  # never created
    _new(home, name)
    assert _has(home, name)
    _kill(home, name)
    assert not _has(home, name)


def test_list_started_at_returns_every_session_in_one_call(sessions: Path) -> None:
    """The batch op answers every live session's launch epoch in one record
    scan — the status snapshot's fast path."""
    home = sessions
    names = ["ava-test-batch-1", "ava-test-batch-2"]
    for name in names:
        _new(home, name)

    result = _run_cli(home, "list-started-at")
    assert result.returncode == 0
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if line.strip())
    for name in names:
        assert name in lines, f"{name} missing from batch response"
        assert float(lines[name]) > 0
    result = _run_cli(home, "list-started-at", "ava-test-batch-1")
    assert result.returncode == 0
    assert result.stdout.splitlines()[0].startswith("ava-test-batch-1 ")
    assert len(result.stdout.splitlines()) == 1


def test_list_filters_by_prefix(sessions: Path) -> None:
    home = sessions
    a = "ava-test-list-a-1"
    b = "ava-test-list-b-1"
    _new(home, a)
    _new(home, b)
    out = _run_cli(home, "list").stdout.splitlines()
    assert a in out and b in out
    only_a = _run_cli(home, "list", "ava-test-list-a-").stdout.splitlines()
    assert only_a == [a]


def test_has_defeats_pid_reuse(sessions: Path) -> None:
    """A record whose pid was recycled onto an unrelated process must read
    dead unless the start-time matches — the pid-reuse protection."""
    home = sessions
    name = "ava-test-reuse-1"
    _new(home, name)
    _kill(home, name)

    rec_path = record_path(name)
    live = subprocess.Popen(["/bin/sleep", "300"])
    try:
        forged = SessionRecord(pid=live.pid, create_time=0.0, cmd="", cwd="", started_at=0.0)
        forged.write(rec_path)
        assert _run_cli(home, name, "has").returncode == 1, "wrong start-time must read dead"

        real = SessionRecord(
            pid=live.pid,
            create_time=psutil.Process(live.pid).create_time(),
            cmd="",
            cwd="",
            started_at=0.0,
        )
        real.write(rec_path)
        assert _run_cli(home, name, "has").returncode == 0, "matching start-time reads alive"
    finally:
        live.kill()
        live.wait(timeout=5)
        with contextlib.suppress(OSError):
            rec_path.unlink()


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_record_starttime_survives_wall_clock_drift(sessions: Path) -> None:
    """A pty shell stays live when its stable ticks match but its epoch does not."""
    name = "ava-test-starttime-drift"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        assert _wait(lambda: psutil.pid_exists(child.pid))
        starttime = pid_starttime_ticks(child.pid)
        assert starttime is not None
        rec = SessionRecord(
            pid=child.pid,
            create_time=1.0,
            cmd="test",
            cwd=str(sessions),
            started_at=time.time(),
            starttime=starttime,
        )
        rec.write(record_path(name))

        assert rec.identifies(child.pid) is True
        assert pty_cli._record_alive(rec) is True
        assert pty_cli.live_sessions(prefix="ava-test-starttime-") == {name: rec}
        assert record_path(name).exists()
    finally:
        try:
            child.kill()
            child.wait(timeout=5)
        finally:
            record_path(name).unlink(missing_ok=True)
            socket_path(name).unlink(missing_ok=True)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_live_sessions_reaps_starttime_pid_reuse(sessions: Path) -> None:
    """Different clock ticks prove a live shell pid has been recycled."""
    name = "ava-test-starttime-reuse"
    rec = SessionRecord(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cmd="test",
        cwd=str(sessions),
        started_at=time.time(),
        starttime=0,
    )
    rec.write(record_path(name))

    assert rec.identifies(os.getpid()) is False
    assert pty_cli._record_alive(rec) is False
    assert pty_cli.live_sessions(prefix="ava-test-starttime-") == {}
    assert not record_path(name).exists()


def test_live_sessions_keeps_live_legacy_record_with_clock_drift(
    sessions: Path,
    loguru_records: list[dict[str, object]],
) -> None:
    """A legacy epoch mismatch cannot make a live pty shell unowned."""
    name = "ava-test-legacy-drift"
    rec = SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(sessions),
        started_at=time.time(),
    )
    rec.write(record_path(name))

    assert pty_cli._record_alive(rec) is False
    assert pty_cli.live_sessions(prefix="ava-test-legacy-") == {}
    assert record_path(name).exists()
    assert any(
        "pty retaining live session record" in str(record["message"]) for record in loguru_records
    )


# ---------------------------------------------------------------------------
# PtySessionBackend in-process enumeration (task #1200)
# ---------------------------------------------------------------------------


def test_backend_enumerates_without_subprocess_or_socket(
    sessions: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PtySessionBackend.list_sessions / session_started_ats must enumerate
    from the records in-process — no CLI subprocess (the status snapshot pays
    these on every probe; ~0.58s per python startup on a WSL runner, task
    #1200). subprocess.run is pinned to raise so a regression fails loudly."""
    home = sessions
    names = ["ava-test-direct-1", "ava-test-direct-2"]
    for name in names:
        _new(home, name)

    def _no_subprocess(*_a: object, **_kw: object) -> object:
        raise AssertionError("session backend must not spawn subprocesses on the list path")

    monkeypatch.setattr("shared.session_backend.subprocess.run", _no_subprocess)

    from shared.session_backend import PtySessionBackend

    backend = PtySessionBackend()
    got = backend.list_sessions()
    assert set(names) <= set(got)
    epochs = backend.session_started_ats(names)
    for name in names:
        epoch = epochs[name]
        assert epoch is not None and epoch > 0
    assert backend.session_started_at(names[0]) == epochs[names[0]]


def test_backend_list_is_empty_on_a_fresh_home(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sessions = empty enumeration, no subprocess, no error."""
    del unit_home

    def _no_subprocess(*_a: object, **_kw: object) -> object:
        raise AssertionError("session backend must not spawn subprocesses on the list path")

    monkeypatch.setattr("shared.session_backend.subprocess.run", _no_subprocess)

    from shared.session_backend import PtySessionBackend

    backend = PtySessionBackend()
    assert backend.list_sessions() == []
    assert backend.session_started_ats([]) == {}


# ---------------------------------------------------------------------------
# new / send / capture loop
# ---------------------------------------------------------------------------


def test_new_send_capture_loop(sessions: Path) -> None:
    home = sessions
    name = "ava-test-loop-1"
    _new(home, name)
    assert _has(home, name)
    _send(home, name, "echo hello-pty")
    out = _output_until(home, name, "hello-pty")
    assert "hello-pty" in out


def test_new_is_idempotent_for_live_session(sessions: Path) -> None:
    home = sessions
    name = "ava-test-ido-1"
    _new(home, name)
    first = _record(home, name)
    assert first is not None
    _new(home, name)  # second new is a no-op
    assert _record(home, name) == first, "idempotent new must not relaunch"
    _send(home, name, "echo still-one-shell")
    _output_until(home, name, "still-one-shell")


def test_new_reaps_a_recordless_host_before_replacement(sessions: Path) -> None:
    """A vanished record cannot leave a SIGTERM-immune host owning the name.

    The record is deliberately removed while the host and shell are still live,
    modelling the failed registry layer from the schedule incident. A same-name
    ``new`` must force-reap that precisely identified orphan and create one fresh
    session instead of failing on the old host's live socket.
    """
    home = sessions
    name = "ava-test-recordless-1"
    _new(home, name)
    first = _record(home, name)
    identity = host_identity(record_path(name))
    assert first is not None and identity is not None
    old_shell = psutil.Process(first.pid)
    old_host = psutil.Process(identity[0])
    record_path(name).unlink()
    assert not _has(home, name), "the missing record must make the session unlisted"

    envfile = write_env_file({})
    try:
        result = _run_cli(home, name, "new", str(home), str(envfile))
        assert result.returncode == 0, result.stderr
        second = _record(home, name)
        assert second is not None and second.pid != first.pid
        assert not envfile.exists(), "the replacement must consume its env handoff"
        assert _wait(lambda: not old_host.is_running()), "recordless host survived replacement"
        assert _wait(lambda: not old_shell.is_running()), "recordless shell survived replacement"
    finally:
        # The pre-fix behavior rejects the replacement and leaves the deliberately
        # recordless host outside the fixture's normal record-based teardown.
        for proc in (old_shell, old_host):
            with contextlib.suppress(psutil.Error):
                if proc.is_running():
                    proc.kill()
        envfile.unlink(missing_ok=True)


def test_kill_reaps_a_recordless_host_without_its_socket(sessions: Path) -> None:
    """`kill` remains authoritative after both routing artifacts disappear."""
    home = sessions
    name = "ava-test-recordless-kill-1"
    _new(home, name)
    first = _record(home, name)
    identity = host_identity(record_path(name))
    assert first is not None and identity is not None
    old_shell = psutil.Process(first.pid)
    old_host = psutil.Process(identity[0])
    record_path(name).unlink()
    socket_path(name).unlink()

    try:
        _kill(home, name)
        assert _wait(lambda: not old_host.is_running()), "recordless host survived kill"
        assert _wait(lambda: not old_shell.is_running()), "recordless shell survived kill"
    finally:
        for proc in (old_shell, old_host):
            with contextlib.suppress(psutil.Error):
                if proc.is_running():
                    proc.kill()


def test_new_honors_cwd(sessions: Path) -> None:
    home = sessions
    name = "ava-test-cwd-1"
    cwd = home / "somewhere"
    cwd.mkdir()
    _new(home, name, cwd=cwd)
    _send(home, name, "pwd")

    # The pty is DEFAULT_COLS (120) columns wide, and pytest's tmp path (base
    # dir length + monotonically increasing tmp counter, neither related to
    # this test) can exceed that, wrapping `pwd`'s output mid-path with a
    # newline the terminal inserted rather than the shell. Match against the
    # capture with those wrap newlines stripped so the assertion tracks the
    # code, not the environment (issue #77).
    def _seen() -> bool:
        return str(cwd) in _capture(home, name).replace("\n", "")

    assert _wait(_seen, timeout=10.0), f"capture never contained {cwd!s} (even de-wrapped)"


def test_send_transports_tricky_text_via_base64(sessions: Path) -> None:
    home = sessions
    name = "ava-test-b64-1"
    _new(home, name)
    tricky = "echo 'quote \" and spaces \u4f60\u597d'"
    _send(home, name, tricky)
    out = _output_until(home, name, 'quote " and spaces \u4f60\u597d')
    assert 'quote " and spaces \u4f60\u597d' in out


def test_send_without_enter_does_not_submit(sessions: Path) -> None:
    """Text and Enter are separate writes (the SDK contract); text alone
    must sit unsubmitted on the line editor."""
    home = sessions
    name = "ava-test-noent-1"
    _new(home, name)
    _send(home, name, "echo ready-check")
    _output_until(home, name, "ready-check")
    _send(home, name, "echo not-submitted-yet", enter=False)

    # Whitespace-normalized: with a long cwd the echoed text can wrap mid-word.
    def _typed() -> bool:
        return "echonot-submitted-yet" in "".join(_capture(home, name).split())

    assert _wait(_typed, timeout=10.0), "typed text never reached the line editor"
    lines = [ln.strip() for ln in _capture(home, name).split("\n")]
    assert "not-submitted-yet" not in lines
    _keys(home, name, "Enter")
    _output_until(home, name, "not-submitted-yet")


# ---------------------------------------------------------------------------
# send_keys / resize / capture semantics
# ---------------------------------------------------------------------------


def test_send_keys_ctrl_c_interrupts_foreground(sessions: Path) -> None:
    home = sessions
    name = "ava-test-cc-1"
    _new(home, name)
    _send(home, name, "cat")
    time.sleep(0.4)
    _keys(home, name, "C-c")
    _send(home, name, "echo after-interrupt")
    _output_until(home, name, "after-interrupt")
    assert _has(home, name)


def test_send_keys_up_arrow_recalls_history(sessions: Path) -> None:
    home = sessions
    name = "ava-test-up-1"
    _new(home, name)
    _send(home, name, "echo arrow-marker-77")
    _output_until(home, name, "arrow-marker-77")
    _keys(home, name, "Up", "Enter")
    out = _output_until(home, name, "arrow-marker-77")
    assert out.count("arrow-marker-77") >= 2, out


def test_send_keys_literal_unknown_key_types_text(sessions: Path) -> None:
    """screen semantics: an unrecognized key name is typed as literal text."""
    home = sessions
    name = "ava-test-lit-1"
    _new(home, name)
    _keys(home, name, "echo", " ", "literal-keys-ok")
    _keys(home, name, "Enter")
    _output_until(home, name, "literal-keys-ok")


def test_resize_is_seen_by_stty(sessions: Path) -> None:
    home = sessions
    name = "ava-test-resize-1"
    _new(home, name)
    _send(home, name, "echo stty-ready")
    _output_until(home, name, "stty-ready")
    result = _run_cli(home, name, "resize", "100", "25")
    assert result.returncode == 0, result.stderr
    _send(home, name, "stty size")
    out = _capture_until(home, name, "25 100")
    assert "25 100" in out


def test_capture_visible_screen_renders_tui_layout(sessions: Path) -> None:
    """A cursor-addressed full-screen program: the visible screen must show
    the final layout (pyte), and scrollback must still hold the history."""
    home = sessions
    name = "ava-test-tui-1"
    _new(home, name)
    _send(home, name, "echo tui-ready")
    _output_until(home, name, "tui-ready")
    _send(home, name, "seq 1 45")
    _output_until(home, name, "45")
    _send(
        home,
        name,
        "python3 -c 'import sys; sys.stdout.write(\"\\x1b[2J\\x1b[HFAKE-TOP"
        "\\x1b[5;3Hmid-cell\\x1b[8;1Hbottom-row\")'",
    )

    def _drawn() -> bool:
        return _capture(home, name, scrollback=False).split("\n")[0].startswith("FAKE-TOP")

    assert _wait(_drawn)
    vis = _capture(home, name, scrollback=False)
    rows = vis.split("\n")
    assert rows[0].startswith("FAKE-TOP"), rows[:3]
    assert rows[4].startswith("  mid-cell"), rows[:6]
    assert rows[7].startswith("bottom-row"), rows
    assert "\x1b" not in vis, "no raw escape sequences may leak"
    full = _capture(home, name)
    assert "FAKE-TOP" in full and "45" in full, "history must survive the TUI"


def test_capture_lines_caps_output(sessions: Path) -> None:
    """lines=N caps the render at the last N rows (classic -S -N semantics)."""
    home = sessions
    name = "ava-test-lines-1"
    _new(home, name)
    _send(home, name, "echo bulk-line-0")
    _output_until(home, name, "bulk-line-0")
    _send(home, name, "seq 1 45")
    _output_until(home, name, "45")
    out = _capture(home, name, lines=5)
    rows = out.splitlines()
    assert len(rows) <= 5
    assert "45" in out, "the newest rows must be in the tail"
    assert "bulk-line-0" not in out, "older rows must be cut off by the cap"
    assert "42" in out, "rows just above the cap tail are present"
    wide = _capture(home, name, lines=200)
    assert "bulk-line-0" in wide


# ---------------------------------------------------------------------------
# envfile
# ---------------------------------------------------------------------------


def test_envfile_reaches_shell_and_is_consumed(sessions: Path) -> None:
    home = sessions
    name = "ava-test-env-1"
    envfile = write_env_file({"PTY_TEST_VAR": "hello-from-envfile", "EMPTY": ""})
    mode = envfile.stat().st_mode & 0o777
    assert mode == 0o600, f"envfile must be 0600, got {mode:o}"
    result = _run_cli(home, name, "new", str(home), str(envfile))
    assert result.returncode == 0, result.stderr
    assert not envfile.exists(), "envfile must be consumed"
    _send(home, name, "echo value=$PTY_TEST_VAR")
    _output_until(home, name, "value=hello-from-envfile")


def test_new_missing_envfile_fails(sessions: Path) -> None:
    home = sessions
    name = "ava-test-noenv-1"
    result = _run_cli(home, name, "new", str(home), str(home / "does-not-exist.env.sh"))
    assert result.returncode == 1
    assert not _has(home, name)


def test_child_env_does_not_inherit_spawner_process_profile(sessions: Path) -> None:
    """A spawner carrying AVA_PROCESS_PROFILE (an ops-daemon-mediated create,
    a service-context caller) must not leak the marker into session children.

    A leaked service marker is fatal for watcher children: the watcher
    bootstrap `import ava` -> plugin load -> agent.graph._build -> agent/db.py
    reads `settings.agent.*`, and the runner profile does not construct the
    agent config domain (Task #856 fail-fast) -> every watcher dies at boot.
    The pty child pops the marker BEFORE the envfile overlay, so a marker the
    envfile explicitly supplies still rides.
    """
    home = sessions
    name = "ava-test-profile-1"
    envfile = write_env_file({})
    result = subprocess.run(  # noqa: S603 — repo-internal argv
        [sys.executable, "-m", "shared.pty_sessions.cli", name, "new", str(home), str(envfile)],
        cwd=REPO,
        env={**os.environ, "AVA_HOME": str(home), "AVA_PROCESS_PROFILE": "runner"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    _send(home, name, "env | grep AVA_PROCESS_PROFILE ; echo ENVCHECK_DONE")
    out = _capture_until(home, name, "ENVCHECK_DONE")
    assert "AVA_PROCESS_PROFILE=" not in out, f"spawner profile leaked into session child:\n{out}"


# ---------------------------------------------------------------------------
# kill semantics
# ---------------------------------------------------------------------------


def test_kill_reaps_process_tree_no_orphans(sessions: Path) -> None:
    home = sessions
    name = "ava-test-killtree-1"
    _new(home, name)
    rec = _record(home, name)
    assert rec is not None
    shell = psutil.Process(rec.pid)
    _send(home, name, "sleep 300")  # foreground job in the shell's group
    assert _wait(shell.children), "sleep never appeared as a child"
    sleeper = shell.children()[0].pid

    _kill(home, name)
    assert _wait(lambda: not psutil.pid_exists(rec.pid)), "shell survived the kill"
    assert _wait(lambda: not psutil.pid_exists(sleeper)), "sleep orphaned by the kill"
    assert not _has(home, name)
    assert _wait(lambda: _record(home, name) is None), "record must be unlinked on kill"


def test_kill_ends_the_host_process(sessions: Path) -> None:
    """The host exits when its session dies — no host may linger as an
    unmanaged orphan after the session it carried is gone."""
    home = sessions
    name = "ava-test-hostexit-1"
    _new(home, name)
    identity = host_identity(record_path(name))
    assert identity is not None
    host_pid, _ = identity
    _kill(home, name)
    assert _wait(lambda: not psutil.pid_exists(host_pid)), "host must exit with its session"
    assert not socket_path(name).exists(), "socket must be unlinked on session end"


def test_kill_graceful_then_force(sessions: Path) -> None:
    home = sessions
    name = "ava-test-killg-1"
    _new(home, name)
    assert _kill(home, name, graceful=True) == 0
    assert _wait(lambda: not _has(home, name))


def test_kill_is_idempotent_noop_on_absent(sessions: Path) -> None:
    home = sessions
    assert _kill(home, "ava-test-absent-1") == 0


def test_kill_by_record_reaches_a_wedged_host(sessions: Path) -> None:
    """A host that stops answering its socket (SIGSTOP here) must still be
    killable: the CLI falls back to record-pid kills — shell group + host —
    so `ava stop`'s reap stays authoritative against a broken host."""
    home = sessions
    name = "ava-test-wedge-1"
    _new(home, name)
    rec = _record(home, name)
    identity = host_identity(record_path(name))
    assert rec is not None and identity is not None
    host_pid, _ = identity
    os.kill(host_pid, signal.SIGSTOP)
    try:
        from shared.pty_sessions.cli import _kill_by_record

        assert _kill_by_record(name) == 0
    finally:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(host_pid, signal.SIGCONT)
    assert _wait(lambda: not psutil.pid_exists(rec.pid)), "shell must die"
    assert _wait(lambda: not psutil.pid_exists(host_pid)), "host must die"
    assert not record_path(name).exists()
    assert not socket_path(name).exists()


def test_session_exit_cleans_up_record(sessions: Path) -> None:
    home = sessions
    name = "ava-test-exit-1"
    _new(home, name)
    _send(home, name, "exit")
    assert _wait(lambda: not _has(home, name)), "session must die with its shell"
    assert _wait(lambda: _record(home, name) is None)


def test_ops_after_death_report_no_such_session(sessions: Path) -> None:
    home = sessions
    name = "ava-test-dead-1"
    _new(home, name)
    _send(home, name, "exit")
    assert _wait(lambda: not _has(home, name))
    assert _wait(lambda: not socket_path(name).exists()), "socket must be gone after death"
    b64 = base64.b64encode(b"echo x").decode("ascii")
    assert _run_cli(home, name, "send", b64).returncode == 3
    assert _run_cli(home, name, "capture").returncode == 3


def test_record_carries_host_identity(sessions: Path) -> None:
    """The pty record is a SessionRecord (shell pid = the liveness key) plus
    the host identity keys; generic SessionRecord readers must still parse
    it (they ignore the extras)."""
    home = sessions
    name = "ava-test-rec-1"
    _new(home, name)
    raw = json.loads(record_path(name).read_text())
    assert {
        "pid",
        "create_time",
        "starttime",
        "host_pid",
        "host_create_time",
        "host_starttime",
    } <= set(raw)
    rec = SessionRecord.read(record_path(name))
    assert rec is not None and rec.pid == raw["pid"]
    identity = host_identity(record_path(name))
    assert identity is not None
    host_pid, host_create = identity
    assert abs(psutil.Process(host_pid).create_time() - host_create) <= 2.0
    if IS_LINUX:
        assert rec.starttime == pid_starttime_ticks(rec.pid)
        assert raw["host_starttime"] == pid_starttime_ticks(host_pid)


def test_kill_then_immediate_same_name_new_is_a_real_session(sessions: Path) -> None:
    """kill x → new x with no pause: the new must either build a REAL fresh
    session or fail honestly — never adopt the dying host as its success (the
    P2 ghost-success race: the old host's socket answering ping during its
    reap window). The teardown unlinks record+socket at claim time and a dying
    host's ping answers err, so the fresh spawn wins the name immediately."""
    home = sessions
    name = "ava-test-rebirth-1"
    _new(home, name)
    first = _record(home, name)
    assert first is not None
    assert _kill(home, name) == 0
    _new(home, name)  # immediately — no settle sleep
    second = _record(home, name)
    assert second is not None, "the rebuilt session must exist"
    assert second.pid != first.pid, "must be a fresh shell, not the dying one"
    _send(home, name, "echo reborn-ok")
    _output_until(home, name, "reborn-ok")
