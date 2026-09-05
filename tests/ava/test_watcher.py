"""ava.watcher unit tests — launch writes the agent's script verbatim plus a
generated bootstrap file (identity + watchdog + runpy), and runs them in a
PTY session whose command line tees output to a log file and session capture,
then ends with the CLI completion notice (`ava agents send ... --source
watcher:N`); cron/at build a script then spawn.

The `_pty_sessions_env` fixture (session-scoped, tests/ava/conftest.py) runs the
real supervisor daemon under the tmp test home; the session tests are
POSIX-only (skip on Windows — the PTY supervisor is POSIX-only)."""

import datetime
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

import ava
from ava import _watcher_reconcile, watcher
from ava.shell import _background
from shared.platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); apply it
    # module-wide here since every watcher session test needs the fake-id +
    # pty cleanup isolation. `_pty_sessions_env` first — the isolation fixture's
    # own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("_pty_sessions_env", "_isolated_agent"),
]


def _is_live_watcher(wid: int, name: str = "test-watcher") -> bool:
    return ava.shell.list().get(wid) == name


def _boot_text(wid: int) -> str:
    return (watcher._watchers_dir() / f"watcher_{wid}_boot.py").read_text()


def test_validate_message_rejects_empty() -> None:
    with pytest.raises(ValueError, match="message cannot be empty"):
        watcher.at("2030-01-01T00:00:00Z", "   ", name="test-empty")


@pytest.mark.parametrize(
    "value, expected",
    [
        (90, 90.0),
        (datetime.timedelta(minutes=2), 120.0),
        ("30m", 1800.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("45s", 45.0),
    ],
)
def test_parse_timeout_accepts_forms(
    value: int | datetime.timedelta | str, expected: float
) -> None:
    assert watcher._parse_timeout(value) == expected


@pytest.mark.parametrize("bad", ["", "5x", "later", "-3"])
def test_parse_timeout_rejects_bad_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        watcher._parse_timeout(bad)


def test_parse_timeout_rejects_nonpositive_and_bool() -> None:
    with pytest.raises(ValueError, match="positive"):
        watcher._parse_timeout(0)
    with pytest.raises(TypeError):
        watcher._parse_timeout(True)


def test_watcher_script_dir_is_tmp_per_agent(_agent_row: int) -> None:
    # Generated watcher scripts live under the system temp dir, scoped per
    # cluster + agent — NOT in $AVA_HOME (the old global `watchers/` dir there
    # accumulated 180+ files and let co-agents overwrite each other's scripts)
    # and NOT in the workspace. Session ids are per-agent counters, so a
    # per-agent subdir makes cross-agent collision impossible.
    import tempfile

    from shared.paths import ava_home

    d = watcher._watchers_dir()
    td = pathlib.Path(tempfile.gettempdir())
    assert d.is_relative_to(td / "ava")  # under $TMPDIR/ava/<cluster>/<agent>/
    assert str(ava_home()) not in str(d)  # never under $AVA_HOME
    assert d.name == "watchers"
    # cluster segment: the home basename heads the per-cluster dir
    slug = ava_home().name.lstrip(".") or "cluster"
    assert any(part == slug or part.startswith(f"{slug}-") for part in d.parts)
    assert d.is_dir()


def test_spawn_prunes_stale_watcher_files(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every launch deletes generated watcher files from earlier watchers: a
    # hard-killed watcher cannot self-clean, and its pair would otherwise
    # accumulate forever. Non-generated files are left alone.
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    d = watcher._watchers_dir()
    stale_script = d / "watcher_999.py"
    stale_boot = d / "watcher_999_boot.py"
    stale_script.write_text("old")
    stale_boot.write_text("old boot")
    keep = d / "keep_me.py"
    keep.write_text("not generated")

    wid = watcher.launch("import ava\n", timeout="1h", name="test-prune")

    assert not stale_script.exists()
    assert not stale_boot.exists()
    assert keep.exists()  # non-generated files survive
    assert (d / f"watcher_{wid}.py").exists()  # the new pair is there
    assert (d / f"watcher_{wid}_boot.py").exists()


def test_prune_does_not_touch_other_agents_files(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolation is per-agent: pruning this agent's stale files must never
    # reach into another agent's subdir (the old global dir let agents
    # overwrite each other — that is the bug this layout exists to prevent).
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    d = watcher._watchers_dir()
    other = d.parent / str(ava.self.AGENT_ID + 1) / "watchers"
    other.mkdir(parents=True, exist_ok=True)
    foreign = other / "watcher_999.py"
    foreign.write_text("someone else's")

    watcher.launch("import ava\n", timeout="1h", name="test-isolation")

    assert foreign.exists()  # untouched


def test_launch_creates_watcher_session(_agent_row: int) -> None:
    # A long-running watcher keeps its session alive and listed under its name.
    code = "import time\ntime.sleep(60)\n"
    wid = watcher.launch(code, timeout="1h", name="test-launch")
    try:
        assert isinstance(wid, int)
        assert _is_live_watcher(wid, "test-launch")
    finally:
        ava.shell.kill(wid)


def test_watcher_child_dies_when_pty_host_dies(_agent_row: int, tmp_path: pathlib.Path) -> None:
    """Task #1726 acceptance: a watcher child must NEVER outlive its pty host.

    When the host dies (crash / SIGKILL / a reaper sweep), the login shell
    dies with it and the child is reparented to init — still alive, still
    firing cron/at; 49 of 85 watcher processes on the fleet host were such
    multi-generation orphans (8/19 onwards). The bootstrap's orphan guard
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
        # host — the process whose death orphans the child.
        child = psutil.Process(child_pid)
        shell = psutil.Process(child.ppid())
        host = psutil.Process(shell.ppid())
        assert "shared.pty_sessions.host" in " ".join(host.cmdline())

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
    assert "[watcher] session gone (pty host died)" in boot
    assert "os._exit(125)" in boot


def test_boot_loads_plugins_before_running_script(tmp_path: pathlib.Path) -> None:
    # A fresh child's `import ava` is the factory module without the agent
    # process's plugin setattrs, so the bootstrap must load plugin namespaces
    # (ava.tasks etc.) before it runpys the watcher script — otherwise the script
    # AttributeErrors on any plugin namespace. Order matters: load then run.
    boot = watcher._build_boot(tmp_path / "x.py", None, 42)
    assert "ava._ensure_plugins_loaded()" in boot
    assert boot.index("ava._ensure_plugins_loaded()") < boot.index("runpy.run_path")
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
    # finally's own cleanup — file unlink + registry-row delete — is
    # fail-soft, and that except lives AFTER the finally:).
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
    ava.shell.kill(wid)
    # Poll instead of a fixed sleep: session teardown latency varies under CPU
    # contention (audit round-2 cc-docs-tests P2).
    deadline = time.time() + 5
    while time.time() < deadline and _is_live_watcher(wid, "test-launch"):
        time.sleep(0.05)
    assert not _is_live_watcher(wid, "test-launch")


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


def test_at_announcement_uses_cluster_zone_when_authoritative(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """at() passes the cluster timezone to the generated script when the
    process holds an authoritative one (user ruling 2026-08-27)."""
    from shared.config import settings
    from shared.config.general import GeneralSettings

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    watcher.at(when, "ping later", name="test-at-cluster-tz")
    assert "_TZ = ZoneInfo('Asia/Shanghai')" in captured["code"]
    assert "_WHEN.astimezone(_TZ).isoformat()" in captured["code"]


def test_at_announcement_uses_host_clock_without_authoritative_zone(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings-lite process (no authoritative cluster timezone) passes
    None: the announcement renders in the watcher's own wall clock — the
    documented lite degradation."""
    from shared.config import settings
    from shared.config.general import GeneralSettings

    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct())
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    watcher.at(when, "ping later", name="test-at-lite")
    assert "ZoneInfo" not in captured["code"]
    assert "_WHEN.astimezone().isoformat()" in captured["code"]


def test_cron_defaults_to_host_zone_without_authoritative_zone(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings-lite cron (no authoritative cluster timezone) defaults to the
    host's own zone — not the silent America/Los_Angeles field default."""
    from shared.config import host_tz_name, settings
    from shared.config.general import GeneralSettings

    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct())
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    watcher.cron("0 * * * *", "tick", name="test-cron-lite")
    expected = host_tz_name()
    assert f"_TZ = ZoneInfo('{expected}')" in captured["code"]
    assert "America/Los_Angeles" not in captured["code"]


def test_cron_invalid_expr_raises(_agent_row: int) -> None:
    from shared.watcher import CronExprError

    with pytest.raises(CronExprError):
        watcher.cron("not a cron", "msg", name="test-bad-cron")


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
    assert f"AVA_WATCHER_SESSION_ID={wid} " in captured["cmd"]
    boot = _boot_text(wid)
    # Identity is inlined; ava is imported for init_globals
    assert f'os.environ["AVA_AGENT_ID"] = "{_agent_row}"' in boot
    assert "ava._boot.establish" not in boot
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
    assert "ava._boot.establish" not in boot
    assert "import ava" in boot
    assert "init_globals" in boot
    assert f"watcher_{wid}.py" in boot
    assert f"watcher_{wid}_boot.py" in captured["cmd"]


# -- Completion notice (shell-level, fires on every exit path) -----------------


def test_spawn_line_tees_and_notifies(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # The command line must tee the child's output to the per-agent log file
    # and session capture, then end with the CLI completion notice + session
    # close: the notice is sent from the shell level so a crashed or
    # hard-killed child cannot skip it, and `; exit $_ec` closes the session
    # even when delivery fails — a lingering shell is what reconcile reads as
    # alive (Task #1115 bug B).
    from ava.shell import sessions as _sessions

    captured: dict[str, Any] = {}
    monkeypatch.setattr(_sessions, "send", lambda _id, cmd: captured.update(cmd=cmd))  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout="1h", name="test-notice")

    cmd = captured["cmd"]
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
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)

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
    while time.time() < deadline and wid in ava.shell.list():
        time.sleep(0.3)
    assert wid not in ava.shell.list()


def test_cron_invalid_timezone_raises(_agent_row: int) -> None:
    with pytest.raises(ValueError, match="timezone"):
        watcher.cron("0 3 * * *", "daily", timezone="Not/A/Real/Timezone", name="test-bad-tz")


def test_at_past_time_raises(_agent_row: int) -> None:
    """at() with a past datetime raises ValueError."""
    from datetime import UTC, datetime, timedelta

    past = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="past"):
        watcher.at(past, "too late", name="test-past")

    # timedelta going backwards should also fail
    with pytest.raises(ValueError):
        watcher.at(timedelta(days=-1), "negative delta", name="test-neg-delta")


def test_at_future_time_ok(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """at() with a future time should not raise about the past."""
    from datetime import timedelta

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        watcher,
        "_spawn",
        lambda code, _wd, name, **_kw: captured.update(code=code, name=name) or 7,  # pyright: ignore[reportUnknownArgumentType]
    )

    # Far future
    watcher.at("2099-01-01T00:00:00Z", "far future", name="test-future")
    assert "far future" in captured["code"]

    # timedelta from now
    watcher.at(timedelta(hours=1), "one hour", name="test-delta")
    assert "one hour" in captured["code"]


# -- Agent identity in the child (Task #964 regression) ----------------------


def test_boot_inlines_agent_identity(tmp_path: pathlib.Path) -> None:
    """The bootstrap must set AVA_AGENT_ID itself: the session env allowlist
    (shared/env_registry.py child_env, Task #856) deliberately does
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
    assert env_line < boot.index("ava._ensure_plugins_loaded()")
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
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)

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
    monkeypatch.setattr(_background, "cli_path", lambda: fake_cli)

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


# ─── R1-8: the watcher registry (Task #1021) ─────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry_rows(
    _isolated_agent: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Drop this worker's watcher-registry rows after each test — the table is
    not in conftest's TRUNCATE list (it is a registry, not business data), and
    the reconcile tests leave marked rows behind by design."""
    import psycopg

    from ava.shell import sessions as _sessions
    from shared.config import settings

    # This module seeds many registry rows directly without creating their
    # accompanying PTY record. Keep those unit cases in the legacy no-flip
    # generation; focused tests override this seam to exercise the active
    # generation boundary.
    monkeypatch.setattr(_sessions, "_current_session_generation", lambda: None)

    yield
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM agent_watchers WHERE agent_id = %s",
            (ava.self.AGENT_ID,),
        )


def _registry_rows(agent_id: int) -> list[dict[str, Any]]:
    from shared.watcher_registry import watcher_rows

    return watcher_rows(agent_id=agent_id)


def test_launch_registers_and_kill_unregisters(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch watcher writes a registry row at spawn; killing its session
    drops the row, so the boot reconcile will not resurrect a deliberately
    killed watcher."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.launch("import ava\n", timeout=120, name="test-registry")
    try:
        rows = _registry_rows(_agent_row)
        assert next(r for r in rows if r["session_id"] == wid)["kind"] == "launch"
        assert next(r for r in rows if r["session_id"] == wid)["timeout_secs"] == 120.0
    finally:
        ava.shell.kill(wid)
    assert all(r["session_id"] != wid for r in _registry_rows(_agent_row))


def test_cron_registers_rebuild_payload(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry row carries everything the boot reconcile needs to
    re-spawn a killed cron watcher: expression, timezone, message."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.cron("0 9 * * *", "daily", timezone="America/Los_Angeles", name="test-cron-reg")
    try:
        rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == wid]
        assert rows and rows[0]["kind"] == "cron"
        assert rows[0]["cron_expr"] == "0 9 * * *"
        assert rows[0]["cron_timezone"] == "America/Los_Angeles"
        assert rows[0]["message"] == "daily"
        assert rows[0]["status"] == "running"
    finally:
        ava.shell.kill(wid)


def test_reconcile_rebuilds_missing_cron(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cron row whose session is gone is re-spawned from the stored payload
    (the #1014 fix: a rollout reaped the session; the registry is what knows
    the schedule should exist) and the old row is marked 'rebuilt'."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    from shared.watcher_registry import register_watcher

    register_watcher(
        _agent_row,
        424242,
        kind="cron",
        name="daily",
        message="stand-up",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    actions = watcher.reconcile()
    assert any("rebuilt" in a for a in actions)
    assert calls and calls[0][0][0] == "0 9 * * *"  # positional expr
    assert calls[0][0][1] == "stand-up"  # positional message
    # old row marked rebuilt
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 424242]
    assert rows and rows[0]["status"] == "rebuilt"


def test_reconcile_rebuilds_missing_current_generation_cron(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart must restore declared cron state in the active generation (#2811)."""
    from ava.shell import sessions as _sessions
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "_current_session_generation", lambda: "current-generation")
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    register_watcher(
        _agent_row,
        424247,
        kind="cron",
        name="daily-current",
        message="stand-up",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        generation="current-generation",
    )

    actions = watcher.reconcile()

    assert any("rebuilt" in action for action in actions)
    assert calls and calls[0][0][:2] == ("0 9 * * *", "stand-up")
    rows = [row for row in _registry_rows(_agent_row) if row["session_id"] == 424247]
    assert rows and rows[0]["status"] == "rebuilt"


def test_reconcile_reaps_superseded_generation_without_rebuilding(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old exact session is reaped history, never current desired state."""
    from ava.shell import sessions as _sessions
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", lambda: {424248: "old-cron"})
    monkeypatch.setattr(_sessions, "_current_session_generation", lambda: "current-generation")
    reaped: list[int] = []
    monkeypatch.setattr(
        _sessions,
        "_reap",
        lambda session_id: reaped.append(session_id) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    register_watcher(
        _agent_row,
        424248,
        kind="cron",
        name="daily-old",
        message="stand-up",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        generation="previous-generation",
    )

    actions = watcher.reconcile()

    assert reaped == [424248]
    assert calls == []
    assert any("superseded generation reaped" in action for action in actions)
    rows = [row for row in _registry_rows(_agent_row) if row["session_id"] == 424248]
    assert rows and rows[0]["status"] == "reaped"


@pytest.mark.parametrize("kind", ["at", "launch"])
def test_reconcile_notifies_when_a_superseded_one_shot_is_reaped(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A generation flip may never silently discard a one-shot watcher."""
    from ava.shell import sessions as _sessions
    from shared.watcher_registry import register_watcher

    session_id = 424249 if kind == "at" else 424250
    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", lambda: {session_id: f"old-{kind}"})
    monkeypatch.setattr(_sessions, "_current_session_generation", lambda: "current-generation")
    monkeypatch.setattr(_sessions, "_reap", lambda _session_id: True)  # pyright: ignore[reportUnknownArgumentType]
    sent: list[str] = []
    monkeypatch.setattr("ava.agents.send_message", lambda _aid, content: sent.append(content))  # pyright: ignore[reportUnknownArgumentType]
    if kind == "at":
        register_watcher(
            _agent_row,
            session_id,
            kind=kind,
            name="old-one-shot",
            message="wake",
            fires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
            generation="previous-generation",
        )
    else:
        register_watcher(
            _agent_row,
            session_id,
            kind=kind,
            name="old-one-shot",
            timeout_secs=3600,
            generation="previous-generation",
        )

    actions = watcher.reconcile()

    assert any("superseded generation reaped" in action for action in actions)
    rows = [row for row in _registry_rows(_agent_row) if row["session_id"] == session_id]
    assert rows and rows[0]["status"] == "reaped"
    assert sent and "marked missed" in sent[0]


def test_spawn_binds_registry_generation_to_the_created_session_record(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher row records the generation of its admitted PTY, not today's marker."""
    from ava.shell import sessions as _sessions

    class _Backend:
        def __init__(self) -> None:
            self.names: list[str] = []

        def session_generation(self, name: str) -> str:
            self.names.append(name)
            return "record-generation"

    backend = _Backend()

    def _create_session(
        _name: str | None = None,
        *,
        _cwd: str | None = None,
        _ttl: float | None = None,
    ) -> tuple[int, str]:
        return 424271, "ava-agent-1-shell-424271-record-bound"

    monkeypatch.setattr(
        _sessions,
        "_create_session",
        _create_session,
    )
    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)

    watcher.launch("import ava\n", timeout=120, name="record-bound")

    rows = [row for row in _registry_rows(_agent_row) if row["session_id"] == 424271]
    assert rows and rows[0]["generation"] == "record-generation"
    assert backend.names == ["ava-agent-1-shell-424271-record-bound"]


def test_reconcile_drops_already_fired_one_shot_without_alert(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #1858: a one-shot whose moment passed AND whose wake was already
    delivered (child fired, but its clean-exit row delete failed or raced the
    reconcile) is dropped silently — the wake was not lost, so the "marked
    missed" alert is a false alarm."""
    from ava.shell import sessions as _sessions
    from shared.db import connect
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    sent: list[str] = []
    monkeypatch.setattr("ava.agents.send_message", lambda _aid, content: sent.append(content))  # pyright: ignore[reportUnknownArgumentType]

    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    register_watcher(_agent_row, 424245, kind="at", name="fired", message="go", fires_at=past)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, source, content, kind) "
            "VALUES (%s, %s, %s, 'chat')",
            (_agent_row, "watcher:424245", "go"),
        )
    actions = watcher.reconcile()
    assert not any("missed" in a for a in actions)
    assert any("already fired" in a for a in actions)
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 424245]
    assert rows == []  # stale row dropped, not marked missed
    assert sent == []  # no false alert


def test_reconcile_completion_notice_does_not_count_as_delivered(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA discriminator (PR #826 review): the shell completion notice carries
    the same kind+source tag as the wake, so a child that died BEFORE waking
    (notice present, wake absent) must still be marked missed + alerted."""
    from ava.shell import sessions as _sessions
    from shared.db import connect
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    sent: list[str] = []
    monkeypatch.setattr("ava.agents.send_message", lambda _aid, content: sent.append(content))  # pyright: ignore[reportUnknownArgumentType]

    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    register_watcher(
        _agent_row, 424246, kind="at", name="crashed-before-wake", message="go", fires_at=past
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, source, content, kind) "
            "VALUES (%s, %s, %s, 'chat')",
            (
                _agent_row,
                "watcher:424246",
                "Watcher 'crashed-before-wake' exited with code 1. Full output",
            ),
        )
    actions = watcher.reconcile()
    assert any("missed" in a for a in actions)
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 424246]
    assert rows and rows[0]["status"] == "missed"
    assert sent and "crashed-before-wake" in sent[0]


def test_reconcile_marks_missed_one_shot_and_alerts(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An at-watcher whose moment passed while its session was gone is marked
    'missed' and the agent is told (the wake it was scheduled for is lost)."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    sent: list[str] = []
    monkeypatch.setattr("ava.agents.send_message", lambda _aid, content: sent.append(content))  # pyright: ignore[reportUnknownArgumentType]
    from shared.watcher_registry import register_watcher

    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    register_watcher(_agent_row, 424243, kind="at", name="once", message="go", fires_at=past)
    actions = watcher.reconcile()
    assert any("missed" in a for a in actions)
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 424243]
    assert rows and rows[0]["status"] == "missed"
    assert sent and "once" in sent[0]


def test_reconcile_leaves_alive_watchers_alone(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose session still exists is untouched — the watcher is running."""
    from ava.shell import sessions as _sessions
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", lambda: {555555: "test-alive"})
    register_watcher(_agent_row, 555555, kind="cron", name="alive", cron_expr="* * * * *")
    assert watcher.reconcile() == []
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 555555]
    assert rows and rows[0]["status"] == "running"


def test_reconcile_never_reruns_launch(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch watcher whose session died is marked 'missed' + the agent is
    told — but NEVER re-run at boot: launch scripts may carry side effects
    (send a message, call an external API), and blindly re-running one whose
    death circumstances are unknown could double-fire them (user ruling
    2026-08-08: the registry rebuilds at/cron only)."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    sent: list[str] = []
    monkeypatch.setattr("ava.agents.send_message", lambda _aid, content: sent.append(content))  # pyright: ignore[reportUnknownArgumentType]
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "launch", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    from shared.watcher_registry import register_watcher

    register_watcher(
        _agent_row,
        424244,
        kind="launch",
        name="side-effecty",
        message="",
        timeout_secs=3600,
    )
    actions = watcher.reconcile()

    # marked missed + alerted, never re-launched
    assert calls == []  # launch() was NOT called
    assert any("missed" in a for a in actions)
    rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == 424244]
    assert rows and rows[0]["status"] == "missed"
    assert sent and "side-effecty" in sent[0]


def test_spawn_keeps_sibling_files_while_session_alive(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug A (task #1116): a generated pair whose SESSION still exists must
    not be pruned — launch is asynchronous (the command is sent into a fresh
    session whose shell takes a moment to come up), so a back-to-back sibling
    launch deleting it would make that watcher's python start fail with
    "can't open file ... _boot.py". Only provably-dead pairs (session gone)
    are pruned."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", lambda: {4242: "test-sibling"})
    d = watcher._watchers_dir()
    live_script = d / "watcher_4242.py"
    live_boot = d / "watcher_4242_boot.py"
    live_script.write_text("still needed")
    live_boot.write_text("still needed boot")
    dead_script = d / "watcher_4243.py"
    dead_boot = d / "watcher_4243_boot.py"
    dead_script.write_text("dead")
    dead_boot.write_text("dead boot")

    wid = watcher.launch("import ava\n", timeout="1h", name="test-prune-live")

    # live sibling's pair survives (session 4242 exists); dead pair is pruned
    assert live_script.exists() and live_boot.exists()
    assert not dead_script.exists() and not dead_boot.exists()
    assert (d / f"watcher_{wid}.py").exists()


def test_spawn_back_to_back_keeps_all_files(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug A acceptance (task #1116): creating several watchers back-to-back
    must not delete any sibling's not-yet-read boot file — the exact sequence
    that killed watcher_0/3_boot.py. The fake session registry reports every
    created session live immediately (as the real backend does), so each
    launch's prune must leave its siblings' pairs on disk."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    alive: set[int] = set()
    counter = iter(range(1000, 1003))

    def _fake_create(name: str) -> tuple[int, str]:
        sid = next(counter)
        alive.add(sid)
        return sid, name

    monkeypatch.setattr(_sessions, "_create_session", _fake_create)
    monkeypatch.setattr(_sessions, "list", lambda: dict.fromkeys(alive, "w"))
    d = watcher._watchers_dir()

    ids: list[int] = []
    for name in ("b2b-1", "b2b-2", "b2b-3"):
        wid = watcher.launch("import ava\n", timeout="1h", name=name)
        ids.append(wid)

    # all three pairs on disk — none pruned while its session is live
    for wid in ids:
        assert (d / f"watcher_{wid}.py").exists(), f"watcher_{wid}.py pruned"
        assert (d / f"watcher_{wid}_boot.py").exists(), f"watcher_{wid}_boot.py pruned"
    assert len(ids) == 3 and len(set(ids)) == 3


def test_reconcile_skips_rebuilt_rows(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'rebuilt' row is terminal history — the rebuild already happened and
    its new running row owns the watcher now. Re-processing it would re-spawn
    a duplicate on every boot (observed 2026-08-09: each cron came back twice
    after one rollout). 'missed' rows are terminal too (already alerted)."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", dict)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    from shared.watcher_registry import mark_status, register_watcher

    # a rebuilt row (session 424250 was rebuilt into a new session long ago)
    register_watcher(
        _agent_row,
        424250,
        kind="cron",
        name="dup-guard",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    mark_status(_agent_row, 424250, "rebuilt")
    # a missed row (one-shot already marked + alerted)
    register_watcher(
        _agent_row,
        424251,
        kind="at",
        name="gone-once",
        message="x",
        fires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1),
    )
    mark_status(_agent_row, 424251, "missed")
    # a running row with a dead session — must still rebuild
    register_watcher(
        _agent_row,
        424252,
        kind="cron",
        name="real-dead",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )

    actions = watcher.reconcile()

    assert calls and len(calls) == 1  # only the running row rebuilt
    assert any("real-dead" in a for a in actions)
    assert not any("dup-guard" in a or "gone-once" in a for a in actions)


def test_reconcile_kills_orphan_process_before_rebuild(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #1726: a watcher whose SESSION is gone but whose process is still
    alive is an orphan — its pty host died and the child was reparented to
    init, still firing cron/at. The reconcile must SIGKILL it when it rebuilds
    the row: a rebuild that leaves the orphan alive stacks a NEW generation on
    top of it and both fire (the multi-generation duplicate class). The
    generated boot script path is unique per (agent, session), so the argv
    match is precise."""
    import subprocess

    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    from shared.watcher_registry import register_watcher

    register_watcher(
        _agent_row,
        424260,
        kind="cron",
        name="orphan-cron",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    # A fake orphan: a live process whose argv names this watcher's generated
    # boot script — the exact fingerprint of a reparented watcher child.
    boot = watcher._watchers_dir() / "watcher_424260_boot.py"
    boot.write_text("import time\ntime.sleep(600)\n")
    orphan = subprocess.Popen([sys.executable, str(boot)])  # noqa: S603 — repo-internal fake watcher
    from contextlib import suppress

    try:
        assert orphan.poll() is None  # still running
        actions = watcher.reconcile()
        # poll() reaps the SIGKILLed child (a killed-but-unreaped child would
        # otherwise linger as a zombie and still answer pid_exists).
        deadline = time.time() + 10
        while time.time() < deadline and orphan.poll() is None:
            time.sleep(0.05)
        assert orphan.poll() is not None, "reconcile left the orphan alive"
        assert calls, "the orphan's row was not rebuilt"
        assert any("rebuilt as session 999" in a for a in actions)
    finally:
        with suppress(ProcessLookupError):
            orphan.kill()  # a failed assertion must not leak the fake orphan
        boot.unlink(missing_ok=True)


def test_reconcile_kill_skips_alive_watcher_process(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan-kill must never touch a process that does not match this
    watcher's exact boot script path — a sibling session id or another agent's
    same-numbered watcher is a live, healthy process."""
    import subprocess

    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "list", set)
    from shared.watcher_registry import register_watcher

    register_watcher(
        _agent_row,
        424261,
        kind="launch",
        name="orphan-kill-sibling",
        timeout_secs=3600,
    )
    # A live process that looks like a DIFFERENT watcher (session 424262) and
    # one that merely shares the agent's watchers dir — neither matches.
    other_boot = watcher._watchers_dir() / "watcher_424262_boot.py"
    other_boot.write_text("import time\ntime.sleep(600)\n")
    sibling = subprocess.Popen([sys.executable, str(other_boot)])  # noqa: S603 — repo-internal fake watcher
    from contextlib import suppress

    try:
        watcher.reconcile()
        assert sibling.poll() is None, "sibling watcher process was killed"
    finally:
        with suppress(ProcessLookupError):
            sibling.kill()
        other_boot.unlink(missing_ok=True)


# -- boot reconcile: stale-template rebuild (issue #1330) ----------------------


def _cron_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "session_id": 68,
        "agent_id": 2811,
        "kind": "cron",
        "name": "daily-signal-scan",
        "message": "scan now",
        "fires_at": None,
        "cron_expr": "30 21 * * *",
        "cron_timezone": "Asia/Shanghai",
        "cron_end_at": None,
        "timeout_secs": None,
        "status": "running",
        "template_version": 1,
    }
    row.update(over)
    return row


def test_reconcile_rebuilds_live_cron_watcher_with_stale_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cron watcher whose session is alive but whose spawn template version is
    behind the current one must be rebuilt: the generated script is frozen at
    launch, so a template fix (issue #182) never reaches a running session —
    it would keep double-firing at the boundary (issue #1330)."""
    from shared import watcher_registry

    monkeypatch.setattr(
        watcher_registry,
        "watcher_rows",
        lambda _agent_id: [_cron_row()],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(watcher_registry, "mark_status", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(watcher_registry, "delete_watcher", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ava.shell.sessions, "list", lambda: {68: "watcher:68"})
    killed: list[int] = []
    monkeypatch.setattr(ava.shell.sessions, "kill", killed.append)
    spawned: dict[str, object] = {}

    def fake_cron(
        expr: str,
        message: str,
        *,
        timezone: str,
        end_time: object,
        name: str,
        _exclude_session: int | None = None,
    ) -> int:
        spawned.update(
            expr=expr,
            message=message,
            timezone=timezone,
            end_time=end_time,
            name=name,
            exclude=_exclude_session,
        )
        return 999

    monkeypatch.setattr(_watcher_reconcile, "cron", fake_cron)

    actions = watcher.reconcile()

    assert spawned == {
        "expr": "30 21 * * *",
        "message": "scan now",
        "timezone": "Asia/Shanghai",
        "end_time": None,
        "name": "daily-signal-scan",
        # The stale session being replaced is live — the dedupe must skip it,
        # or the rebuild would "reuse" the very session it then kills and
        # leave the schedule with no live copy (Task #1825).
        "exclude": 68,
    }
    assert killed == [68]
    assert any("rebuilt as session 999" in a and "stale template v1" in a for a in actions)


def test_reconcile_leaves_current_template_watcher_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live cron watcher spawned with the current template version must not
    be rebuilt on every boot.

    The row must reference the live TEMPLATE_VERSION constant — a hardcoded
    version goes stale on the next template bump, the stale-rebuild branch
    starts firing, and a fake cron whose signature does not match the real
    call raises inside _rebuild_stale_cron_watcher's bare except, swallowing
    the failure and passing the assertions vacuously (2026-08-26 adversarial
    review of the v2 -> v3 bump)."""
    from shared import watcher_registry
    from shared.watcher import TEMPLATE_VERSION

    monkeypatch.setattr(
        watcher_registry,
        "watcher_rows",
        lambda _agent_id: [_cron_row(template_version=TEMPLATE_VERSION)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(ava.shell.sessions, "list", lambda: {68: "watcher:68"})
    killed: list[int] = []
    monkeypatch.setattr(ava.shell.sessions, "kill", killed.append)
    spawned: dict[str, object] = {}

    def fake_cron(
        expr: str,
        message: str,
        *,
        timezone: str,
        end_time: object,
        name: str,
        _exclude_session: int | None = None,
    ) -> int:
        spawned.update(
            expr=expr,
            message=message,
            timezone=timezone,
            end_time=end_time,
            name=name,
            exclude=_exclude_session,
        )
        return 999

    monkeypatch.setattr(_watcher_reconcile, "cron", fake_cron)

    actions = watcher.reconcile()

    assert actions == []
    assert killed == []
    assert spawned == {}


def test_reconcile_missing_session_still_rebuilds_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-existing missing-session rebuild (issue #1014) is unaffected by
    the version column: a dead cron watcher is rebuilt regardless of its spawn
    version."""
    from shared import watcher_registry
    from shared.watcher import TEMPLATE_VERSION

    monkeypatch.setattr(
        watcher_registry,
        "watcher_rows",
        lambda _agent_id: [_cron_row(template_version=TEMPLATE_VERSION)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(ava.shell.sessions, "list", set)
    statuses: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        watcher_registry,
        "mark_status",
        lambda a, s, st: statuses.append((a, s, st)),  # pyright: ignore[reportUnknownArgumentType]
    )
    spawned: list[object] = []
    monkeypatch.setattr(
        _watcher_reconcile,
        "cron",
        lambda *a, **k: spawned.append((a, k)) or 999,  # pyright: ignore[reportUnknownArgumentType]
    )

    actions = watcher.reconcile()

    from tests.ava.conftest import _TEST_AGENT_BASE

    assert len(spawned) == 1
    assert statuses == [(_TEST_AGENT_BASE, 68, "rebuilt")]
    assert any("rebuilt as session 999" in a for a in actions)


# ─── register-before-start (tech audit 2026-08-24 P1) ────────────────────────


def test_spawn_registers_before_starting(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry row exists when the child starts — register-before-start
    closes the ghost-row race: a child that exits before its row lands leaves
    the row inserted after the child's clean-exit cleanup already ran, i.e. a
    permanent 'running' row the boot reconcile reads as killed-should-exist."""
    from ava.shell import sessions as _sessions

    registered_at_start: list[bool] = []

    def recording_send(_sid: int, _cmd: str) -> None:
        # _spawn calls send AFTER registering — the row must already exist.
        registered_at_start.append(any(r["session_id"] == _sid for r in _registry_rows(_agent_row)))

    monkeypatch.setattr(_sessions, "send", recording_send)
    wid = watcher.launch("import ava\n", timeout=120, name="test-reg-order")
    try:
        assert registered_at_start and registered_at_start[0] is True
        assert any(r["session_id"] == wid for r in _registry_rows(_agent_row))
    finally:
        ava.shell.kill(wid)


def test_spawn_fails_when_registry_write_fails(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry write failure FAILS the spawn instead of fail-soft: the
    watcher must not start if the boot reconcile could never rebuild it, and
    the created session is disposed."""
    from ava.shell import sessions as _sessions

    started: list[str] = []
    killed: list[int] = []
    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: started.append(_cmd))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sessions, "kill", killed.append)

    def _fail_registration(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "shared.watcher_registry.register_watcher",
        _fail_registration,
    )
    with pytest.raises(RuntimeError, match="db down"):
        watcher.launch("import ava\n", timeout=120, name="test-reg-fail")
    assert started == []  # the child never started
    assert killed  # the created session was disposed
    assert _registry_rows(_agent_row) == []  # no row leaked


def test_spawn_compensates_row_when_start_fails(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the child cannot be started after registration, the registry row is
    dropped — a row whose session never ran would be read by the next boot
    reconcile as killed-should-exist and rebuilt forever."""
    from ava.shell import sessions as _sessions

    before = len(_registry_rows(_agent_row))
    monkeypatch.setattr(
        _sessions,
        "send",
        lambda _id, _cmd: (_ for _ in ()).throw(RuntimeError("pty down")),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(RuntimeError, match="pty down"):
        watcher.launch("import ava\n", timeout=120, name="test-reg-comp")
    assert len(_registry_rows(_agent_row)) == before  # no row leaked


# ─── kill → registry cleanup + cron registration dedupe (Task #1825) ────────


def test_cron_kill_unregisters(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Killing a cron watcher's session drops its registry row — a deliberately
    killed cron must not be resurrected by the next boot reconcile. Task
    #1825: a kill path that left the row behind made a killed cron come back
    as a second live instance (the double-instance class)."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.cron("0 9 * * *", "daily", timezone="UTC", name="test-kill-cron")
    try:
        assert any(r["session_id"] == wid for r in _registry_rows(_agent_row))
    finally:
        ava.shell.kill(wid)
    assert all(r["session_id"] != wid for r in _registry_rows(_agent_row))


def test_kill_all_unregisters_watchers(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """kill_all (the prefix-scoped session sweep) drops every watcher registry
    row along with the sessions — same deliberate-kill semantics as kill():
    nothing the sweep killed may be resurrected at the next boot."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid = watcher.cron("0 9 * * *", "daily", timezone="UTC", name="test-killall")
    assert any(r["session_id"] == wid for r in _registry_rows(_agent_row))
    _sessions.kill_all()
    assert all(r["session_id"] != wid for r in _registry_rows(_agent_row))


def test_cron_double_registration_reuses_live_session(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registering a cron schedule that is already live under another session
    reuses it instead of stacking a second generation — the Task #1825
    double-instance fix at the registration gate (a second call, a rebuild,
    or a restart race all flow through cron())."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    wid1 = watcher.cron("0 9 * * *", "stand-up", timezone="UTC", name="dup-a")
    try:
        wid2 = watcher.cron("0 9 * * *", "stand-up", timezone="UTC", name="dup-b")
        assert wid2 == wid1  # reused, not a new session
        rows = [r for r in _registry_rows(_agent_row) if r["session_id"] == wid1]
        assert len(rows) == 1  # exactly one registration, not two
        assert rows[0]["status"] == "running"
    finally:
        ava.shell.kill(wid1)


def test_cron_same_expr_different_timezone_registers_separately(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedupe key is the full schedule — (expr, timezone, end time). The
    same expression in a different timezone fires at different instants and
    is a different watcher."""
    from ava.shell import sessions as _sessions

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    w1 = watcher.cron("0 9 * * *", "utc-nine", timezone="UTC", name="tz-a")
    try:
        w2 = watcher.cron("0 9 * * *", "shanghai-nine", timezone="Asia/Shanghai", name="tz-b")
        try:
            assert w2 != w1
            rows = [r for r in _registry_rows(_agent_row) if r["session_id"] in (w1, w2)]
            assert len(rows) == 2
        finally:
            ava.shell.kill(w2)
    finally:
        ava.shell.kill(w1)


def test_reconcile_rebuild_dedupes_against_live_duplicate(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CEO #228 shape: a cron whose row says 'running' but whose session is
    gone, while a SECOND live session with the same schedule survived. The
    reconcile must reuse the live one — not spawn a third — and drop the dead
    duplicate row."""
    from ava.shell import sessions as _sessions
    from shared.watcher import TEMPLATE_VERSION
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(_watcher_reconcile, "cron", lambda *a, **k: calls.append((a, k)) or 999)  # pyright: ignore[reportUnknownArgumentType]
    # Both rows carry the CURRENT template version: a NULL/0 version would
    # route the live row into the stale-template rebuild (a different branch)
    # instead of the missing-session dedupe under test.
    register_watcher(
        _agent_row,
        52,
        kind="cron",
        name="esc-check",
        message="x",
        cron_expr="0 */6 * * *",
        cron_timezone="UTC",
        template_version=TEMPLATE_VERSION,
    )
    register_watcher(
        _agent_row,
        53,
        kind="cron",
        name="esc-check",
        message="x",
        cron_expr="0 */6 * * *",
        cron_timezone="UTC",
        template_version=TEMPLATE_VERSION,
    )
    monkeypatch.setattr(_sessions, "list", lambda: {53: "watcher:53"})

    actions = watcher.reconcile()

    assert calls == []  # no new spawn — the live duplicate is reused
    rows = {r["session_id"]: r for r in _registry_rows(_agent_row)}
    assert 52 not in rows  # dead duplicate dropped
    assert rows[53]["status"] == "running"
    assert any("duplicate of live session 53" in a for a in actions)


def test_reconcile_collapses_two_dead_rows_into_one_rebuild(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #2811 shape: two 'running' rows with dead sessions for the SAME
    schedule (duplicate registrations that outlived their sessions). The
    reconcile must converge on ONE live watcher: the first dead row rebuilds,
    and the second dead row's rebuild is deduped onto the fresh session by
    cron()'s registration gate (which re-checks liveness against the live
    backend, unlike the reconcile's once-snapshotted session list) — never a
    second generation."""
    from ava.shell import sessions as _sessions
    from shared.watcher import TEMPLATE_VERSION
    from shared.watcher_registry import register_watcher

    monkeypatch.setattr(_sessions, "send", lambda _id, _cmd: None)  # pyright: ignore[reportUnknownArgumentType]
    register_watcher(
        _agent_row,
        52,
        kind="cron",
        name="esc-check",
        message="x",
        cron_expr="0 */6 * * *",
        cron_timezone="UTC",
        template_version=TEMPLATE_VERSION,
    )
    register_watcher(
        _agent_row,
        53,
        kind="cron",
        name="esc-check",
        message="x",
        cron_expr="0 */6 * * *",
        cron_timezone="UTC",
        template_version=TEMPLATE_VERSION,
    )

    actions = watcher.reconcile()

    rows = {r["session_id"]: r for r in _registry_rows(_agent_row)}
    running = [r for r in rows.values() if r["status"] == "running"]
    assert len(running) == 1  # exactly ONE live watcher for the schedule
    live_id = running[0]["session_id"]
    assert live_id not in (52, 53)  # the fresh session, spawned by real _spawn
    assert rows[52]["status"] == "rebuilt"
    assert rows[53]["status"] == "rebuilt"
    assert any(f"rebuilt as session {live_id}" in a for a in actions)


# ─── atomic registration (Task #1825 N2) ────────────────────────────────────


def _run_cron_racers(
    agent_id: int,
    *,
    tmp_path: pathlib.Path,
    db_url: str | None = None,
    timeout: float = 90.0,
) -> list[int]:
    """Spawn two independent processes that both call
    `ava.watcher.cron("0 9 * * *", ...)` at a shared GO signal; return the
    session ids they printed. Each process is a fresh interpreter with its
    own DB connections — the exact topology of two concurrent registrations
    (a restart-overlap pair both running the boot reconcile)."""
    go = tmp_path / "race-go"
    ready_files = [tmp_path / f"race-ready-{i}" for i in range(2)]
    script = (
        "import os, time, sys\n"
        "import ava\n"
        "open(os.environ['RACE_READY'], 'w').close()\n"
        "while not os.path.exists(os.environ['RACE_GO']):\n"
        "    time.sleep(0.005)\n"
        "print(ava.watcher.cron('0 9 * * *', 'race', timezone='UTC', name='race'), flush=True)\n"
    )
    procs: list[subprocess.Popen[str]] = []
    for ready in ready_files:
        env = os.environ.copy()
        env["AVA_AGENT_ID"] = str(agent_id)
        env["AVA_PROCESS_PROFILE"] = "agent"
        env["RACE_READY"] = str(ready)
        env["RACE_GO"] = str(go)
        if db_url is not None:
            env["AVA_DB_URL"] = db_url
        procs.append(
            subprocess.Popen(  # noqa: S603 — test-harness interpreter
                [sys.executable, "-c", script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    try:
        # both racers at the gate before firing — otherwise the race is vacuous
        deadline = time.time() + 30
        while time.time() < deadline and not all(f.exists() for f in ready_files):
            time.sleep(0.01)
        go.touch()
        outs: list[int] = []
        for p in procs:
            out, err = p.communicate(timeout=timeout)
            if p.returncode != 0:
                raise AssertionError(f"racer failed (rc={p.returncode}): {err[-2000:]}")
            outs.append(int(out.strip()))
        return outs
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


def test_cron_concurrent_registration_yields_one_winner(
    _agent_row: int, tmp_path: pathlib.Path
) -> None:
    """The N2 property, end to end: two concurrent registrations of the same
    cron schedule — separate processes, separate DB connections — must yield
    exactly ONE live watcher. The loser's registration serializes on the
    transaction-scoped advisory lock and reuses the winner's session instead
    of stacking a second generation. (Direct-connection environment; the
    pooled-URL variant runs in
    test_cron_concurrent_registration_through_pooler when a PgBouncer binary
    is available.)"""
    from shared.watcher_registry import watcher_rows

    ids = _run_cron_racers(_agent_row, tmp_path=tmp_path)

    assert len(ids) == 2
    assert ids[0] == ids[1], f"two winners registered: {ids}"
    rows = [r for r in watcher_rows(agent_id=_agent_row) if r["kind"] == "cron"]
    running = [r for r in rows if r["status"] == "running"]
    assert len(running) == 1, f"expected exactly one running cron row, got {rows}"
    assert running[0]["session_id"] == ids[0]


def test_cron_concurrent_registration_through_pooler(
    _agent_row: int, tmp_path: pathlib.Path, _provisioned_db: str
) -> None:
    """Production-equivalent lock semantics (QA #794 BLOCK): the registration
    race through a REAL PgBouncer in `pool_mode = transaction` with
    `server_reset_query = DISCARD ALL` — the exact config that silently
    dropped the session-level advisory lock of the previous revision. The
    transaction-scoped lock must survive the pooler: exactly one winner.
    Skipped where no pgbouncer binary exists (CI Linux has none)."""
    import shutil

    bin_path = shutil.which("pgbouncer")
    if bin_path is None:
        pytest.skip("pgbouncer binary not available on this host")
    import socket
    from contextlib import suppress

    import psycopg

    from shared.watcher_registry import watcher_rows

    # the provisioned test PG (trust auth) + a free port for the pooler
    pg_port = int(_provisioned_db.rsplit(":", 1)[1].split("/", 1)[0])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        pooler_port = s.getsockname()[1]

    # PgBouncer's trust mode still requires the user in an auth_file (it
    # trusts the password, not the username); the password field is ignored.
    auth_file = tmp_path / "pgbouncer-users.txt"
    auth_file.write_text('"ava_citest" ""\n"ava" ""\n')
    ini = tmp_path / "pgbouncer.ini"
    ini.write_text(
        f"""[databases]
ava_citest = host=127.0.0.1 port={pg_port} dbname=ava_citest

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {pooler_port}
auth_type = trust
auth_file = {auth_file}
pool_mode = transaction
server_reset_query = DISCARD ALL
ignore_startup_parameters = extra_float_digits,options
max_client_conn = 100
default_pool_size = 4
pidfile = {tmp_path}/pgbouncer.pid
logfile = {tmp_path}/pgbouncer.log
"""
    )
    proc = subprocess.Popen(  # noqa: S603 — throwaway pooler for the test
        [bin_path, str(ini)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        # wait for the pooler's listener
        deadline = time.time() + 20
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", pooler_port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("pgbouncer did not become ready: " + _pooler_tail(ini))
        # sanity: the pooled URL really reaches the test DB through the pooler
        pooled_url = f"postgresql://ava_citest@127.0.0.1:{pooler_port}/ava_citest"
        with psycopg.connect(pooled_url, autocommit=True) as conn:
            row = conn.execute("SELECT 1").fetchone()
            assert row is not None and row[0] == 1

        race_tmp = tmp_path / "pooled-race"
        race_tmp.mkdir()
        ids = _run_cron_racers(_agent_row, tmp_path=race_tmp, db_url=pooled_url)

        assert len(ids) == 2
        assert ids[0] == ids[1], f"two winners registered through the pooler: {ids}"
        rows = [r for r in watcher_rows(agent_id=_agent_row) if r["kind"] == "cron"]
        running = [r for r in rows if r["status"] == "running"]
        assert len(running) == 1, f"expected one running cron row, got {rows}"
        assert running[0]["session_id"] == ids[0]
    finally:
        proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()


def _pooler_tail(ini_path: pathlib.Path) -> str:
    """Tail the throwaway pooler's log for failure diagnostics."""

    log_path = ini_path.parent / "pgbouncer.log"
    if log_path.exists():
        return (log_path.read_text() or "")[-1500:]
    return "(no pooler log found)"


def test_cron_advisory_key_is_stable_and_schedule_scoped() -> None:
    """The advisory-lock key is a pure function of (agent, expr, timezone,
    end time): identical schedules key identically across calls; a different
    agent, timezone, or end time keys differently (so they serialize
    independently)."""
    from shared.watcher_registry import cron_advisory_key

    end = datetime.datetime(2026, 12, 31, 16, 0, tzinfo=datetime.UTC)
    assert cron_advisory_key(1, "0 9 * * *", "UTC", None) == cron_advisory_key(
        1, "0 9 * * *", "UTC", None
    )
    assert cron_advisory_key(1, "0 9 * * *", "UTC", None) != cron_advisory_key(
        2, "0 9 * * *", "UTC", None
    )
    assert cron_advisory_key(1, "0 9 * * *", "UTC", None) != cron_advisory_key(
        1, "0 9 * * *", "Asia/Shanghai", None
    )
    assert cron_advisory_key(1, "0 9 * * *", "UTC", None) != cron_advisory_key(
        1, "0 9 * * *", "UTC", end
    )
