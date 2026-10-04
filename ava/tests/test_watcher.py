"""ava.watcher unit tests — launch writes the agent's script verbatim plus a
generated bootstrap file (identity + watchdog + runpy), and runs them in a
PTY session whose command line tees output to a log file and session capture,
then ends with the CLI completion notice (`ava agents send ... --source
watcher:N`); cron/at build a script then spawn.

The `pty_service` fixture (tests/path_scoped/pty_service.py) runs a real
pty-sessions service under the tmp test home; the session tests are
POSIX-only (skip on Windows — the pty-sessions service is POSIX-only)."""

from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

import psycopg
import pytest

import ava
from ava import watcher
from ava.shell import background
from base.native_process.os_platform import IS_WINDOWS
from tests.path_scoped.pty_shells import wait_for

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); apply it
    # module-wide here since every watcher session test needs the fake-id +
    # pty cleanup isolation. `pty_service` first — the isolation fixture's
    # own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("pty_service", "_isolated_agent"),
]


def _is_live_watcher(wid: int, name: str = "test-watcher") -> bool:
    return ava.shell.sessions.list().get(wid) == name


def _boot_text(wid: int) -> str:
    return (watcher._watchers_dir() / f"watcher_{wid}_boot.py").read_text()


def test_launch_creates_watcher_session(_agent_row: int) -> None:
    # A long-running watcher keeps its session alive and listed under its name.
    code = "import time\ntime.sleep(60)\n"
    wid = watcher.launch(code, timeout="1h", name="test-launch")
    try:
        assert isinstance(wid, int)
        assert _is_live_watcher(wid, "test-launch")
    finally:
        ava.shell.sessions.kill(wid)


def _ttl_deadline(
    conn: psycopg.Connection, agent_id: int, session_id: int
) -> datetime.datetime | None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def test_launch_registers_ttl_row_from_timeout(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #2614: a launch watcher records its timeout as the session TTL —
    the shell monitor page must show a deadline for every session."""
    before = datetime.datetime.now(datetime.UTC)
    wid = watcher.launch("import time\ntime.sleep(60)\n", timeout="30m", name="test-launch-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert (
            before + datetime.timedelta(minutes=29)
            <= deadline
            <= datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=31)
        )
    finally:
        ava.shell.sessions.kill(wid)


def test_cron_registers_ttl_folded_to_end_time(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: a cron watcher's session TTL IS its deadline — the
    registration's end_time (defaulted to now + 7 days, task #2617), folded
    as `deadline - now` at mount time — never the old 24h placeholder."""
    before = datetime.datetime.now(datetime.UTC)
    wid = watcher.cron("0 4 * * *", "wake", name="test-cron-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        delta = deadline - before
        # Default end = the registration minute + 7 days (minute-truncated).
        assert datetime.timedelta(days=7) - datetime.timedelta(minutes=1) <= delta
        assert delta <= datetime.timedelta(days=7, minutes=1)
        # The system-side true value is exempt from the 24h sessions.new cap.
        assert deadline > datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=6)
    finally:
        ava.shell.sessions.kill(wid)


def test_cron_registers_ttl_from_explicit_end(db_conn: psycopg.Connection, _agent_row: int) -> None:
    """An explicit end_time beyond the user-session cap still folds exactly:
    the watcher TTL is the stored deadline, uncapped (task #3411)."""
    end = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=30)
    wid = watcher.cron("0 4 * * *", "wake", end_time=end, name="test-cron-ttl-explicit")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert abs((deadline - end).total_seconds()) < 10
    finally:
        ava.shell.sessions.kill(wid)


def test_launch_timeout_beyond_24h_is_not_capped(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: a launch watcher's timeout IS its deadline — a 48h
    watchdog registers a ~48h session TTL. The 24h cap protects user sessions
    (sessions.new / run_background), not system watchers."""
    wid = watcher.launch("import time\ntime.sleep(60)\n", timeout="48h", name="test-cap-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        assert deadline >= datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=47, minutes=59
        )
    finally:
        ava.shell.sessions.kill(wid)


def test_at_registers_ttl_row_from_fires_at_plus_grace(
    db_conn: psycopg.Connection, _agent_row: int
) -> None:
    """Task #3411: an at watcher's session TTL is its fire moment plus the
    grace (`base.daemon.schedules.watcher.AT_SESSION_TTL_GRACE_SECONDS`), so the wake
    delivery and the session's exit notice complete before the reaper may
    reclaim it."""
    from base.daemon.schedules.watcher import AT_SESSION_TTL_GRACE_SECONDS

    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
    wid = watcher.at(when, "wake", name="test-at-ttl")
    try:
        deadline = _ttl_deadline(db_conn, _agent_row, wid)
        assert deadline is not None
        expected = when + datetime.timedelta(seconds=AT_SESSION_TTL_GRACE_SECONDS)
        assert abs((deadline - expected).total_seconds()) < 10
    finally:
        ava.shell.sessions.kill(wid)


def test_watcher_child_dies_when_the_pty_service_dies(
    _agent_row: int, tmp_path: pathlib.Path
) -> None:
    """Task #1726 acceptance: a watcher child must NEVER outlive its pty service.

    When the service dies (crash / SIGKILL), the login shell dies with it
    (the master closes, which hangs it up) and the child is reparented to
    init — still alive, still firing cron/at; 49 of 85 watcher processes on
    the fleet host were such multi-generation orphans (8/19 onwards). The
    bootstrap's orphan guard
    compares getppid() against the boot-time parent and hard-exits within a
    few seconds. The child IGNORES SIGHUP, so its death can only come from
    the guard — the test fails if the guard regresses and the child survives.
    """
    import signal
    from contextlib import suppress

    import psutil

    from ava.shell import sessions as _sessions

    pidfile = tmp_path / "child.pid"
    pending_pidfile = tmp_path / "child.pid.pending"
    code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"with open({str(pending_pidfile)!r}, 'w') as _pidfile:\n"
        "    _pidfile.write(str(os.getpid()))\n"
        f"os.replace({str(pending_pidfile)!r}, {str(pidfile)!r})\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    wid = watcher.launch(code, timeout="1h", name="test-orphan-guard")

    deadline = time.time() + 20
    # The child publishes the ready file atomically after its contents are
    # closed, so existence means the PID is complete rather than merely open.
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.2)
    assert pidfile.exists(), "watcher child never started"
    child_pid = int(pidfile.read_text())

    try:
        # The child's parent is the session shell; the grandparent is the pty
        # service — the process whose death orphans the child.
        child = psutil.Process(child_pid)
        shell = psutil.Process(child.ppid())
        host = psutil.Process(shell.ppid())
        assert "services.agent_runner.pty_sessions.daemon" in " ".join(host.cmdline())

        os.kill(host.pid, signal.SIGKILL)

        # The guard exits within its 5s interval; 20s is generous CI slack.
        deadline = time.time() + 20
        while time.time() < deadline and psutil.pid_exists(child_pid):
            try:
                if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                    break  # dead, waiting for init to reap
            except psutil.NoSuchProcess:
                break
            time.sleep(0.2)
        assert (
            not psutil.pid_exists(child_pid)
            or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        ), "watcher child survived its host"
    finally:
        # A failed assertion must not leak the sleeper (the session record is
        # gone with the host, so fixture teardown cannot reach it).
        with suppress(psutil.Error):
            psutil.Process(child_pid).kill()
        with suppress(ValueError, subprocess.CalledProcessError):
            _sessions.kill(wid)


def test_launch_requires_timeout_and_name() -> None:
    # Both timeout and name are required
    with pytest.raises(TypeError):
        watcher.launch("import ava\n")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        watcher.launch("import ava\n", timeout="1h")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        watcher.launch("import ava\n", name="test")  # type: ignore[call-arg]


def test_launch_arms_watchdog_in_boot_file(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generated bootstrap must arm a daemon timer that hard-exits with
    # code 124 at the deadline — that is what bounds a custom watcher's
    # lifetime. The timeout reason is printed so it lands in the log file.
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout=120, name="test-watchdog")
    boot = _boot_text(wid)
    assert "threading.Timer(120.0" in boot
    assert "os._exit(124)" in boot
    assert "120s limit" in boot


def test_watchdog_message_formats_fractional_seconds(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout=0.5, name="test-fractional")
    assert "0.5s limit" in _boot_text(wid)


def test_boot_without_watchdog_has_no_timer(tmp_path: pathlib.Path) -> None:
    # at/cron scripts self-terminate, so their bootstrap arms no watchdog.
    # The orphan guard (task #1726) is armed regardless — it is not a
    # watchdog: it only hard-exits a watcher whose session chain died, and
    # never bounds a healthy watcher's lifetime.
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert "Timer" not in boot
    assert "os._exit(124)" not in boot
    assert "os._exit(125)" in boot  # the orphan guard's exit code


def test_boot_arms_orphan_guard(tmp_path: pathlib.Path) -> None:
    """Task #1726: every generated bootstrap must arm the orphan guard — a
    daemon thread that compares getppid() against the boot-time parent and
    hard-exits (code 125) on a mismatch, so a watcher child dies within
    seconds of its pty host dying instead of firing cron/at forever as a
    ppid=1 orphan. Armed BEFORE `import ava`: a hung import must not keep an
    orphan alive."""
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert "_parent_pid = os.getppid()" in boot
    assert "def _orphan_guard" in boot
    assert "os.getppid() != _parent_pid" in boot
    assert "os._exit(125)" in boot
    guard_line = boot.index("_orphan_guard")
    assert guard_line < boot.index("import ava")
    compile(boot, "<boot>", "exec")  # must be valid Python


def test_boot_orphan_guard_message_names_session_gone(tmp_path: pathlib.Path) -> None:
    # The guard's stderr line lands in the watcher's log — a debugger must be
    # able to tell an orphan-guard exit (125) from a watchdog timeout (124).
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert "[watcher] session gone (pty session ended)" in boot
    assert "os._exit(125)" in boot


def test_boot_loads_plugins_before_running_script(tmp_path: pathlib.Path) -> None:
    # A fresh child's `import ava` is the factory module without the agent
    # process's plugin setattrs, so the bootstrap must load plugin namespaces
    # (ava.tasks etc.) before it runpys the watcher script — otherwise the script
    # AttributeErrors on any plugin namespace. Order matters: load then run.
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert "ava.ensure_plugins_loaded()" in boot
    assert boot.index("ava.ensure_plugins_loaded()") < boot.index("runpy.run_path")
    compile(boot, "<boot>", "exec")  # must be valid Python


def test_boot_propagates_failures_without_catching(tmp_path: pathlib.Path) -> None:
    # try/finally around runpy only cleans up the generated files — no except
    # anywhere, so a crash still prints its traceback into the log and exits
    # non-zero; the shell-level notice reports the code. Only the cleanup's
    # own OSError is swallowed (unlink failure is harmless).
    boot = watcher._build_boot(tmp_path / "x.py", 60.0, 42)
    assert "try:" in boot
    assert "finally:" in boot
    # The runpy try has NO except — a crash propagates (R1-8: only the
    # finally's own cleanup — deleting the generated files — is fail-soft).
    assert "except Exception" not in boot.split("finally:")[0]
    assert "except BaseException" not in boot
    assert "runpy.run_path" in boot
    compile(boot, "<boot>", "exec")  # must be valid Python


def test_boot_self_cleans_generated_files(tmp_path: pathlib.Path) -> None:
    # The finally block deletes the script + the bootstrap itself — a watcher
    # reads them exactly once at launch, so normal exits leave the watchers
    # dir empty instead of accumulating a script graveyard.
    script = tmp_path / "watcher_3.py"
    boot = watcher._build_boot(script, None, 42)
    assert "finally:" in boot
    assert "os.unlink(_p)" in boot
    assert "except OSError" in boot
    assert str(script) in boot  # the script path is unlinked too
    assert "__file__" in boot  # and the bootstrap deletes itself
    compile(boot, "<boot>", "exec")


@pytest.mark.flaky  # real watcher session + time.sleep polling (5s deadline)
def test_shell_kill_stops_watcher(_agent_row: int) -> None:
    # A watcher is an ordinary session — the generic session kill stops it.
    code = "import time\ntime.sleep(60)\n"
    wid = watcher.launch(code, timeout="1h", name="test-launch")
    ava.shell.sessions.kill(wid)
    # Poll instead of a fixed sleep: session teardown latency varies under CPU
    # contention (audit round-2 cc-docs-tests P2).
    deadline = time.time() + 5
    while time.time() < deadline and _is_live_watcher(wid, "test-launch"):
        time.sleep(0.05)
    assert not _is_live_watcher(wid, "test-launch")


def test_spawn_kills_session_when_send_fails(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose launch command never sent must not linger as an idle
    login shell under the watcher's name until its TTL (up to 7 days for a
    default cron) — kill it and propagate the failure."""
    from ava.shell import sessions as _sessions

    captured: dict[str, int] = {}

    def fail_send(session_id: int, _cmd: str) -> None:
        captured["session_id"] = session_id
        raise RuntimeError("session send failed")

    monkeypatch.setattr(_sessions, "send", fail_send)

    with pytest.raises(RuntimeError, match="session send failed"):
        watcher.launch("import ava\n", timeout="1h", name="test-send-fail")

    assert captured["session_id"] not in ava.shell.sessions.list()


def test_at_builds_and_spawns_without_watchdog(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # at/cron scripts self-terminate, so they spawn with no watchdog (None).
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        captured["watchdog_secs"] = watchdog_secs
        captured["name"] = name
        captured["kind"] = kw.get("kind")
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    # Derived, not a literal year: `at` rejects a past `when`, so a pinned date
    # silently becomes a failing test the moment the clock passes it. Keeps the
    # trailing-Z ISO shape, which is the parse path this test exercises.
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    wid = watcher.at(when, "ping later", name="test-at")
    assert wid == 7
    assert captured["watchdog_secs"] is None
    assert "_wake(_MESSAGE)" in captured["code"]
    assert "ping later" in captured["code"]


def test_cron_spawns_without_watchdog(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured.update(
            code=code,
            watchdog_secs=watchdog_secs,
            name=name,
            kind=kw.get("kind"),
            expr=kw.get("cron_expr"),
        )
        return 3

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    watcher.cron("0 3 * * *", "daily", name="test-cron")
    assert captured["watchdog_secs"] is None


def test_watcher_apis_forward_failure_notify(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []

    def fake_spawn(_code: str, _watchdog_secs: float | None, _name: str, **kw: object) -> int:
        notify = kw["notify"]
        assert isinstance(notify, str)
        captured.append(notify)
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    watcher.launch("pass", timeout="1h", name="test-launch-notify", notify="failure")
    watcher.at(when, "ping later", name="test-at-notify", notify="failure")
    watcher.cron("0 3 * * *", "daily", name="test-cron-notify", notify="failure")
    assert captured == ["failure", "failure", "failure"]


def test_watcher_rejects_unknown_notify(_agent_row: int) -> None:
    with pytest.raises(ValueError, match="notify must be one of"):
        watcher.launch("pass", timeout="1h", name="test-invalid-notify", notify="success")


def test_launch_carries_agent_and_session_to_child(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identity crosses the process boundary inlined into the generated
    # bootstrap (the session env allowlist does NOT forward AVA_AGENT_ID —
    # Task #856/#964): the child must see its agent id as AVA_AGENT_ID.
    # The watcher's session id rides as an env var on the run command so the
    # time watchers can tag their wake-ups.
    from ava.shell import sessions as _sessions

    captured: dict[str, Any] = {}
    monkeypatch.setattr(_sessions, "send", lambda _id, cmd: captured.update(cmd=cmd))  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout="1h", name="test-watcher")
    launch = (watcher._watchers_dir() / f"watcher_{wid}_launch.sh").read_text()
    assert f"AVA_WATCHER_SESSION_ID={wid} " in launch
    boot = _boot_text(wid)
    # Identity is inlined; ava is imported for init_globals
    assert f'os.environ["AVA_AGENT_ID"] = "{_agent_row}"' in boot
    assert "process_context" not in boot
    assert "import ava" in boot
    assert "runpy.run_path" in boot
    assert "init_globals" in boot


def test_launch_writes_script_verbatim_and_runs_via_runpy(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The script file is the agent's code unchanged — nothing prepended — so a
    # leading `from __future__` import (must be the first statement of a file)
    # stays valid. Watchdog and runpy live in the separate generated bootstrap
    # file; identity is inlined there (the session env allowlist does not
    # forward AVA_AGENT_ID — Task #856/#964).
    from ava.shell import sessions as _sessions

    captured: dict[str, Any] = {}
    monkeypatch.setattr(_sessions, "send", lambda id, cmd: captured.update(id=id, cmd=cmd))  # pyright: ignore[reportUnknownArgumentType]

    code = "from __future__ import annotations\nimport ava\n"
    wid = watcher.launch(code, timeout="1h", name="test-launch")

    script = watcher._watchers_dir() / f"watcher_{wid}.py"
    assert script.read_text() == code  # verbatim, no bootstrap prepended
    boot = _boot_text(wid)
    assert "runpy.run_path" in boot
    # Identity is inlined into the bootstrap
    assert f'os.environ["AVA_AGENT_ID"] = "{_agent_row}"' in boot
    assert "process_context" not in boot
    assert "import ava" in boot
    assert "init_globals" in boot
    assert f"watcher_{wid}.py" in boot
    launch = (watcher._watchers_dir() / f"watcher_{wid}_launch.sh").read_text()
    assert f"watcher_{wid}_boot.py" in launch


# -- Completion notice (shell-level, fires on every exit path) -----------------


def test_spawn_line_tees_and_notifies(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # The command line must tee the child's output to the per-agent log file
    # and session capture, then end with the CLI completion notice + session
    # close: the notice is sent from the shell level so a crashed or
    # hard-killed child cannot skip it, and `; exit $_ec` closes the session
    # even when delivery fails — a lingering shell is a live session nobody
    # ever intended to keep (Task #1115 bug B).
    from ava.shell import sessions as _sessions

    captured: dict[str, Any] = {}
    monkeypatch.setattr(_sessions, "send", lambda _id, cmd: captured.update(cmd=cmd))  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout="1h", name="test-notice")

    import shlex

    sent = captured["cmd"]
    assert shlex.split(sent) == [".", str(watcher._watchers_dir() / f"watcher_{wid}_launch.sh")]
    cmd = (watcher._watchers_dir() / f"watcher_{wid}_launch.sh").read_text().splitlines()[1]
    assert ".shell_logs" in cmd  # output is teed to the workspace log dir
    assert "2>&1 | tee" in cmd
    assert "_ec=${PIPESTATUS[0]}" in cmd
    assert "agents send" in cmd
    assert f"--source watcher:{wid}" in cmd
    assert "exited with code ${_ec}" in cmd
    assert "--tail-file" in cmd
    assert cmd.endswith("; exit $_ec")


@pytest.mark.flaky  # real pty watcher session + time.sleep polling (15s deadline)
def test_watcher_completion_notice_e2e(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """End-to-end through a real pty session: when the watcher child exits, the
    completion notice fires (CLI invoked with the watcher source) and the
    session closes itself. The CLI is faked with a script that records its
    argv, so no gateway is needed."""
    argv_file = tmp_path / "argv.txt"
    fake_cli = tmp_path / "fake-ava"
    fake_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_file}\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(background, "cli_path", lambda: fake_cli)

    # `exit 7` exits the shell subshell wrapping the python command — use a
    # python-level SystemExit instead so the exit code flows through the child.
    wid = watcher.launch("raise SystemExit(7)\n", timeout="1h", name="test-e2e")

    deadline = time.time() + 15
    while time.time() < deadline and not argv_file.exists():
        time.sleep(0.3)
    assert argv_file.exists(), "completion notice never fired"
    argv = argv_file.read_text().splitlines()
    assert argv[0] == "agents"
    assert argv[1] == "send"
    assert argv[2] == str(ava.self.AGENT_ID)
    assert "exited with code" in argv[3]  # ${_ec} expanded by the session shell
    assert "--source" in argv
    assert f"watcher:{wid}" in argv

    # The session closes itself after the notice is delivered.
    deadline = time.time() + 10
    while time.time() < deadline and wid in ava.shell.sessions.list():
        time.sleep(0.3)
    assert wid not in ava.shell.sessions.list()


def test_watcher_launch_during_shell_startup_with_long_paths(
    _agent_row: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    unit_home: pathlib.Path,
) -> None:
    """Long notice paths must survive the shell's startup canonical input mode."""
    # Delay readline startup deterministically; a new PTY is initially canonical.
    (unit_home / ".bash_profile").write_text("sleep 1\n")
    # Darwin's canonical input limit is smaller than Linux's; stay below
    # each platform's filesystem path limit while exceeding its input line.
    depth = 1 if sys.platform == "darwin" else 6
    carrier_dir = tmp_path / "carrier with 'quotes'"
    carrier_dir.mkdir()
    monkeypatch.setattr(watcher, "_watchers_dir", lambda: carrier_dir)
    long_dir = tmp_path.joinpath(*("long-path-" + "a" * 180 for _ in range(depth)))
    long_dir.mkdir(parents=True)
    argv_file = long_dir / "argv.txt"
    fake_cli = long_dir / "fake-ava"
    fake_cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_file}'\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(background, "cli_path", lambda: fake_cli)
    monkeypatch.setattr(background, "output_dir", lambda: long_dir)
    wid = watcher.launch("raise SystemExit(7)\n", timeout="1h", name="test-long-path")
    assert wait_for(argv_file.exists, timeout=15), ava.shell.sessions.capture(wid, lines=30)
    argv = argv_file.read_text().splitlines()
    assert "exited with code 7." in argv[3]
    assert f"watcher:{wid}" in argv
    output_path = long_dir / f"{wid}_test-long-path.log"
    assert f"Full output at {output_path}." in argv[3]
    assert str(output_path) == argv[argv.index("--tail-file") + 1]
    assert wait_for(lambda: wid not in ava.shell.sessions.list(), timeout=10)
    assert not (watcher._watchers_dir() / f"watcher_{wid}_launch.sh").exists()
    assert not (watcher._watchers_dir() / f"watcher_{wid}_boot.py").exists()
    assert not (watcher._watchers_dir() / f"watcher_{wid}.py").exists()


# -- Agent identity in the child (Task #964 regression) ----------------------


def test_boot_inlines_agent_identity(tmp_path: pathlib.Path) -> None:
    """The bootstrap must set AVA_AGENT_ID itself: the session env allowlist
    (base/host/env/registry.py child_env, Task #856) deliberately does
    not forward agent-scope knobs to session children, so a child that relied
    on inheritance would see ava.self.AGENT_ID=None and its wake-up
    send_message would 422 on /api/agents/None/messages (Task #964). The
    inline assignment must run before any SDK use — including `import ava`
    itself: a stale AVA_AGENT_ID in the session env (a server freezes
    its first session's env) would otherwise establish the WRONG identity at
    import time (2026-08-09: every watcher child on the shared server woke
    agent 2959 instead of its owner)."""
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert 'os.environ["AVA_AGENT_ID"] = "42"' in boot
    env_line = boot.index('os.environ["AVA_AGENT_ID"]')
    assert env_line < boot.index("import ava")  # before the ava import, not just before use
    assert env_line < boot.index("ava.ensure_plugins_loaded()")
    assert env_line < boot.index("runpy.run_path")
    compile(boot, "<boot>", "exec")  # must be valid Python


@pytest.mark.flaky  # real pty watcher session + file-polling (15s deadline)
def test_watcher_child_sees_agent_identity(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """End-to-end regression (Task #964): a real watcher child must see its
    agent id via ava.self.AGENT_ID — the wake-up send_message depends on it.
    Before the fix the child had no AVA_AGENT_ID (None) and every scheduled
    watcher died with a 422 on /api/agents/None/messages."""
    from ava.shell import sessions as _sessions

    out = tmp_path / "identity.txt"
    code = (
        "import ava\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(ava.self.AGENT_ID))\n"
    )
    # Fake the completion-notice CLI so no gateway is needed.
    fake_cli = tmp_path / "fake-ava"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(background, "cli_path", lambda: fake_cli)

    wid = watcher.launch(code, timeout="1h", name="test-identity")

    deadline = time.time() + 15
    while time.time() < deadline and not out.exists():
        time.sleep(0.3)
    assert out.exists(), "watcher child never wrote its identity"
    assert out.read_text() == str(_agent_row)  # NOT the stale session-env id
    # The watcher self-terminates and the session closes itself after the
    # completion notice; if the child is somehow still alive, clean it up but
    # tolerate an already-closed session (the kill path resolves by name).
    from contextlib import suppress

    # Tolerate every "session is already gone" failure mode: `_resolve` raises
    # ValueError for a session that is not ours, and the pty kill path
    # raises subprocess.CalledProcessError when the session vanished mid-kill
    # (exit 1 from kill-session) — a teardown must not fail on a corpse.
    with suppress(ValueError, subprocess.CalledProcessError):
        _sessions.kill(wid)


def test_boot_overrides_stale_env_identity(tmp_path: pathlib.Path) -> None:
    """The generated bootstrap must override a stale AVA_AGENT_ID in the
    environment: `import ava` establishes identity FROM THE ENV at import
    time, so a session whose env carries another agent's id (a server
    freezes the env of its first session) would otherwise bind the WRONG
    identity and every wake-up send_message would route there. Regression for
    2026-08-09 — every watcher child on the shared server woke agent 2959
    instead of its owner. Runs the boot file in a real subprocess with the
    env poisoned; the child must report the INLINED id, not the stale one."""
    script = tmp_path / "probe.py"
    out = tmp_path / "identity.txt"
    script.write_text(
        "import ava\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(ava.self.AGENT_ID))\n"
    )
    boot_path = tmp_path / "probe_boot.py"
    boot_path.write_text(watcher._build_boot(script, None, 4242))

    env = {**os.environ, "AVA_AGENT_ID": "9999"}  # stale/wrong identity
    res = subprocess.run(  # noqa: S603 — repo-internal generated boot file
        [sys.executable, str(boot_path)],
        cwd=pathlib.Path(__file__).resolve().parents[2],  # repo root: `import ava` resolves
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, f"boot failed: {res.stderr}"
    assert out.read_text() == "4242", "the stale env identity won over the inline id"


def test_watcher_child_overrides_stale_session_identity(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A stale AVA_AGENT_ID in the session env must NOT win: the bootstrap
    inlines the spawning agent's id BEFORE `import ava`. Regression for
    2026-08-09 — every watcher child on the shared supervisor woke agent 2959
    instead of its owner, because the server's frozen env carried 2959 and
    `import ava` established it before the inline assignment ran. Here the
    session env is poisoned with a wrong id (via the backend's envfile); the
    child must still report the real agent id."""
    from ava.shell import sessions as _sessions

    out = tmp_path / "identity.txt"
    code = (
        "import ava\n"
        "import pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(str(ava.self.AGENT_ID))\n"
    )
    fake_cli = tmp_path / "fake-ava"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(background, "cli_path", lambda: fake_cli)

    stale = _agent_row + 1  # a WRONG identity, as if frozen into the session env

    def forward_env(*, activate_venv: bool = True) -> dict[str, str]:
        del activate_venv
        return {"AVA_AGENT_ID": str(stale)}

    monkeypatch.setattr(
        _sessions,
        "forward_env_dict",
        forward_env,
    )

    wid = watcher.launch(code, timeout="1h", name="test-stale-id")

    deadline = time.time() + 15
    while time.time() < deadline and not out.exists():
        time.sleep(0.3)
    assert out.exists(), "watcher child never wrote its identity"
    assert out.read_text() == str(_agent_row), "the stale session identity won"
    from contextlib import suppress

    # Tolerate every "session is already gone" failure mode: `_resolve` raises
    # ValueError for a session that is not ours, and the pty kill path
    # raises subprocess.CalledProcessError when the session vanished mid-kill
    # (exit 1 from kill-session) — a teardown must not fail on a corpse.
    with suppress(ValueError, subprocess.CalledProcessError):
        _sessions.kill(wid)


# ─── watchers carry no registry; nothing dedupes or restarts them ───────────
# (decisions/2026-09-27-watchers-are-never-restarted.md)


def test_cron_registered_twice_yields_two_independent_sessions(_agent_row: int) -> None:
    """Calling cron() again with the same expression + timezone no longer
    dedupes, renews, or supersedes anything — it starts a second, independent
    session, and both keep running until killed."""
    first = watcher.cron("0 4 * * *", "wake", timezone="UTC", name="test-cron-dup-1")
    second = watcher.cron("0 4 * * *", "wake", timezone="UTC", name="test-cron-dup-2")
    try:
        assert first != second
        alive = ava.shell.sessions.list()
        assert first in alive
        assert second in alive
    finally:
        ava.shell.sessions.kill(first)
        ava.shell.sessions.kill(second)
