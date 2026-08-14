"""ava.shell unit tests — one-shot `run(cmd)` + persistent PTY session wrapper.

Runs against the real PTY supervisor daemon + real bash (S6 step 2). The
`_pty_sessions_env` fixture (session-scoped, tests/ava/conftest.py) bootstraps
the daemon under the tmp test home; POSIX-only, skips entirely on Windows.

Sessions are pty sessions owned by the supervisor daemon, distinguished by
name prefix. Parallel xdist workers each use a reserved high-range fake
agent-id (`_TEST_AGENT_BASE`) for isolation; tests clean up only their own
agent-prefixed sessions via prefix-scoped `kill_all`.

Session naming format: `ava-<cluster>-agent-{agent_id}-shell-{shell_id}[-<name>]`.
Tests pin cluster to `test-cluster` via monkeypatch so session names are
deterministic regardless of the real host identity.
"""

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

import ava
import ava._boot
from ava import shell
from shared.platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); only the
    # pty-backed session tests want it. `_pty_sessions_env` must come first: the
    # isolation fixture's own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("_pty_sessions_env", "_isolated_agent"),
]

# The shared PTY/agent isolation fixtures (`_pty_sessions_env`, `_isolated_agent`,
# `_agent_row`) and the `_ensure_agents_meta_row` helper live in
# `tests/ava/conftest.py` so both this module and `test_watcher.py` inherit
# them.


# ─── ava.shell.run (one-shot subprocess; returns stdout, non-zero exit **does not raise**) ───
#
# Contract: shell=True / text=True / check=False / capture_output=True. Uses stdlib
# `subprocess.run` directly, no extra thread or custom polling. Non-zero exit
# (grep no match / pytest fail etc.) is a valid result, agent checks stdout itself, no
# exception raised.


def test_run_returns_stdout_string() -> None:
    out = ava.shell.run("echo hi")
    assert out.strip() == "hi"
    assert isinstance(out, str)


def test_run_non_zero_exit_returns_stdout_without_raising() -> None:
    """check=False: non-zero exit does not raise, stdout still returned (valid use case: grep no-match)."""
    out = ava.shell.run("echo before-exit && exit 3")
    assert "before-exit" in out


def test_run_stderr_only_path_returns_empty_stdout() -> None:
    """Command succeeds but only writes to stderr → stdout empty string (stderr is not returned)."""
    out = ava.shell.run("echo err >&2")
    assert out == ""


def test_run_shell_features_work() -> None:
    """Pipes + redirection = shell=True's main selling point."""
    out = ava.shell.run("echo abc | wc -c")
    assert out.strip() == "4"  # "abc\n" = 4 characters


def test_run_timeout_raises() -> None:
    """Timeout raises subprocess.TimeoutExpired."""
    with pytest.raises(subprocess.TimeoutExpired):
        ava.shell.run("sleep 10", timeout=1.0)


def test_run_explicit_cwd_overrides_default(tmp_path: Path) -> None:
    """When cwd= is passed, command runs in the specified directory."""
    out = ava.shell.run("pwd", cwd=str(tmp_path))
    # macOS tmp goes through /private/var/... symlink, use resolve to normalize
    assert Path(out.strip()).resolve() == tmp_path.resolve()


def test_run_default_cwd_is_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No cwd → runs in agent workspace (consistent with ava.files relative path base)."""
    from shared.config import settings

    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    out = ava.shell.run("pwd")
    expected = tmp_path / "workspaces" / str(ava._boot._agent_id)
    assert Path(out.strip()).resolve() == expected.resolve()
    assert expected.is_dir()  # workspace_dir mkdir on demand


def test_run_default_cwd_is_home_before_identity() -> None:
    """Identity not bound (pre-bootstrap) → falls back to $HOME — consistent with ava.files._resolve's
    pre-identity base, both surfaces share the same base in any state.

    Manually save/restore instead of monkeypatch: this module's `_isolated_agent` (usefixtures)
    already swaps _agent_id to a worker fake id before test body runs; monkeypatch.setattr's snapshot
    would capture that fake id and restore it last in teardown, leaking the fake id to subsequent tests."""
    original = ava._boot._agent_id
    ava._boot._agent_id = None
    original_env = os.environ.pop("AVA_AGENT_ID", None)
    try:
        out = ava.shell.run("pwd")
    finally:
        ava._boot._agent_id = original
        if original_env is not None:
            os.environ["AVA_AGENT_ID"] = original_env
    assert Path(out.strip()).resolve() == Path.home().resolve()


# ─── ava.shell.{new, send, capture, kill, list} (persistent PTY session) ───


def test_new_returns_int_session_id(_agent_row: int) -> None:
    sid = shell.new("test-new")
    try:
        assert isinstance(sid, int)
        assert sid == 0  # first session of a fresh agent
    finally:
        shell.kill(sid)


def test_new_increments_session_index(_agent_row: int) -> None:
    a = shell.new("a")
    b = shell.new("b")
    try:
        assert (a, b) == (0, 1)
    finally:
        shell.kill(a)
        shell.kill(b)


@pytest.mark.flaky  # real session + time.sleep polling (10s deadline)
def test_send_capture_roundtrip_by_id(_agent_row: int) -> None:
    sid = shell.new("test-roundtrip")
    try:
        shell.send(sid, "echo unified-session-ok")
        # Poll instead of a fixed sleep: under a CPU-saturated parallel bucket
        # a 0.5s sleep could elapse before the session flushed the echo (audit
        # round-2 cc-docs-tests P2 — the background-session sibling tests
        # already carry this marker + polling shape).
        deadline = time.time() + 10
        out = ""
        while time.time() < deadline and "unified-session-ok" not in out:
            time.sleep(0.1)
            out = shell.capture(sid)
        assert "unified-session-ok" in out
    finally:
        shell.kill(sid)


def test_list_returns_id_and_name(_agent_row: int) -> None:
    sid = shell.new("alpha")
    named = shell.new("dev-server")
    try:
        listed = shell.list()
        assert listed[sid] == "alpha"
        assert listed[named] == "dev-server"
    finally:
        shell.kill(sid)
        shell.kill(named)


@pytest.mark.flaky  # real session + time.sleep polling (10s deadline)
def test_named_session_capture_and_kill_by_id(_agent_row: int) -> None:
    # The name is a label only — send/capture/kill address a named session by
    # the same int id as an unnamed one.
    sid = shell.new(name="scratch")
    try:
        shell.send(sid, "echo named-session-ok")
        # Poll — see test_send_capture_roundtrip_by_id (audit round-2 P2).
        deadline = time.time() + 10
        out = ""
        while time.time() < deadline and "named-session-ok" not in out:
            time.sleep(0.1)
            out = shell.capture(sid)
        assert "named-session-ok" in out
    finally:
        shell.kill(sid)
    assert sid not in shell.list()


def test_new_rejects_invalid_name(_agent_row: int) -> None:
    for bad in ("Dev Server", "1abc", "-x", "a_b"):
        with pytest.raises(ValueError, match="lowercase slug"):
            shell.new(name=bad)


def test_resolve_does_not_conflate_id_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    # `shell-1` must not match `shell-12-<name>`: the suffix match requires a
    # dash right after the full id.
    from ava.shell import sessions

    prefix = sessions._shell_prefix()
    monkeypatch.setattr(
        sessions,
        "_own_sessions",
        lambda: [f"{prefix}12-watcher", f"{prefix}1"],
    )
    assert sessions._resolve(1) == f"{prefix}1"
    assert sessions._resolve(12) == f"{prefix}12-watcher"


def test_operations_reject_foreign_id(_agent_row: int) -> None:
    # A session id that does not resolve to one of this agent's sessions raises.
    with pytest.raises(ValueError, match="not this agent's"):
        shell.send(99999, "echo nope")


def test_kill_removes_session(_agent_row: int) -> None:
    sid = shell.new("test-kill")
    shell.kill(sid)
    assert sid not in shell.list()


def test_stale_session_cleanup_spares_lone_shell(_agent_row: int) -> None:
    """Resurrect/respawn's stale-session cleanup must not kill a surviving shell.

    `_kill_stale_session` targets the agent PROCESS — a native-supervisor
    record (`ava-<cluster>-agent-{id}`) — never the shell backend. The agent's
    shells live in a different subsystem (PTY supervisor) under a longer name,
    so a stale-process kill can never reach them. Here no process record exists
    (the common post-exit state), so the kill is a noop and the lone shell
    survives.
    """
    from ops import agent_launch

    sid = shell.new(name="claude")  # the lone surviving session
    try:
        agent_launch._kill_stale_session(_agent_row)
        assert sid in shell.list()  # process kill never touches the shell backend
    finally:
        shell.kill(sid)


def test_stale_session_cleanup_kills_process_session_spares_shell(_agent_row: int) -> None:
    """_kill_stale_session removes a lingering agent PROCESS while sparing the shell.

    Companion to test_stale_session_cleanup_spares_lone_shell: when a stale agent
    PROCESS (a native-supervisor record, here a real detached `sleep`) IS still
    present — the resurrect/respawn race — the kill removes it, while the agent's
    shell (a different subsystem — the PTY supervisor) is left untouched. No
    prefix-match hazard is even possible now: the process and its shells no
    longer share a namespace.
    """
    from ops import agent_launch
    from shared import posixproc
    from shared.cluster import session_name

    agent_sess = session_name(f"agent-{_agent_row}")
    # a real detached "stale process" tracked by the native supervisor
    posixproc.new_session(agent_sess, ["/bin/sleep", "300"], Path.cwd(), env=dict(os.environ))
    sid = shell.new(name="claude")
    try:
        assert posixproc.has_session(agent_sess)
        agent_launch._kill_stale_session(_agent_row)
        # the stale process session is killed ...
        assert not posixproc.has_session(agent_sess)
        # ... but the shell survived
        assert sid in shell.list()
    finally:
        shell.kill(sid)
        posixproc.kill_session(agent_sess, graceful=False)


# ─── send_keys + PTY supervisor integration (S6 step 2) ─────────────────────
#
# These run against the REAL supervisor daemon + REAL bash -l -i (the
# `_pty_sessions_env` fixture), exercising the PtySessionBackend end to end:
# sessions.new -> send -> capture, the key vocabulary (Enter / C-c / Up), and
# kill. A short poll replaces fixed sleeps so a loaded box does not flake.


def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    # 30s default: the daemon's reader thread shares the box with the whole
    # parallel suite, and under CI runner CPU oversubscription the reap can
    # lag well past 10s (2026-08-09: 4 CI runs failed on the same kill test
    # in a busy window; the same runner passed minutes earlier).
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _capture_until(sid: int, needle: str) -> str:
    def _seen() -> bool:
        # Whitespace-normalized: with a long cwd the prompt + text exceeds the
        # terminal width and pyte wraps the line mid-word, splitting any raw
        # substring across two rows (CI observed the wrap; W1b's shared
        # supervisor tests normalize the same way).
        return needle in "".join(shell.capture(sid).split())

    assert _wait_for(_seen), f"capture never contained {needle!r}"
    return shell.capture(sid)


def _ready(sid: int) -> None:
    """Wait until the session's shell is up and reading input.

    The daemon starts `bash -l -i`, and a login shell's startup (profile
    scripts, prompt setup) can take seconds on a loaded box — text sent
    before the shell reads stdin can be swallowed or mis-echoed. The canary
    command round-trips through bash itself, so it proves send → execute →
    capture all work before the test's own scenario runs (the same barrier
    the shared supervisor tests use). The session id makes the marker
    unique per test, so a stale capture can never satisfy it."""
    marker = f"__pty_ready_{sid}__"
    shell.send(sid, f"echo {marker}")
    _capture_until(sid, marker)


def test_send_enter_false_then_send_keys_enter_submits(_agent_row: int) -> None:
    """sessions.send(enter=False) types text without submitting; a later
    send_keys('Enter') submits it — the text/Enter split the SDK contract
    promises (a combined write races TUI programs)."""
    sid = shell.new("test-noenter")
    try:
        _ready(sid)
        shell.send(sid, "echo submitted-by-enter", enter=False)
        # the typed text must sit on the line editor, NOT have run
        # (whitespace-normalized: the typed line can wrap mid-word on a box
        # whose cwd makes the prompt exceed the terminal width)
        assert _wait_for(lambda: "echosubmitted-by-enter" in "".join(shell.capture(sid).split()))
        lines = [ln.strip() for ln in shell.capture(sid).split("\n")]
        assert "submitted-by-enter" not in lines  # no bare output line yet
        shell.send_keys(sid, "Enter")
        _capture_until(sid, "submitted-by-enter")
    finally:
        shell.kill(sid)


def test_send_keys_ctrl_c_interrupts_foreground(_agent_row: int) -> None:
    """C-c interrupts a foreground job (here `cat` blocking on stdin) and the
    shell survives to run the next command."""
    sid = shell.new("test-cc")
    try:
        _ready(sid)
        shell.send(sid, "cat")
        time.sleep(0.5)
        shell.send_keys(sid, "C-c")
        shell.send(sid, "echo after-interrupt")
        _capture_until(sid, "after-interrupt")
        assert sid in shell.list()  # the shell is still alive
    finally:
        shell.kill(sid)


def test_send_keys_up_arrow_recalls_history(_agent_row: int) -> None:
    """Up recalls the previous command from shell history; Enter re-runs it —
    the raw-key path that drives interactive programs."""
    sid = shell.new("test-up")
    try:
        _ready(sid)
        shell.send(sid, "echo hist-marker-1")
        _capture_until(sid, "hist-marker-1")
        shell.send_keys(sid, "Up", "Enter")
        _capture_until(sid, "hist-marker-1")  # the re-run echoes it again
        # the recalled line must have EXECUTED again: count bare output lines
        lines = [ln.strip() for ln in shell.capture(sid).split("\n")]
        assert lines.count("hist-marker-1") >= 2
    finally:
        shell.kill(sid)


def test_new_default_cwd_is_agent_workspace(_agent_row: int) -> None:
    """A session created without an explicit cwd starts in the agent's
    workspace — the same base ava.shell.run uses (the PTY daemon needs a real
    directory; the workspace is created on demand)."""
    from shared.paths import workspace_dir

    sid = shell.new("test-cwd")
    try:
        _ready(sid)
        shell.send(sid, "pwd")
        _capture_until(sid, str(workspace_dir(_agent_row)))  # normalized match
    finally:
        shell.kill(sid)


def test_kill_reaps_session_and_foreground_child(_agent_row: int) -> None:
    """kill() removes the session, and a FOREGROUND child is reaped with it —
    the PTY kill signals the shell's group AND the tty's foreground group (a
    job backgrounded into its own pgrp outlives the tty, exactly like a shell session
    kill-session)."""
    import json

    import psutil

    from ava.shell import sessions as _sessions
    from shared.paths import run_dir

    sid = shell.new("test-tree")
    try:
        _ready(sid)
        full = _sessions._resolve(sid)
        rec = json.loads((run_dir() / "pty" / f"{full}.json").read_text())
        shell_pid = int(rec["pid"])
        assert psutil.pid_exists(shell_pid)
        # a foreground child blocks the shell; it gets its own pgrp (job
        # control), which the kill must signal alongside the shell's group.
        shell.send(sid, "sleep 300")
        kids: list[psutil.Process] = []
        deadline = time.time() + 10.0
        while time.time() < deadline:
            kids = [c for c in psutil.Process(shell_pid).children() if "sleep" in (c.name() or "")]
            if kids:
                break
            time.sleep(0.1)
        assert kids, "the foreground sleep never started"
        child_pid = kids[0].pid
        shell.kill(sid)
        assert _wait_for(lambda: not psutil.pid_exists(child_pid), timeout=30.0), (
            "the foreground sleep survived the session kill"
        )
        assert _wait_for(lambda: not psutil.pid_exists(shell_pid), timeout=30.0), (
            "the shell survived the session kill"
        )
        assert sid not in shell.list()
    finally:
        with contextlib.suppress(ValueError, RuntimeError):
            shell.kill(sid)
