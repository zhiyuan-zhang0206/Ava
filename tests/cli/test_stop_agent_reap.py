"""`ava stop` reaps this cluster's agent processes + their shell sessions.

Agent processes are spawned via the gateway (`POST /api/agents`), not as
ServiceSpecs, so the service stop never saw them and they outlived `ava stop` on
a stale env (the rollout-respawn survivors that forced a manual `.env` sed during
a data-plane cutover). Agent processes are detached native sessions the
native supervisor tracks; `_reap_agent_sessions` SIGTERMs each (shared deadline),
then force-kills stragglers, and force-kills the agents' lingering shell sessions.
`cmd_stop` opts in via reap_agents, while `cmd_update` / `cmd_restart` leave it
off (their agent lifecycle is the rollout's, not a teardown).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import stop as _stop
from cli.commands.stop import _reap_agent_sessions
from shared.cluster import session_name

_PREFIX = session_name("agent-")  # ava-<cluster>-agent-


class _FakeNative:
    """In-memory native supervisor for the agent PROCESS sessions. A session in
    `graceful_exits` dies on `graceful_signal` (the agent ran its finally on
    SIGTERM and exited); the rest survive until `kill_session` force-kills them."""

    def __init__(self, sessions: set[str], *, graceful_exits: set[str] | None = None) -> None:
        self.sessions = set(sessions)
        self.graceful_exits = set(graceful_exits or ())
        self.signalled: list[str] = []
        self.killed: list[tuple[str, bool]] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(s for s in self.sessions if s.startswith(prefix))

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def graceful_signal(self, name: str) -> bool:
        self.signalled.append(name)
        present = name in self.sessions
        if name in self.graceful_exits:
            self.sessions.discard(name)
        return present

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple:
        self.killed.append((name, graceful))
        existed = name in self.sessions
        self.sessions.discard(name)
        return True, ("graceful" if graceful else "forced") if existed else "noop"


class _FakeShellBackend:
    """In-memory shell backend for the agents' persistent SHELL sessions."""

    def __init__(self, sessions: set[str]) -> None:
        self.sessions = set(sessions)
        self.killed: list[tuple[str, bool]] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(s for s in self.sessions if s.startswith(prefix))

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple:
        self.killed.append((name, graceful))
        self.sessions.discard(name)
        return True, "forced"


def _use_backends(
    monkeypatch: pytest.MonkeyPatch, native: _FakeNative, shells: _FakeShellBackend
) -> None:
    import shared.session_backend as _sb

    monkeypatch.setattr(_sb, "native_proc", lambda: native)
    # The agent shells / watchers are reached via get_shell_backend() — service
    # sessions (get_backend) live on the native backend.
    monkeypatch.setattr(_sb, "get_shell_backend", lambda: shells)


def test_reap_graceful_exit_then_force_kills_child_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent-5 + agent-9 exit cleanly on SIGTERM; agent-5's persistent shell lingers.
    a5, a9, shell = f"{_PREFIX}5", f"{_PREFIX}9", f"{_PREFIX}5-shell-1"
    native = _FakeNative({a5, a9}, graceful_exits={a5, a9})
    shells = _FakeShellBackend({shell})
    _use_backends(monkeypatch, native, shells)

    results = _reap_agent_sessions(timeout_s=0.0)

    # both process sessions were SIGTERM'd, and (having exited) reported graceful
    assert set(native.signalled) == {a5, a9}
    modes = dict(results)
    assert modes[a5] == "graceful"
    assert modes[a9] == "graceful"
    # the lingering shell child is force-killed via the shell backend as a straggler
    assert modes[shell] == "child"
    assert (shell, False) in shells.killed
    assert native.sessions == set() and shells.sessions == set()  # nothing survives


def test_reap_stuck_agent_is_force_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent-5 ignores SIGTERM → survives the (zero) wait → force-killed.
    a5 = f"{_PREFIX}5"
    native = _FakeNative({a5}, graceful_exits=set())
    _use_backends(monkeypatch, native, _FakeShellBackend(set()))

    results = _reap_agent_sessions(timeout_s=0.0)

    assert native.signalled == [a5]  # graceful attempt made first
    assert dict(results)[a5] == "forced"
    assert (a5, False) in native.killed


def test_reap_includes_legacy_cluster_named_agent_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transitional (path-only cutover): agent processes started by pre-cutover
    code are recorded as `ava-<cluster>-agent-<id>` in THIS home's native session
    registry — `ava stop` must reap them too (they were exactly the agents that
    once outlived a stop on a stale env), while never touching a daemon session
    like agent-runner-watchdog (no digit tail)."""
    legacy, current = "ava-main-agent-7", f"{_PREFIX}5"
    daemon = "ava-agent-runner-watchdog"  # NOT an agent process; must survive
    native = _FakeNative({legacy, current, daemon}, graceful_exits={legacy, current})
    _use_backends(monkeypatch, native, _FakeShellBackend(set()))

    results = _reap_agent_sessions(timeout_s=0.0)

    modes = dict(results)
    assert modes[legacy] == "graceful"
    assert modes[current] == "graceful"
    assert daemon not in modes
    assert daemon in native.sessions  # untouched


def test_reap_no_agents_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    native = _FakeNative(set())
    shells = _FakeShellBackend(set())
    _use_backends(monkeypatch, native, shells)
    assert _reap_agent_sessions(timeout_s=0.0) == []
    assert native.killed == [] and shells.killed == []


def test_do_stop_reaps_only_when_opted_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import cli.commands as _cli

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_kill_session", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._cluster_instance.stop_cluster_instance", lambda: None)

    reaped: list[bool] = []
    monkeypatch.setattr(_cli, "_reap_agent_sessions", lambda **_k: reaped.append(True) or [])  # pyright: ignore[reportUnknownArgumentType]

    # default (cmd_update / cmd_restart path): agents are NOT reaped
    assert _cli._do_stop(tmp_path, graceful=False, require_confirmation=False) == 0
    assert reaped == []

    # opt-in (cmd_stop path): agents ARE reaped
    assert (
        _cli._do_stop(tmp_path, graceful=False, require_confirmation=False, reap_agents=True) == 0
    )
    assert reaped == [True]


def test_cmd_stop_opts_into_agent_reap(monkeypatch, tmp_path: Path) -> None:

    monkeypatch.setattr(_stop, "_repo_root", lambda: tmp_path)  # pyright: ignore[reportUnknownMemberType]

    captured: dict[str, object] = {}
    monkeypatch.setattr(_stop, "_do_stop", lambda _repo, **kw: captured.update(kw) or 0)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    assert _stop.cmd_stop(require_confirmation=False) == 0
    assert captured["reap_agents"] is True


def test_reap_names_watcher_sessions_from_the_registry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R1-8: the reaper consults the watcher registry to name the watcher
    sessions it kills — a stopped host's watchers are exactly what the next
    boot reconcile rebuilds, so the stop output says which sessions that will
    be. A plain shell session (no registry row) gets no annotation."""
    a5, watcher_sess, shell_sess = (
        f"{_PREFIX}5",
        f"{_PREFIX}5-shell-77-ci-monitor",
        f"{_PREFIX}5-shell-78",
    )
    native = _FakeNative({a5}, graceful_exits={a5})
    shells = _FakeShellBackend({watcher_sess, shell_sess})
    _use_backends(monkeypatch, native, shells)
    monkeypatch.setattr(
        "shared.watcher_registry.watcher_rows",
        lambda: [{"agent_id": 5, "session_id": 77, "name": "ci-monitor"}],
    )

    results = _reap_agent_sessions(timeout_s=0.0)

    assert dict(results)[watcher_sess] == "child"
    assert dict(results)[shell_sess] == "child"
    out = capsys.readouterr().out
    assert "watcher 'ci-monitor'" in out
    assert "77" in out
    assert "rebuilt from the registry" in out


def test_reap_registry_read_failure_still_reaps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registry read is fail-soft by contract — the reaper runs while the
    DB may already be down, and a registry blip must not block the stop."""
    a5 = f"{_PREFIX}5"
    native = _FakeNative({a5}, graceful_exits={a5})
    _use_backends(monkeypatch, native, _FakeShellBackend(set()))
    monkeypatch.setattr(
        "shared.watcher_registry.watcher_rows",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    results = _reap_agent_sessions(timeout_s=0.0)

    assert dict(results)[a5] == "graceful"


def test_reap_kill_shells_false_keeps_shells(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The update / restart force-reap path (kill_shells=False) kills the agent
    PROCESSES but deliberately leaves the persistent shell / watcher
    sessions running — they outlive their processes by contract
    (agent/lifecycle.py), and an update only wants the processes gone so the
    restarter respawns them on new code. Killing them too is what silently
    killed every watcher on rollouts with a quiesce straggler (#1014; user
    ruling 2026-08-08). No rebuild annotation is printed — nothing was reaped
    that needs rebuilding."""
    a5, a9 = f"{_PREFIX}5", f"{_PREFIX}9"
    shell, watcher_sess = f"{_PREFIX}5-shell-1", f"{_PREFIX}5-shell-77-ci-monitor"
    native = _FakeNative({a5, a9}, graceful_exits=set())  # both stragglers
    shells = _FakeShellBackend({shell, watcher_sess})
    _use_backends(monkeypatch, native, shells)
    monkeypatch.setattr(
        "shared.watcher_registry.watcher_rows",
        lambda: [{"agent_id": 5, "session_id": 77, "name": "ci-monitor"}],
    )

    results = _reap_agent_sessions(timeout_s=0.0, kill_shells=False)

    # agent processes are force-killed (stragglers)
    modes = dict(results)
    assert modes[a5] == "forced" and modes[a9] == "forced"
    # shell / watcher sessions survive untouched
    assert shells.sessions == {shell, watcher_sess}
    assert shells.killed == []
    # no watcher-rebuild annotation: nothing that needs rebuilding was killed
    out = capsys.readouterr().out
    assert "watcher" not in out and "rebuilt" not in out


def test_reap_kill_shells_false_is_noop_without_agent_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill_shells=False with only shell sessions present reaps nothing: shells
    are not this path's business, so the early return must not treat them as
    something to reap."""
    shell = f"{_PREFIX}5-shell-1"
    _use_backends(monkeypatch, _FakeNative(set()), _FakeShellBackend({shell}))

    results = _reap_agent_sessions(timeout_s=0.0, kill_shells=False)

    assert results == []


def test_full_stop_reaps_non_agent_pty_sessions_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill_shells sweeps EVERY shell-backend session, not just the agent
    prefix: gateway-owned schedule sessions (`ava-schedule-<id>`) live in the
    same pty namespace, and per-session hosts detach from every process tree —
    a full stop that does not name them leaves schedules firing on a stopped
    cluster (P1 review, 2026-08-13). Restores the pre-host full-stop semantic."""
    a5, shell, sched = f"{_PREFIX}5", f"{_PREFIX}5-shell-1", "ava-schedule-7"
    native = _FakeNative({a5}, graceful_exits={a5})
    shells = _FakeShellBackend({shell, sched})
    _use_backends(monkeypatch, native, shells)

    _reap_agent_sessions(timeout_s=0.0)

    assert not shells.sessions, "every pty session must be reaped on a full stop"
    assert {name for name, _ in shells.killed} == {shell, sched}


def test_update_force_reap_spares_all_pty_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill_shells=False (update/restart) spares the whole pty namespace —
    agent shells AND schedule sessions persist across an update."""
    a5, shell, sched = f"{_PREFIX}5", f"{_PREFIX}5-shell-1", "ava-schedule-7"
    native = _FakeNative({a5}, graceful_exits={a5})
    shells = _FakeShellBackend({shell, sched})
    _use_backends(monkeypatch, native, shells)

    _reap_agent_sessions(timeout_s=0.0, kill_shells=False)

    assert shells.sessions == {shell, sched}, "an update must not touch pty sessions"
    assert shells.killed == []


def test_windows_reap_keeps_the_agent_prefix_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows-only scope guard: there the shell backend IS winproc, sharing
    `run/sessions` with the service roster — an unprefixed sweep would feed
    ava-ops/restarter/watchdog (and an in-flight ava-updater) into this reap,
    which runs BEFORE the ordered service stop. No pty shells exist on
    Windows (conventions/windows-setup.md), so the reap must keep the agent
    prefix: only agent-prefixed sessions may be touched, every service and
    orchestration session stays for `_stop_sessions`' ordered leg."""
    a5, shell = f"{_PREFIX}5", f"{_PREFIX}5-shell-1"
    services = {"ava-ops", "ava-restarter", "ava-agent-runner-watchdog", "ava-updater"}
    native = _FakeNative({a5}, graceful_exits={a5})
    shells = _FakeShellBackend(services | {shell})  # winproc: one shared namespace
    _use_backends(monkeypatch, native, shells)
    monkeypatch.setattr("cli.commands.stop.IS_WINDOWS", True)

    _reap_agent_sessions(timeout_s=0.0)

    assert {name for name, _ in shells.killed} == {shell}, "only the agent-prefixed session"
    assert shells.sessions == services, "service/orchestration sessions must remain untouched"
