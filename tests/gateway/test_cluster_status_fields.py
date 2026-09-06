"""Unit tests for the per-host status fields added to ClusterStatus."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import gateway.routers.status as status_mod
from ops import cluster_status
from ops.cluster import (
    _check_pidfile,
    _count_agent_shells,
    agent_shell_sessions,
)
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
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: svc)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: shell)  # pyright: ignore[reportUnknownMemberType]

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
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)  # pyright: ignore[reportUnknownMemberType]

    empty = type(
        "_Empty",
        (),
        {
            "list_sessions": staticmethod(lambda _p="": []),  # pyright: ignore[reportUnknownArgumentType]
            "session_started_ats": staticmethod(lambda _names: {}),  # pyright: ignore[reportUnknownArgumentType]
        },
    )
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: empty)  # pyright: ignore[reportUnknownMemberType]
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

    monkeypatch.setattr("shared.session_backend.get_shell_backend", _FakeBackend)


def test_capture_shell_reconstructs_name_and_captures(monkeypatch: pytest.MonkeyPatch):
    """capture_shell resolves the session via agent_shell_sessions, rebuilds the
    full session name (with `-<name>` suffix), captures with the requested
    depth through the shell backend, and returns (name, lines) with the
    trailing newline stripped."""
    from shared.cluster import session_name

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
    from shared.cluster import session_name

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
    from ops.rpc_schemas import ShellInfo
    from shared.cluster import session_name

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
    monkeypatch.setattr("shared.session_backend.get_shell_backend", _Backend)

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
    monkeypatch.setattr("shared.session_backend.get_shell_backend", _Backend)

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
    monkeypatch.setattr("shared.session_backend.get_shell_backend", _Backend)

    assert cluster_status.kill_shell(7, 3) == ("killed", False, None)


def test_kill_shell_missing_session_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_status, "agent_shell_sessions", lambda _agent_id: [])  # pyright: ignore[reportUnknownArgumentType]
    assert cluster_status.kill_shell(7, 99) == ("absent", False, None)


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


def test_gather_cluster_status_local_agent_runner_probed(monkeypatch: pytest.MonkeyPatch):
    """A local agent-runner row is probed via its own ops server (status_probe,
    dialed at its registered localhost URL), picking up shell_count + daemon
    health from the op result — same path as any remote machine."""

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        assert kind == "status_probe"
        assert target_machine == "m1"
        assert ops_url == "http://localhost:9"
        # The real ops server always echoes its own machine_name in the ClusterStatus
        # probe result; the gateway verifies it matches the targeted row.
        return {
            "machine_name": "m1",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
            "running_sha": "def456",
            "shell_count": 4,
            "agent_host_online": True,
            "watchdog_online": False,
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        (
            "m1",
            "http://localhost:9",
            ["agent-runner", "gateway"],
            datetime.now(UTC),
            None,
            None,
            False,
        )
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert len(machines) == 1
    m = machines[0]
    assert m.online is True
    assert m.identity_mismatch is False
    assert m.head_sha == "abc123"
    assert m.running_sha == "def456"
    assert m.shell_count == 4
    assert m.agent_host_online is True
    assert m.watchdog_online is False


def test_probe_flags_identity_mismatch_when_responder_name_differs(monkeypatch: pytest.MonkeyPatch):
    """If the ops server answers under a machine_name != the targeted row, the
    probe returns a loud identity-mismatch row (online False, identity_mismatch
    True) instead of a false-green online. Guards the 2026-07-18 incident where a
    loopback gateway_url made the gateway dial itself and answer under its own name."""

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        assert target_machine == "air"
        assert ops_url == "http://localhost:8106"
        # The gateway (co-located ops server) answers under ITS name, not "air" — a
        # complete ClusterStatus (so it validates), just from the wrong host.
        return {
            "machine_name": "gateway-host",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("air", "http://localhost:8106", ["agent-runner"], datetime.now(UTC), None, None, False)
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))

    assert len(machines) == 1
    m = machines[0]
    assert m.name == "air"
    assert m.identity_mismatch is True
    assert m.online is False
    # It did NOT pick up the impostor's data.
    assert m.head_sha is None


def test_gather_cluster_status_local_pure_gateway_lightweight(monkeypatch: pytest.MonkeyPatch):
    """A local machine WITHOUT agent-runner capability (pure gateway) runs no
    ops server: its row is a lightweight local read — no probe dispatched, no
    session/pidfile reads, agent-runner-only fields at their defaults."""
    dispatched: list[str] = []

    async def _fake_dispatch(**kwargs):
        dispatched.append(kwargs["kind"])  # pyright: ignore[reportUnknownArgumentType]
        return {}

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: True)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("m1", "http://m1", ["gateway"], datetime.now(UTC), None, None, False)
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert dispatched == []
    m = machines[0]
    assert m.online is True
    assert m.paused is True
    assert m.head_sha == "abc123"
    assert m.shell_count == 0
    assert m.agent_host_online is None
    assert m.watchdog_online is None


# ─── deploy-hold stamping (the roster's `hold` column) ────────────────────────


def _hold_rows() -> list[
    tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]
]:
    """Two pure-gateway-shaped rows so the fan-out takes the no-probe path (the hold
    stamping is independent of what the probes return)."""
    now = datetime.now(UTC)
    return [
        ("m1", "http://m1", ["gateway"], now, None, None, False),
        ("m2", "http://m2", ["gateway"], now, None, None, False),
    ]


def test_gather_stamps_settle_hold_only_on_the_hosts_the_note_names(
    monkeypatch: pytest.MonkeyPatch,
):
    """A settle hold's note is the population: the hosts it names get
    settle_waited_on=True, every other row False — and the lease sentence is stamped
    on ALL rows, being cluster-global (same treatment as the pin verdict)."""
    from shared.cluster_lock import DeployLease, settle_note

    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: False)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")
    lease = DeployLease(
        holder="gateway-host:pid42", held_for_s=300.0, expires_in_s=600.0, note=settle_note(["m2"])
    )

    machines = asyncio.run(status_mod.gather_cluster_status(_hold_rows(), "m1", deploy_lease=lease))

    by_name = {m.name: m for m in machines}
    assert by_name["m2"].settle_waited_on is True
    assert by_name["m1"].settle_waited_on is False
    assert all(
        m.deploy_hold is not None and "gateway-host:pid42" in m.deploy_hold for m in machines
    )


def test_gather_stamps_hold_with_no_waited_on_hosts_for_an_executing_rollout(
    monkeypatch: pytest.MonkeyPatch,
):
    """A lease with no note is a rollout *executing*, not a settle hold: the sentence
    is stamped so the roster can explain the refusal, but no row is marked waited-on —
    there is no recorded waiting set to speak for."""
    from shared.cluster_lock import DeployLease

    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: False)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")
    lease = DeployLease(
        holder="gateway-host:pid42", held_for_s=60.0, expires_in_s=1740.0, note=None
    )

    machines = asyncio.run(status_mod.gather_cluster_status(_hold_rows(), "m1", deploy_lease=lease))

    assert all(m.deploy_hold is not None for m in machines)
    assert not any(m.settle_waited_on for m in machines)


def test_gather_leaves_hold_blank_when_no_lease(monkeypatch: pytest.MonkeyPatch):
    """No live lease -> both hold fields at their defaults, on every row."""
    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: False)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")

    machines = asyncio.run(status_mod.gather_cluster_status(_hold_rows(), "m1"))

    assert all(m.deploy_hold is None and m.settle_waited_on is False for m in machines)


def test_read_deploy_lease_degrades_on_operational_error(monkeypatch: pytest.MonkeyPatch):
    """A connectivity blip while reading the lease blanks the hold column instead of
    failing the roster — mid-rollout is exactly when the roster is asked for."""
    import psycopg

    def _boom() -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _boom)
    assert status_mod._read_deploy_lease() is None
