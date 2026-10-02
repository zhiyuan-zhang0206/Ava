"""Unit tests for the per-host status fields added to ClusterStatus."""

import os
from pathlib import Path

import pytest

from ops import cluster_status
from ops.cluster_status import _check_pidfile, _count_agent_shells, agent_shell_sessions
from ops.rpc_schemas import SessionInfo


def _rows(*names: str) -> list[SessionInfo]:
    """SessionInfo rows named like a live session listing would return them."""
    return [SessionInfo(name=n) for n in names]


def test_collect_sessions_enumerates_both_backends(monkeypatch):
    """`_collect_sessions` merges the service backend and the shell backend,
    scoped to the cluster prefix, with timestamps where the backend records
    them."""

    class _FakeBackend:
        def __init__(self, names, started=None):
            self._names = names
            self._started = started  # pyright: ignore[reportUnknownMemberType]

        def list_sessions(self, prefix=""):
            return [n for n in self._names if n.startswith(prefix)]  # pyright: ignore[reportUnknownMemberType]

        def session_started_at(self, name: str) -> float | None:
            return self._started(name) if self._started else None  # pyright: ignore[reportUnknownMemberType]

        def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
            return {n: self.session_started_at(n) for n in names}

    svc = _FakeBackend(
        ["ava-main-restarter", "ava-main-gateway", "ava-main-agent-9", "other-stray"]
    )
    shell = _FakeBackend(["ava-main-agent-7-shell-0", "ava-main-agent-7-shell-0-watcher"])
    monkeypatch.setattr("base.sessions.backend.get_backend", lambda: svc)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", lambda: shell)  # pyright: ignore[reportUnknownMemberType]

    sessions, shell_count, total = cluster_status._collect_sessions()
    names = {s.name for s in sessions}
    assert names == {
        "ava-main-restarter",
        "ava-main-gateway",
        "ava-main-agent-7-shell-0",
        "ava-main-agent-7-shell-0-watcher",
    }  # stray AND the bare agent process (ava-main-agent-9) excluded
    assert shell_count == 2
    assert total == 4


def test_collect_sessions_records_uptime_when_started_at_known(monkeypatch):
    class _FakeBackend:
        def list_sessions(self, prefix=""):
            return ["ava-main-restarter"]

        def session_started_at(self, name: str) -> float | None:
            return 1000.0

        def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
            return {n: self.session_started_at(n) for n in names}

    backend = _FakeBackend()
    monkeypatch.setattr("base.sessions.backend.get_backend", lambda: backend)  # pyright: ignore[reportUnknownMemberType]

    empty = type(
        "_Empty",
        (),
        {
            "list_sessions": staticmethod(lambda _p="": []),  # pyright: ignore[reportUnknownArgumentType]
            "session_started_ats": staticmethod(lambda _names: {}),  # pyright: ignore[reportUnknownArgumentType]
        },
    )
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", lambda: empty)  # pyright: ignore[reportUnknownMemberType]
    sessions, _, _ = cluster_status._collect_sessions()
    assert sessions[0].created_at is not None
    assert sessions[0].uptime_seconds > 0


def test_count_agent_shells():
    sessions = _rows(
        "ava-main-agent-7-shell-0",
        "ava-main-agent-7-shell-0-watcher",
        "ava-main-agent-12-shell-3",
        "ava-main-restarter",  # not an agent shell
    )
    assert _count_agent_shells(sessions) == 3


def test_count_agent_shells_empty():
    assert _count_agent_shells([]) == 0


def _stub_sessions(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    # agent_shell_sessions reads `_collect_sessions()` (sessions, *counts);
    # stub it to the named rows so the test exercises only the filter.
    sessions = _rows(*names)
    monkeypatch.setattr(cluster_status, "_collect_sessions", lambda: (sessions, 0, 0))


def test_agent_shell_sessions_parses_and_filters(monkeypatch: pytest.MonkeyPatch):
    """Only the target agent's `-shell-<sid>[-<name>]` sessions, parsed to
    {id, name}, sorted by id. The agent's main session (no `-shell-`) and other
    agents' shells are excluded; an unnamed shell -> name None, a watcher ->
    name 'watcher', a named shell -> its slug."""
    _stub_sessions(
        monkeypatch,
        "ava-main-agent-7",  # main, excluded
        "ava-main-agent-7-shell-2-watcher",
        "ava-main-agent-7-shell-0",
        "ava-main-agent-7-shell-1-dev-server",
        "ava-main-agent-7-shell-3-page-dashboard",
        "ava-main-agent-12-shell-3",  # other agent
        "ava-main-restarter",  # not an agent
    )
    shells = agent_shell_sessions(7)
    assert [(s.id, s.name) for s in shells] == [
        (0, None),
        (1, "dev-server"),
        (2, "watcher"),
        (3, "page-dashboard"),
    ]


def test_agent_shell_sessions_none_for_agent_without_shells(monkeypatch: pytest.MonkeyPatch):
    _stub_sessions(monkeypatch, "ava-main-agent-7-shell-0")
    assert agent_shell_sessions(99) == []


# ─── capture_shell (the runner-side half of the shell monitor op) ─────────────


def _stub_capture_backend(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object] | None = None,
    *,
    output: str = "",
    error: Exception | None = None,
) -> None:
    # capture_shell imports get_shell_backend inside the function; patch the
    # module attribute so the per-call import picks the stub up.
    class _FakeBackend:
        def capture_pane(self, name: str, lines: int, *, scrollback: bool = True) -> str:
            if captured is not None:
                captured["name"] = name
                captured["lines"] = lines
                captured["scrollback"] = scrollback
            if error is not None:
                raise error
            return output

    monkeypatch.setattr("base.sessions.backend.get_shell_backend", _FakeBackend)


def test_capture_shell_reconstructs_name_and_captures(monkeypatch: pytest.MonkeyPatch):
    """capture_shell resolves the session via agent_shell_sessions, rebuilds the
    full session name (with `-<name>` suffix), captures with the requested
    depth through the shell backend, and returns (name, lines) with the
    trailing newline stripped."""
    from base.cluster import session_name

    stub = f"{session_name('agent-7')}-shell-3-watcher"
    _stub_sessions(monkeypatch, stub)
    captured: dict[str, object] = {}
    _stub_capture_backend(monkeypatch, captured, output="line one\nline two\n")
    name, lines, _, _ = cluster_status.capture_shell(7, 3, lines=200)
    assert name == f"{session_name('agent-7-shell-3')}-watcher"
    assert lines == ["line one", "line two"]
    assert captured["name"] == name
    assert captured["lines"] == 200
    assert captured["scrollback"] is True


def test_capture_shell_unnamed_session_no_suffix(monkeypatch: pytest.MonkeyPatch):
    """An unnamed shell (no `-<name>` segment) → full name without suffix."""
    from base.cluster import session_name

    stub = f"{session_name('agent-7')}-shell-1"
    _stub_sessions(monkeypatch, stub)
    _stub_capture_backend(monkeypatch, output="")
    name, lines, _, _ = cluster_status.capture_shell(7, 1)
    assert name == f"{session_name('agent-7-shell-1')}"
    assert lines == []


def test_capture_shell_unknown_session_raises(monkeypatch: pytest.MonkeyPatch):
    """No live shell with that id on this host → ShellNotFoundError (surfaces as
    a failed shell_capture op; the gateway 404s)."""
    _stub_sessions(monkeypatch, "ava-main-agent-7-shell-3")
    import pytest

    from ops.cluster_status import ShellNotFoundError

    with pytest.raises(ShellNotFoundError):
        cluster_status.capture_shell(7, 99)


def test_kill_shell_resolves_full_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.cluster import session_name
    from ops.rpc_schemas import ShellInfo

    killed: list[str] = []

    class _Backend:
        @staticmethod
        def kill_session_with_verdict(name: str) -> tuple[bool, str, bool]:
            killed.append(name)
            return True, "forced", True

    monkeypatch.setattr(
        cluster_status,
        "agent_shell_sessions",
        lambda _agent_id: [ShellInfo(id=3, name="build", uptime_seconds=1)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", _Backend)

    # the verdict rides the kill itself (one call, no separate probe)
    assert cluster_status.kill_shell(7, 3) == ("killed", True, "build")
    assert killed == [session_name("agent-7-shell-3-build")]


def test_kill_shell_uninspectable_backend_reports_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend without a kill verdict is treated as interrupted (fail-open):
    the reap may interrupt work it cannot see, so the notice must not be
    dropped."""
    from ops.rpc_schemas import ShellInfo

    class _Backend:
        @staticmethod
        def kill_session_with_verdict(_name: str) -> tuple[bool, str, bool]:
            raise NotImplementedError

        @staticmethod
        def kill_session(_name: str) -> tuple[bool, str]:
            return True, "forced"

    monkeypatch.setattr(
        cluster_status,
        "agent_shell_sessions",
        lambda _agent_id: [ShellInfo(id=3, name=None, uptime_seconds=1)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", _Backend)

    assert cluster_status.kill_shell(7, 3) == ("killed", True, None)


def test_kill_shell_idle_session_reports_not_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle shell (no running job) is reclaimed silently — interrupted
    False is what makes the gateway skip the notice."""
    from ops.rpc_schemas import ShellInfo

    class _Backend:
        @staticmethod
        def kill_session_with_verdict(_name: str) -> tuple[bool, str, bool]:
            return True, "forced", False

    monkeypatch.setattr(
        cluster_status,
        "agent_shell_sessions",
        lambda _agent_id: [ShellInfo(id=3, name=None, uptime_seconds=1)],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", _Backend)

    assert cluster_status.kill_shell(7, 3) == ("killed", False, None)


def test_kill_shell_missing_session_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_status, "agent_shell_sessions", lambda _agent_id: [])  # pyright: ignore[reportUnknownArgumentType]
    assert cluster_status.kill_shell(7, 99) == ("absent", False, None)


class _KillAllBackend:
    """Shell backend stub with the real kill contract: a kill is idempotent
    (an absent session answers ok, like the PTY and native backends); only a
    session in `survives` fails, because the backend could not confirm it gone."""

    def __init__(self, *, survives: frozenset[str] = frozenset()) -> None:
        self.killed: list[str] = []
        self.survives = survives

    def kill_session(self, name: str, *, graceful: bool = False) -> tuple[bool, str]:
        assert not graceful
        if name in self.survives:
            return False, "forced"
        self.killed.append(name)
        return True, "forced"


def test_kill_agent_shells_kills_only_the_owners_shell_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `-agent-<id>-shell-<sid>[-<name>]` session of the owner goes —
    watchers included; another agent's shells, non-shell sessions and the
    owner's `ava.ui.serve` page sessions (named by the page daemon's grammar)
    stay."""
    from base.cluster import session_name
    from base.sessions.page_session import page_session_name

    page = page_session_name(7, "dash_board", 3)
    _stub_sessions(
        monkeypatch,
        "ava-agent-7",
        "ava-agent-7-shell-0",
        "ava-agent-7-shell-2-watcher",
        page,
        "ava-agent-7-shell-5-dev-server",
        "ava-agent-71-shell-0",
        "ava-agent-12-shell-3",
        "ava-restarter",
    )
    backend = _KillAllBackend()
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", lambda: backend)

    assert cluster_status.kill_agent_shells(7) == [0, 2, 5]
    assert sorted(backend.killed) == sorted(
        [
            session_name("agent-7-shell-0"),
            session_name("agent-7-shell-2-watcher"),
            session_name("agent-7-shell-5-dev-server"),
        ]
    )
    assert page not in backend.killed
    assert cluster_status.kill_agent_shells(99) == []


def test_kill_agent_shells_raises_after_trying_every_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import session_name

    _stub_sessions(monkeypatch, "ava-agent-7-shell-0", "ava-agent-7-shell-1")
    backend = _KillAllBackend(survives=frozenset({session_name("agent-7-shell-0")}))
    monkeypatch.setattr("base.sessions.backend.get_shell_backend", lambda: backend)

    with pytest.raises(RuntimeError, match=r"shell session\(s\) \[0\] of agent 7"):
        cluster_status.kill_agent_shells(7)
    assert backend.killed == [session_name("agent-7-shell-1")]


def test_capture_shell_capture_failure_raises(monkeypatch: pytest.MonkeyPatch):
    """A backend capture failure (session died after probe) → RuntimeError."""
    _stub_sessions(monkeypatch, "ava-main-agent-7-shell-1")
    _stub_capture_backend(monkeypatch, error=RuntimeError("can't find pane"))
    import pytest

    with pytest.raises(RuntimeError, match="can't find pane"):
        cluster_status.capture_shell(7, 1)


def test_check_pidfile_alive(tmp_path: Path):
    pf = tmp_path / "live.pid"
    pf.write_text(str(os.getpid()))
    alive, pid = _check_pidfile(str(pf))
    assert alive is True
    assert pid == os.getpid()


def test_check_pidfile_missing(tmp_path: Path):
    alive, pid = _check_pidfile(str(tmp_path / "nope.pid"))
    assert alive is False
    assert pid is None


def test_check_pidfile_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pf = tmp_path / "dead.pid"
    pf.write_text("999999")

    def _raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError(3, "No such process")

    monkeypatch.setattr(os, "kill", _raise_oserror)
    alive, pid = _check_pidfile(str(pf))
    assert alive is False
    assert pid == 999999


# ─── row order ────────────────────────────────────────────────────────────────
