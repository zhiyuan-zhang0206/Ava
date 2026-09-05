"""Tests for ``shared.session_backend.PtySessionBackend`` — the PTY backend
that talks to the per-session-host CLI (``shared.pty_sessions.cli``).

Most tests drive the backend through a fake ``subprocess.run`` — the same
monkeypatch shape ``test_session_backend.py`` uses for the shell backend. The
assertion target is the **calling shape**: argv, exit-status mapping, error
propagation, and idempotence semantics — the real CLI round-trip is
tests/shared/test_pty_sessions_cli.py's job.

The enumeration ops (``list_sessions`` / ``session_started_ats`` /
``session_started_at``) spawn nothing at all: they read the session records
in-process (task #1200 — the CLI subprocess cost dominated the status
snapshot on slow hosts), so their tests fake the record scan
(``shared.pty_sessions.cli.live_sessions``) and assert the mapping.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import shared.session_backend as sb
from shared.platform import IS_WINDOWS
from shared.session_backend import (
    PosixProcSessionBackend,
    PtySessionBackend,
    SessionBackend,
    WinprocSessionBackend,
)

# The exact CLI invocation shape the backend must produce (W1a contract):
CLI_PREFIX = [sys.executable, "-m", "shared.pty_sessions.cli"]


class _FakeCompletedProcess:
    """Minimal fake for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def cli_calls(monkeypatch: pytest.MonkeyPatch):
    """Record every CLI argv and let the test script the responses."""
    calls: list[list[str]] = []
    script: dict[tuple[str, ...], _FakeCompletedProcess] = {}

    def fake_run(args: list[str], **_kw: object) -> _FakeCompletedProcess:
        calls.append(list(args))
        key = tuple(args[len(CLI_PREFIX) :][:2])  # (name, op) — ignore op args
        if key in script:
            return script[key]
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls, script


def _backend() -> PtySessionBackend:
    return PtySessionBackend()


def _argv(calls: list[list[str]]) -> list[str]:
    assert calls, "expected at least one CLI call"
    argv = calls[-1]
    assert argv[: len(CLI_PREFIX)] == CLI_PREFIX, f"bad CLI prefix: {argv}"
    return argv[len(CLI_PREFIX) :]


# ---------------------------------------------------------------------------
# has_session
# ---------------------------------------------------------------------------


def test_has_session_true(cli_calls):
    calls, _ = cli_calls
    assert _backend().has_session("ava-agent-1-shell-2") is True
    assert _argv(calls) == ["ava-agent-1-shell-2", "has"]  # pyright: ignore[reportUnknownArgumentType]


def test_has_session_false(cli_calls):
    _calls, script = cli_calls
    script[("ava-agent-1-shell-2", "has")] = _FakeCompletedProcess(returncode=1)
    assert _backend().has_session("ava-agent-1-shell-2") is False


# ---------------------------------------------------------------------------
# new_session
# ---------------------------------------------------------------------------


def test_new_session_cli_shape_and_envfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cli_calls,
):
    """new → CLI `new <name> <cwd> <envfile>`; the env rides a 0600 handoff
    file, never argv (issue #974 — the CLI only ever sees the file path)."""
    calls, _ = cli_calls
    monkeypatch.setattr("shared.session_backend.run_dir", lambda: tmp_path)
    backend = _backend()
    ok = backend.new_session(
        "ava-agent-1-shell-2",
        "",
        Path("/workdir"),
        env={"AVA_HOME": "/home", "SECRET": "hunter2 value", "BASH_FUNC_x%%": "no"},
    )
    assert ok is True
    argv = _argv(calls)  # pyright: ignore[reportUnknownArgumentType]
    assert argv[0] == "ava-agent-1-shell-2"
    assert argv[1] == "new"
    assert argv[2] == "/workdir"
    envfile = Path(argv[3])
    assert envfile.parent == tmp_path / "session-env"
    assert envfile.stat().st_mode & 0o777 == 0o600
    # shell-identifier keys only, sorted, shlex-quoted (env_load_prefix format)
    body = envfile.read_text()
    assert body == (f"AVA_HOME={shlex.quote('/home')}\nSECRET={shlex.quote('hunter2 value')}\n")
    # no secret value ever reaches the CLI argv
    assert "hunter2" not in argv
    assert "hunter2" not in " ".join(argv)


def test_new_session_login_shell_false_raises(cli_calls):
    calls, _ = cli_calls
    with pytest.raises(NotImplementedError):
        _backend().new_session("s", "cmd", Path("/"), env={}, login_shell=False)
    assert calls == []  # nothing reached the CLI


def test_new_session_failure_returns_false_and_cleans_envfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    cli_calls,
):
    calls, script = cli_calls
    monkeypatch.setattr("shared.session_backend.run_dir", lambda: tmp_path)
    script[("s", "new")] = _FakeCompletedProcess(
        returncode=1, stderr="pty allocation refused: generation 'freeze-1'"
    )
    backend = _backend()
    ok = backend.new_session("s", "", Path("/"), env={"PATH": "/bin"})
    assert ok is False
    envfile = Path(_argv(calls)[3])  # pyright: ignore[reportUnknownArgumentType]
    assert not envfile.exists()  # the failed handoff must not linger
    assert "pty allocation refused: generation 'freeze-1'" in caplog.text


def test_new_session_env_null_byte_raises(cli_calls):
    calls, _ = cli_calls
    with pytest.raises(RuntimeError):
        _backend().new_session("s", "", Path("/"), env={"A": "x\0y"})
    assert calls == []


# ---------------------------------------------------------------------------
# send / send_keys
# ---------------------------------------------------------------------------


def test_send_text_base64_single_argument(cli_calls):
    """The text rides one base64 argument (the W1a contract — no quote hell);
    Enter is NOT included (the caller submits it separately)."""
    calls, _ = cli_calls
    _backend().send("s", 'echo "hi"; ls ~/a b')
    argv = _argv(calls)  # pyright: ignore[reportUnknownArgumentType]
    assert argv[0] == "s"
    assert argv[1] == "send"
    assert len(argv) == 3
    assert argv[2] == base64.b64encode(b'echo "hi"; ls ~/a b').decode("ascii")
    assert "\r" not in argv[2] and "Enter" not in argv


def test_send_raises_with_cli_stderr(cli_calls):
    _calls, script = cli_calls
    script[("s", "send")] = _FakeCompletedProcess(returncode=1, stderr="no such session")
    with pytest.raises(RuntimeError, match="no such session"):
        _backend().send("s", "echo x")


def test_send_keys_names_pass_through(cli_calls):
    calls, _ = cli_calls
    _backend().send_keys("s", "C-c", "Up", "Enter")
    assert _argv(calls) == ["s", "send_keys", "C-c", "Up", "Enter"]  # pyright: ignore[reportUnknownArgumentType]


def test_send_keys_raises_with_cli_stderr(cli_calls):
    _calls, script = cli_calls
    script[("s", "send_keys")] = _FakeCompletedProcess(returncode=1, stderr="dead")
    with pytest.raises(RuntimeError, match="dead"):
        _backend().send_keys("s", "C-c")


# ---------------------------------------------------------------------------
# capture_pane — defaults (lines=200, scrollback=True) are the classic
# ---------------------------------------------------------------------------


def test_capture_pane_with_scrollback(cli_calls):
    calls, script = cli_calls
    script[("s", "capture")] = _FakeCompletedProcess(returncode=0, stdout="line1\nline2\n")
    out = _backend().capture_pane("s")
    assert out == "line1\nline2\n"
    assert _argv(calls) == ["s", "capture", "200", "--scrollback"]  # pyright: ignore[reportUnknownArgumentType]


def test_capture_pane_custom_lines(cli_calls):
    calls, _ = cli_calls
    _backend().capture_pane("s", lines=50)
    assert _argv(calls) == ["s", "capture", "50", "--scrollback"]  # pyright: ignore[reportUnknownArgumentType]


def test_capture_pane_no_scrollback_passes_flag(cli_calls):
    """scrollback=False shows only the visible screen — the CLI defaults to
    scrollback=True, so the flag must be explicit (W1a contract: capture
    <name> [lines] [--scrollback|--no-scrollback])."""
    calls, _ = cli_calls
    _backend().capture_pane("s", scrollback=False)
    assert _argv(calls) == ["s", "capture", "--no-scrollback"]  # pyright: ignore[reportUnknownArgumentType]


def test_capture_pane_raises_with_cli_stderr(cli_calls):
    _calls, script = cli_calls
    script[("s", "capture")] = _FakeCompletedProcess(returncode=1, stderr="gone")
    with pytest.raises(RuntimeError, match="gone"):
        _backend().capture_pane("s")


# ---------------------------------------------------------------------------
# kill_session — exit status maps to ok; idempotent via the CLI
# ---------------------------------------------------------------------------


def test_kill_session_force(cli_calls):
    calls, _ = cli_calls
    ok, mode = _backend().kill_session("s", graceful=False)
    assert (ok, mode) == (True, "forced")
    assert _argv(calls) == ["s", "kill"]  # pyright: ignore[reportUnknownArgumentType]


def test_kill_session_graceful_flag(cli_calls):
    calls, _ = cli_calls
    ok, mode = _backend().kill_session("s", graceful=True, timeout=9.0, expected=True)
    assert (ok, mode) == (True, "graceful")
    assert _argv(calls) == ["s", "kill", "--graceful"]  # pyright: ignore[reportUnknownArgumentType]


def test_kill_session_reports_failure(cli_calls):
    _calls, script = cli_calls
    script[("s", "kill")] = _FakeCompletedProcess(returncode=1)
    ok, mode = _backend().kill_session("s")
    assert (ok, mode) == (False, "forced")


def test_kill_session_absent_session_is_ok(cli_calls):
    """Idempotence: killing an absent session is a noop success — the CLI
    answers exit 0 and the backend maps it straight through."""
    _calls, _ = cli_calls  # default fake answers 0
    ok, mode = _backend().kill_session("ghost")
    assert (ok, mode) == (True, "forced")


def test_kill_session_with_verdict_maps_cli_stdout(cli_calls):
    """The TTL reaper's interrupt verdict rides the kill CLI's stdout: `idle`
    → not interrupted; `interrupted` → interrupted; anything else (record-based
    fallback kill of a wedged host) is not proven idle → interrupted (fail-open)."""
    calls, script = cli_calls
    script[("s", "kill")] = _FakeCompletedProcess(returncode=0, stdout="interrupted\n")
    assert _backend().kill_session_with_verdict("s") == (True, "forced", True)
    assert _argv(calls) == ["s", "kill"]  # pyright: ignore[reportUnknownArgumentType]

    script[("s", "kill")] = _FakeCompletedProcess(returncode=0, stdout="idle\n")
    assert _backend().kill_session_with_verdict("s") == (True, "forced", False)

    script[("s", "kill")] = _FakeCompletedProcess(returncode=0, stdout="")
    assert _backend().kill_session_with_verdict("s") == (True, "forced", True)

    script[("s", "kill")] = _FakeCompletedProcess(returncode=1, stderr="boom")
    assert _backend().kill_session_with_verdict("s")[0] is False


# ---------------------------------------------------------------------------
# list_sessions / session_log_path
# ---------------------------------------------------------------------------


def _fake_records(names: list[str]) -> dict[str, object]:
    from shared.session_record import SessionRecord

    return {
        n: SessionRecord(pid=1, create_time=1.0, cmd="", cwd="", started_at=100.0 + i)
        for i, n in enumerate(names)
    }


def test_list_sessions(monkeypatch: pytest.MonkeyPatch):
    """list_sessions enumerates from the record scan in-process — no CLI
    subprocess and no socket (task #1200: each CLI spawn measured ~0.58s on a
    WSL runner)."""
    seen: list[str] = []

    def _fake_live(prefix: str = "") -> dict[str, object]:
        seen.append(prefix)
        return _fake_records(["ava-agent-1-shell-2", "ava-agent-1-shell-1", "other"])

    monkeypatch.setattr("shared.pty_sessions.cli.live_sessions", _fake_live)
    assert _backend().list_sessions() == [
        "ava-agent-1-shell-1",
        "ava-agent-1-shell-2",
        "other",
    ]
    assert seen == [""]


def test_list_sessions_with_prefix(monkeypatch: pytest.MonkeyPatch):
    """The prefix rides into the record scan's filter."""
    seen: list[str] = []

    def _fake_live(prefix: str = "") -> dict[str, object]:
        seen.append(prefix)
        return _fake_records(["ava-agent-1-shell-1"])

    monkeypatch.setattr("shared.pty_sessions.cli.live_sessions", _fake_live)
    assert _backend().list_sessions(prefix="ava-agent-1") == ["ava-agent-1-shell-1"]
    assert seen == ["ava-agent-1"]


def test_started_ats_maps_absent_sessions_to_none(monkeypatch: pytest.MonkeyPatch):
    """session_started_ats answers every asked name — a live session with its
    record epoch, an absent one with None — from ONE record scan."""

    calls = 0

    def _fake_live(prefix: str = "") -> dict[str, object]:
        nonlocal calls
        del prefix
        calls += 1
        return _fake_records(["ava-agent-1-shell-1"])

    monkeypatch.setattr("shared.pty_sessions.cli.live_sessions", _fake_live)
    epochs = _backend().session_started_ats(["ava-agent-1-shell-1", "ava-agent-1-shell-9"])
    assert epochs["ava-agent-1-shell-1"] == 100.0
    assert epochs["ava-agent-1-shell-9"] is None
    assert calls == 1


def test_started_ats_falls_back_when_record_scan_fails(monkeypatch: pytest.MonkeyPatch):
    """A transient batch-scan I/O failure falls back to the single-record
    path, preserving timestamps for records that remain individually readable."""
    backend = _backend()
    names = ["ava-agent-1-shell-1", "ava-agent-1-shell-9"]
    single_reads: list[str] = []

    def _failed_scan(prefix: str = "") -> dict[str, object]:
        del prefix
        raise OSError("record directory changed during scan")

    def _single_read(name: str) -> float | None:
        single_reads.append(name)
        return 100.0 if name == names[0] else None

    monkeypatch.setattr("shared.pty_sessions.cli.live_sessions", _failed_scan)
    monkeypatch.setattr(backend, "session_started_at", _single_read)

    assert backend.session_started_ats(names) == {names[0]: 100.0, names[1]: None}
    assert single_reads == names


def test_session_log_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The session host writes a free byte log per session at
    $AVA_HOME/logs/<name>.out.log (the posixproc convention)."""
    monkeypatch.setattr(sb, "logs_dir", lambda: tmp_path)
    assert _backend().session_log_path("ava-agent-1-shell-2") == Path(
        tmp_path / "ava-agent-1-shell-2.out.log"
    )


# ---------------------------------------------------------------------------
# interface alignment
# ---------------------------------------------------------------------------


def test_pty_backend_is_a_session_backend():
    assert isinstance(_backend(), SessionBackend)


def test_pty_capture_defaults():
    """capture_pane's (lines=200, scrollback=True) defaults are the interface
    contract the sessions.py consumer relies on."""
    import inspect

    pty_sig = inspect.signature(PtySessionBackend.capture_pane)
    assert pty_sig.parameters["lines"].default == 200
    assert pty_sig.parameters["scrollback"].default is True


def test_non_pty_backends_still_raise_send():
    """send is PTY-only like send_keys/capture_pane: the process supervisors
    raise, the PTY backends implement."""
    for backend in (PosixProcSessionBackend(), WinprocSessionBackend()):
        with pytest.raises(NotImplementedError):
            backend.send("s", "text")


def test_get_shell_backend_is_pty_after_switch():
    """get_shell_backend() is PtySessionBackend on POSIX; orchestration
    sessions live on the service backend (the legacy orchestration backend
    is gone)."""
    if IS_WINDOWS:
        pytest.skip("POSIX-only migration")
    assert isinstance(sb.get_shell_backend(), PtySessionBackend)
    assert sb.get_shell_backend() is not sb.get_backend()
