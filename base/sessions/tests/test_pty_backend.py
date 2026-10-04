"""Tests for ``base.sessions.backend.PtySessionBackend`` -- the backend of agent
shells and watchers, a thin mapping over the pty-sessions service client.

Observable behavior runs against a real service (the `pty_service` fixture: a
subprocess under the test's home, real `bash -l -i` shells on real ptys): create,
idempotent create, typing, key presses, capture, kill verdicts, listing, launch
epochs, generations, the transcript path, and that the creator's environment
travels in the request body rather than on an argv. The mapping of failures runs
without a healthy service: a home with no service (queries read as empty, `kill` is
a noop, mutating calls fail) and a wedged endpoint that accepts a connection and
never answers (the service dialed but did not respond).
"""

from __future__ import annotations

import contextlib
import inspect
import socket
import threading
from collections.abc import Generator
from pathlib import Path

import psutil
import pytest

import base.sessions.backend as sb
from base.native_process.os_platform import IS_WINDOWS
from base.sessions.backend import (
    PosixProcSessionBackend,
    PtySessionBackend,
    SessionBackend,
)
from base.sessions.pty import allocation_freeze, client
from base.sessions.pty.paths import service_socket_path
from tests.path_scoped.pty_service import PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import (
    gone,
    output_until,
    screen,
    shell_process,
    type_line,
    wait_for,
)

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only")

_NAME = "ava-agent-1-shell-2"


def _backend() -> PtySessionBackend:
    return PtySessionBackend()


def _new(name: str, cwd: Path, *, cmd: str = "", env: dict[str, str] | None = None) -> None:
    assert _backend().new_session(name, cmd, cwd, env={} if env is None else env)


@contextlib.contextmanager
def _wedged_service() -> Generator[None]:
    """An endpoint at the service socket that accepts a connection and closes it
    unanswered: a service that was dialed and did not respond."""
    path = service_socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)

    def accept_and_drop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=accept_and_drop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        listener.close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# A real service: create, type, capture, kill
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("pty_service")
def test_has_session_tracks_liveness(unit_home: Path) -> None:
    assert _backend().has_session(_NAME) is False
    _new(_NAME, unit_home)
    assert _backend().has_session(_NAME) is True
    _backend().kill_session(_NAME)
    assert _backend().has_session(_NAME) is False


@pytest.mark.usefixtures("pty_service")
def test_new_session_starts_a_login_shell_in_the_cwd(unit_home: Path) -> None:
    cwd = unit_home / "work"
    cwd.mkdir()
    _new(_NAME, cwd)
    (info,) = client.list_sessions()
    assert (info.name, info.cwd, info.cmd) == (_NAME, str(cwd), "/bin/bash -l -i")
    assert shell_process(_NAME).cwd() == str(cwd.resolve())


@pytest.mark.usefixtures("pty_service")
def test_new_session_submits_the_command_once_the_shell_is_ready(unit_home: Path) -> None:
    _new(_NAME, unit_home, cmd="echo initial-command-ran")
    output_until(_NAME, "initial-command-ran")


@pytest.mark.usefixtures("pty_service")
def test_new_session_is_idempotent_for_a_live_session(unit_home: Path) -> None:
    """A second create of a live name succeeds and leaves the shell untouched."""
    _new(_NAME, unit_home)
    (first,) = client.list_sessions()
    assert _backend().new_session(_NAME, "", unit_home, env={}) is True
    (second,) = client.list_sessions()
    assert second == first


@pytest.mark.usefixtures("pty_service")
def test_new_session_without_env_creates_the_standard_projection(unit_home: Path) -> None:
    """`env` omitted: the backend builds the standard forward projection itself."""
    assert _backend().new_session(_NAME, "", unit_home) is True
    type_line(_NAME, "echo projection-ready")
    output_until(_NAME, "projection-ready")


def test_the_creator_env_reaches_the_shell_in_the_request_not_on_an_argv(
    pty_service: PtyServiceProcess, unit_home: Path
) -> None:
    canary = "argv-canary-0042"  # a sentinel to search argv for
    _new(_NAME, unit_home, env={"AVA_TEST_CANARY": canary})
    type_line(_NAME, "echo value=$AVA_TEST_CANARY")
    output_until(_NAME, f"value={canary}")
    service = psutil.Process(pty_service.pid)
    for process in [service, *service.children(recursive=True)]:
        with contextlib.suppress(psutil.NoSuchProcess):
            assert canary not in " ".join(process.cmdline()), process.cmdline()


@pytest.mark.usefixtures("pty_service")
def test_send_types_text_without_enter(unit_home: Path) -> None:
    """`send` types the text only; the caller submits the line separately."""
    _new(_NAME, unit_home)
    _backend().send(_NAME, 'echo "hi there"; echo typed-marker')
    # Whitespace-normalized: with a long cwd the echoed text can wrap mid-word.
    assert wait_for(lambda: 'echo"hithere";echotyped-marker' in "".join(screen(_NAME).split()))
    assert "typed-marker" not in [line.strip() for line in screen(_NAME).split("\n")]
    _backend().send_keys(_NAME, "Enter")
    output_until(_NAME, "typed-marker")


@pytest.mark.usefixtures("pty_service")
def test_send_keys_names_become_key_presses(unit_home: Path) -> None:
    _new(_NAME, unit_home)
    type_line(_NAME, "cat")
    # `cat` runs with the terminal in its default mode: C-c interrupts it, then the
    # shell takes the next line.
    assert wait_for(lambda: bool(shell_process(_NAME).children()))
    _backend().send_keys(_NAME, "C-c")
    _backend().send(_NAME, "echo after-interrupt")
    _backend().send_keys(_NAME, "Enter")
    output_until(_NAME, "after-interrupt")


@pytest.mark.usefixtures("pty_service")
def test_capture_pane_reads_scrollback_by_default_and_the_screen_on_request(
    unit_home: Path,
) -> None:
    _new(_NAME, unit_home)
    type_line(_NAME, "echo first-line")
    output_until(_NAME, "first-line")
    type_line(_NAME, "seq 1 60")
    output_until(_NAME, "60")
    assert "first-line" in _backend().capture_pane(_NAME)
    assert "first-line" not in _backend().capture_pane(_NAME, scrollback=False)
    tail = _backend().capture_pane(_NAME, lines=5)
    assert len(tail.splitlines()) <= 5
    assert "60" in tail and "first-line" not in tail


@pytest.mark.usefixtures("pty_service")
def test_send_and_capture_on_an_absent_session_raise_runtime_error() -> None:
    backend = _backend()
    with pytest.raises(RuntimeError, match=r"pty send 'ghost' failed: no such pty session"):
        backend.send("ghost", "echo x")
    with pytest.raises(RuntimeError, match=r"pty send_keys 'ghost' failed: no such pty session"):
        backend.send_keys("ghost", "C-c")
    with pytest.raises(RuntimeError, match=r"pty capture 'ghost' failed: no such pty session"):
        backend.capture_pane("ghost")


@pytest.mark.usefixtures("pty_service")
def test_kill_session_force(unit_home: Path) -> None:
    _new(_NAME, unit_home)
    shell = shell_process(_NAME)
    assert _backend().kill_session(_NAME, graceful=False) == (True, "forced")
    assert wait_for(lambda: gone(shell))
    assert not _backend().has_session(_NAME)


def test_gone_reads_a_process_reaped_between_two_polls_as_gone() -> None:
    """The service reaps a killed shell on its own schedule, so a status read can land
    after the reap: that is `gone`, not an error (the CI flake of `test_kill_session_force`
    polled `is_running()` and then `status()`, and the reap fell between the two)."""

    class ReapedBetweenPolls:
        def is_running(self) -> bool:
            return True

        def status(self) -> str:
            raise psutil.NoSuchProcess(1)

    assert gone(ReapedBetweenPolls())  # type: ignore[arg-type]


@pytest.mark.usefixtures("pty_service")
def test_kill_session_graceful(unit_home: Path) -> None:
    _new(_NAME, unit_home)
    ok, mode = _backend().kill_session(_NAME, graceful=True, timeout=9.0, expected=True)
    assert ok is True
    assert mode in ("graceful", "forced")
    assert wait_for(lambda: not _backend().has_session(_NAME))


@pytest.mark.usefixtures("pty_service")
def test_kill_session_of_an_absent_session_is_a_noop_success() -> None:
    """Idempotence: killing an absent session confirms it is gone."""
    assert _backend().kill_session("ghost") == (True, "noop")
    assert _backend().kill_session_with_verdict("ghost") == (True, "noop", False)


@pytest.mark.usefixtures("pty_service")
def test_kill_session_with_verdict_reports_whether_work_was_cut_short(unit_home: Path) -> None:
    """The verdict comes from the kill itself: an idle shell is not interrupted, a
    shell running a job is."""
    _new("ava-test-idle-1", unit_home)
    type_line("ava-test-idle-1", "echo idle-ready")
    output_until("ava-test-idle-1", "idle-ready")
    ok, mode, interrupted = _backend().kill_session_with_verdict("ava-test-idle-1")
    assert (ok, mode, interrupted) == (True, "forced", False)

    _new("ava-test-busy-1", unit_home)
    type_line("ava-test-busy-1", "sleep 300")
    assert wait_for(lambda: bool(shell_process("ava-test-busy-1").children()))
    ok, mode, interrupted = _backend().kill_session_with_verdict("ava-test-busy-1")
    assert (ok, mode, interrupted) == (True, "forced", True)
    assert not _backend().has_session("ava-test-busy-1")


# ---------------------------------------------------------------------------
# list_sessions / launch epochs / generation / transcript path
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("pty_service")
def test_list_sessions_is_sorted_and_filtered_by_prefix(unit_home: Path) -> None:
    for name in ("ava-agent-1-shell-2", "ava-agent-1-shell-1", "other-1"):
        _new(name, unit_home)
    assert _backend().list_sessions() == ["ava-agent-1-shell-1", "ava-agent-1-shell-2", "other-1"]
    assert _backend().list_sessions(prefix="ava-agent-1") == [
        "ava-agent-1-shell-1",
        "ava-agent-1-shell-2",
    ]
    assert _backend().list_sessions(prefix="nothing") == []


@pytest.mark.usefixtures("pty_service")
def test_started_ats_answers_every_name_from_the_service_epochs(unit_home: Path) -> None:
    """A live session maps to the epoch the service recorded, an absent one to None."""
    _new("ava-agent-1-shell-1", unit_home)
    (info,) = client.list_sessions()
    epochs = _backend().session_started_ats(["ava-agent-1-shell-1", "ava-agent-1-shell-9"])
    assert epochs == {"ava-agent-1-shell-1": info.started_at, "ava-agent-1-shell-9": None}
    assert info.started_at > 0
    assert _backend().session_started_at("ava-agent-1-shell-1") == info.started_at
    assert _backend().session_started_at("ava-agent-1-shell-9") is None
    assert _backend().session_started_ats([]) == {}


@pytest.mark.usefixtures("pty_service")
def test_session_generation_is_the_one_the_session_was_admitted_under(unit_home: Path) -> None:
    _new("ava-agent-1-shell-1", unit_home)
    assert _backend().session_generation("ava-agent-1-shell-1") is None
    frozen = allocation_freeze.freeze(holder="operator", reason="generation flip")
    assert frozen.generation is not None
    assert allocation_freeze.resume(frozen.generation)
    _new("ava-agent-1-shell-2", unit_home)
    assert _backend().session_generation("ava-agent-1-shell-2") == frozen.generation
    assert _backend().session_generation("ava-agent-1-shell-9") is None


@pytest.mark.usefixtures("pty_service")
def test_session_log_path_is_the_transcript_the_service_writes(unit_home: Path) -> None:
    """The service writes a byte transcript per session at $AVA_HOME/logs/<name>.out.log."""
    _new(_NAME, unit_home)
    transcript = _backend().session_log_path(_NAME)
    assert transcript == unit_home / "logs" / f"{_NAME}.out.log"
    assert transcript is not None
    type_line(_NAME, "echo transcript-line")
    output_until(_NAME, "transcript-line")
    assert wait_for(lambda: "transcript-line" in transcript.read_text(errors="replace"))


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("pty_service")
def test_new_session_login_shell_false_raises() -> None:
    with pytest.raises(NotImplementedError):
        _backend().new_session("s", "cmd", Path("/"), env={}, login_shell=False)
    assert client.list_sessions() == [], "nothing reached the service"


@pytest.mark.usefixtures("pty_service")
def test_new_session_refused_by_the_service_returns_false_and_warns(
    unit_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An env the service cannot forward (a NUL byte) is refused in its response."""
    ok = _backend().new_session("s", "", unit_home, env={"A": "x\0y"})
    assert ok is False
    assert "pty session allocation failed for s" in caplog.text
    assert "cannot be forwarded" in caplog.text
    assert client.list_sessions() == []


@pytest.mark.usefixtures("pty_service")
def test_new_session_under_an_allocation_freeze_returns_false_and_warns(
    unit_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    frozen = allocation_freeze.freeze(holder="operator", reason="cleanup")
    assert frozen.generation is not None
    assert _backend().new_session("s", "", unit_home, env={}) is False
    assert "allocation refused" in caplog.text
    assert allocation_freeze.resume(frozen.generation)
    assert _backend().new_session("s", "", unit_home, env={}) is True


def test_a_home_without_a_service_holds_no_sessions(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is alive in a service that is not there: queries read as empty and `kill`
    as a noop; `new` fails as False with a warning, and `send`/`capture` raise."""
    monkeypatch.setattr(client, "CONNECT_WAIT_S", 0.0)
    backend = _backend()
    assert backend.has_session(_NAME) is False
    assert backend.list_sessions() == []
    assert backend.list_sessions(prefix="ava-agent-1") == []
    assert backend.session_started_ats([_NAME]) == {_NAME: None}
    assert backend.session_started_at(_NAME) is None
    assert backend.session_generation(_NAME) is None
    assert backend.kill_session(_NAME) == (True, "noop")
    assert backend.kill_session_with_verdict(_NAME, graceful=True) == (True, "noop", False)

    assert backend.new_session(_NAME, "", unit_home, env={}) is False
    assert "no pty-sessions service is listening" in caplog.text
    with pytest.raises(RuntimeError, match=rf"pty send {_NAME!r} failed: no pty-sessions service"):
        backend.send(_NAME, "echo x")
    with pytest.raises(RuntimeError, match=rf"pty send_keys {_NAME!r} failed"):
        backend.send_keys(_NAME, "C-c")
    with pytest.raises(RuntimeError, match=rf"pty capture {_NAME!r} failed"):
        backend.capture_pane(_NAME)


def test_a_service_that_does_not_answer_fails_loudly_not_as_empty(
    unit_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A dialed service that never answers is not a service that holds nothing: the kill
    is NOT confirmed (and counts as interrupting), creation fails, typing raises."""
    backend = _backend()
    with _wedged_service():
        assert backend.kill_session(_NAME) == (False, "forced")
        assert backend.kill_session(_NAME, graceful=True) == (False, "graceful")
        assert backend.kill_session_with_verdict(_NAME) == (False, "forced", True)
        assert backend.kill_session_with_verdict(_NAME, graceful=True) == (False, "graceful", True)
        assert f"pty kill of {_NAME} failed" in caplog.text

        assert backend.new_session(_NAME, "", unit_home, env={}) is False
        assert f"pty session allocation failed for {_NAME}" in caplog.text
        with pytest.raises(RuntimeError, match=rf"pty send {_NAME!r} failed"):
            backend.send(_NAME, "echo x")
        with pytest.raises(RuntimeError, match=rf"pty capture {_NAME!r} failed"):
            backend.capture_pane(_NAME)


# ---------------------------------------------------------------------------
# interface alignment
# ---------------------------------------------------------------------------


def test_pty_backend_is_a_session_backend() -> None:
    assert isinstance(_backend(), SessionBackend)


def test_pty_capture_defaults() -> None:
    """capture_pane's (lines=200, scrollback=True) defaults are the interface
    contract the sessions.py consumer relies on."""
    pty_sig = inspect.signature(PtySessionBackend.capture_pane)
    assert pty_sig.parameters["lines"].default == 200
    assert pty_sig.parameters["scrollback"].default is True


def test_non_pty_backends_still_raise_send() -> None:
    """send is PTY-only like send_keys/capture_pane: the process supervisors
    raise, the PTY backend implements."""
    with pytest.raises(NotImplementedError):
        PosixProcSessionBackend().send("s", "text")


def test_get_shell_backend_is_pty() -> None:
    """get_shell_backend() is PtySessionBackend on POSIX; service sessions live on
    the process-supervisor backend."""
    assert isinstance(sb.get_shell_backend(), PtySessionBackend)
    assert sb.get_shell_backend() is not sb.get_backend()
