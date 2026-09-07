"""ava.shell unit tests — one-shot `run(cmd)` + persistent PTY session wrapper.

Runs against real detached per-session PTY hosts + real bash. The
`_pty_sessions_env` fixture (session-scoped, tests/ava/conftest.py) pins a tmp
test home and sweeps its sessions; POSIX-only, skips entirely on Windows.

Sessions are distinguished by name prefix. Parallel xdist workers each use a
reserved high-range fake agent-id (`_TEST_AGENT_BASE`) for isolation; tests
clean up only their own agent-prefixed sessions via prefix-scoped `kill_all`.

Session naming format: `ava-agent-{agent_id}-shell-{shell_id}[-<name>]`; the
per-home record namespace provides cluster isolation.
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
    pytest.mark.skipif(IS_WINDOWS, reason="PTY sessions are POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); only the
    # pty-backed session tests want it. `_pty_sessions_env` must come first: the
    # isolation fixture's own kill_all/list calls hit the session backend.
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


def test_run_result_carries_returncode_and_stderr() -> None:
    """The returned value is a str with the command's exit status attached —
    `.returncode` (0 = success, non-zero = failure) and `.stderr`."""
    res = ava.shell.run("echo hi; echo err >&2; exit 3")
    assert res.returncode == 3
    assert res.stderr.strip() == "err"
    assert res.strip() == "hi"


def test_run_returncode_zero_on_success() -> None:
    assert ava.shell.run("echo ok").returncode == 0


def test_run_result_fields_are_read_only() -> None:
    """returncode/stderr are read-only — the hash/equality invariant (which
    follows the string content only) cannot be broken by later mutation."""
    res = ava.shell.run("echo hi; exit 3")
    with pytest.raises(AttributeError, match="has no setter"):
        res.returncode = 0  # type: ignore[misc]
    with pytest.raises(AttributeError, match="has no setter"):
        res.stderr = ""  # type: ignore[misc]


def test_run_result_repr_contains_fields() -> None:
    res = ava.shell.run("echo hi; echo err >&2; exit 3")
    r = repr(res)
    assert r.startswith("ShellResult(")
    assert "'hi\\n'" in r and "returncode=3" in r and "'err\\n'" in r


def test_run_result_equality_and_hash_follow_content_only() -> None:
    """Two results with the same text are equal and hash equal regardless of
    their fields, so content-keyed dicts keep working."""
    from ava.shell import ShellResult

    a = ShellResult("x", returncode=0, stderr="")
    b = ShellResult("x", returncode=3, stderr="boom")
    assert a == b
    assert hash(a) == hash(b)
    d = {a: "v"}
    assert d[b] == "v"


def test_run_result_pickle_roundtrip_preserves_fields() -> None:
    """pickle/copy reconstruct the result with its fields — a str subclass
    whose constructor takes the fields must declare __getnewargs_ex__."""
    import copy
    import pickle

    from ava.shell import ShellResult

    res = ShellResult("hi", returncode=3, stderr="err")
    for clone in (pickle.loads(pickle.dumps(res)), copy.copy(res)):  # noqa: S301 — own object roundtrip
        assert type(clone) is ShellResult
        assert clone == "hi"
        assert clone.returncode == 3
        assert clone.stderr == "err"


def test_run_result_preserves_str_behavior() -> None:
    """The result stays a plain string for every existing usage — str
    methods, equality, membership, formatting."""
    from ava.shell import ShellResult

    out = ava.shell.run("printf 'a\\nb\\n'")
    assert isinstance(out, ShellResult)
    assert isinstance(out, str)
    assert out.splitlines() == ["a", "b"]  # str methods still work
    assert "b" in out  # membership still works
    assert f"{out}" == "a\nb\n"  # formatting still works
    assert out == "a\nb\n"  # equality against a plain str still works


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
    sid = shell.new("test-new", ttl=120)
    try:
        assert isinstance(sid, int)
        assert sid == 0  # first session of a fresh agent
    finally:
        shell.kill(sid)


def test_new_increments_session_index(_agent_row: int) -> None:
    a = shell.new("a", ttl=120)
    b = shell.new("b", ttl=120)
    try:
        assert (a, b) == (0, 1)
    finally:
        shell.kill(a)
        shell.kill(b)


@pytest.mark.flaky  # real session + time.sleep polling (10s deadline)
def test_send_capture_roundtrip_by_id(_agent_row: int) -> None:
    sid = shell.new("test-roundtrip", ttl=120)
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
    sid = shell.new("alpha", ttl=120)
    named = shell.new("dev-server", ttl=120)
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
    sid = shell.new(name="scratch", ttl=120)
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
            shell.new(name=bad, ttl=120)


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
    sid = shell.new("test-kill", ttl=120)
    shell.kill(sid)
    assert sid not in shell.list()


def test_rebuild_uses_new_id_and_old_handle_stays_rejected(_agent_row: int) -> None:
    """A destroyed Persistent Shell id is a stale handle, not a rebuild key.

    The per-agent counter is monotonic, so stateless rebuild publishes its new
    id and capture of the pre-flip id remains correctly rejected.
    """
    old_id = shell.new("before-flip", ttl=120)
    shell.kill(old_id)
    new_id = shell.new("after-flip", ttl=120)
    try:
        assert (old_id, new_id) == (0, 1)
        with pytest.raises(ValueError, match="not this agent's"):
            shell.capture(old_id)
        assert shell.list()[new_id] == "after-flip"
        assert new_id != old_id
    finally:
        shell.kill(new_id)


# ─── send_keys + per-session PTY integration ──────────────────────────────
#
# These run against REAL detached session hosts + REAL bash -l -i (the
# `_pty_sessions_env` fixture), exercising the PtySessionBackend end to end:
# sessions.new -> send -> capture, the key vocabulary (Enter / C-c / Up), and
# kill. A short poll replaces fixed sleeps so a loaded box does not flake.


def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    # 30s default: the host's reader thread shares the box with the whole
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
    sid = shell.new("test-noenter", ttl=120)
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
    sid = shell.new("test-cc", ttl=120)
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
    sid = shell.new("test-up", ttl=120)
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
    workspace — the same base ava.shell.run uses (the PTY host needs a real
    directory; the workspace is created on demand)."""
    from shared.paths import workspace_dir

    sid = shell.new("test-cwd", ttl=120)
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

    sid = shell.new("test-tree", ttl=120)
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
