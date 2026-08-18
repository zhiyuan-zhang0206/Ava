"""`shared.service_respawn` — healthcheck _restart common helper.

The respawn is backend-driven: kill the stale session on the service backend,
then launch through the service backend with the daemon's forward env. Tests
substitute a recording fake for the backend and assert the orchestration shape:
- First ``kill_session`` on the service backend (idempotent)
- Then ``new_session`` with ``forward_env_dict()`` (+ ``extra_env``)

``respawn_service`` takes a bare service kebab and composes the session
name via ``session_name``. Tests pin the composer to a
deterministic value so assertions don't depend on the dev host's
machine identity.

The `run_keepalive` cases below cover the three-way policy every daemon
healthcheck's ``main()`` now shares — no-op / respawn / report-and-stop — at the
level where it is defined once. The end-to-end version, driving the real
`probe_daemon` against a real health server, is in
`tests/services/test_healthcheck_terminal_state.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import shared.service_respawn as _sr_mod
from shared.daemon_health import EXIT_PORT_TAKEN, EXIT_RESPAWN_FAILED, DaemonProbe
from shared.service_respawn import respawn_service

_log = logging.getLogger("tests.shared.test_service_respawn")

# These call the REAL respawn_service (with the two backends faked) to assert
# the respawn shape, so they opt out of the autouse daemon-respawn guard that
# replaces it suite-wide (tests/conftest.py:_guard_service_respawn).
pytestmark = pytest.mark.real_service_respawn


class _FakeBackend:
    """Recording session backend: kills and launches are observed, nothing runs."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.killed: list[str] = []
        self.launched: list[tuple[str, str, Path, dict[str, str]]] = []
        self.new_ok = True

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        self.killed.append(name)
        return True, "forced"

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        self.launched.append((name, cmd, cwd, env))
        return self.new_ok

    def has_session(self, name: str) -> bool:
        return False

    def list_sessions(self, prefix: str = "") -> list[str]:
        return []


def test_respawn_kills_stale_session_then_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unified respawn: kill the stale session on the SERVICE backend, then
    launch through the service backend.

    The backend's own command wrapping (cd + venv activation + bash -lc) is
    exercised by the backend unit tests; here the respawn's orchestration shape
    is pinned: single kill, single launch, env = forward_env_dict() + extra_env.
    """
    # Pin the composer so the test does not depend on dev host machine_name.
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)

    ok = respawn_service(
        "gateway",
        ".venv/bin/python -m gateway",
        Path("/repo"),
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
    )
    assert ok is True
    # stale session dies on the service backend before the relaunch
    assert service.killed == ["t-gateway"]
    assert len(service.launched) == 1
    name, cmd, cwd, env = service.launched[0]
    assert name == "t-gateway"
    assert cmd == ".venv/bin/python -m gateway"
    assert cwd == Path("/repo")
    # env = the daemon forward view + extra_env wins
    assert env["AVA_PROCESS_PROFILE"] == "gateway"
    assert env["AVA_HOME"]  # forward_env_dict() carried the unit's config


# ── source-switch window (an update is mid-checkout) ───────────────────────


def test_respawn_holds_back_while_source_is_mid_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While an update's checkout is replacing the tree file by file, a respawn
    could import a half-written module — the win 2026-08-12/13 defect class. The
    respawn holds back (False) and the backend is not even touched; the update's
    own `ava start` relaunches the service, and the caller's probe retries next
    round if it did not."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr(_sr_mod, "is_switching", lambda: True)

    ok = respawn_service("browser", ".venv/bin/python -m services.browser.daemon", Path("/repo"))
    assert ok is False
    assert service.killed == []
    assert service.launched == []


def test_respawn_force_ignores_the_source_switch_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog-probe's contract is dumb revival that ignores every gate
    (cli/commands/_cluster_watchdog_probe.py passes force=True) — a dead
    watchdog must be revived even mid-update, or the host loses supervision
    for the whole window."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr(_sr_mod, "is_switching", lambda: True)

    ok = respawn_service(
        "agent-runner-watchdog",
        "python -m services.watchdog.daemon --role agent-runner",
        Path("/repo"),
        force=True,
    )
    assert ok is True
    assert service.killed == ["t-agent-runner-watchdog"]
    assert len(service.launched) == 1


def test_respawn_proceeds_when_no_switch_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker is absent in the normal case — the guard must be a no-op then
    (the existing respawn tests already exercise this through the real marker
    path; this pins the False branch explicitly)."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr(_sr_mod, "is_switching", lambda: False)

    ok = respawn_service("browser", ".venv/bin/python -m services.browser.daemon", Path("/repo"))
    assert ok is True
    assert len(service.launched) == 1


def test_respawn_returns_false_when_backend_launch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend refusing the launch → returns False, does not raise."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    service.new_ok = False
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)

    ok = respawn_service("scheduler", "cmd", Path("/repo"))
    assert ok is False


def test_respawn_and_verify_reports_the_probe_not_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful spawn is NOT the success signal — the daemon's own
    probe is. The pane's process can die milliseconds later (EADDRINUSE against a
    process already holding the port, import error, schema-drift exit), which is
    exactly what happened nine times in a row on 2026-07-24 while the healthcheck
    logged "restarted successfully" each time."""
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    result = _sr_mod.respawn_and_verify(
        "restarter",
        "cmd",
        Path("/repo"),
        verify=lambda: DaemonProbe.down("healthz unreachable"),
        deadline_s=0.05,
        interval_s=0.01,
    )
    assert result.alive is False
    assert result.detail == "healthz unreachable"


def test_respawn_and_verify_returns_alive_once_the_probe_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polls until the daemon answers — a daemon needs a moment to bind."""
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    attempts = {"n": 0}

    def verify() -> DaemonProbe:
        attempts["n"] += 1
        return DaemonProbe.up("pid 42") if attempts["n"] >= 3 else DaemonProbe.down("not yet")

    result = _sr_mod.respawn_and_verify(
        "restarter", "cmd", Path("/repo"), verify=verify, deadline_s=5.0, interval_s=0.01
    )
    assert result.alive is True
    assert attempts["n"] == 3


def test_respawn_and_verify_skips_probing_when_the_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the spawn itself refused → report that, and do not spend the verify deadline."""
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    probed = {"n": 0}

    def verify() -> DaemonProbe:
        probed["n"] += 1
        return DaemonProbe.up("unexpected")

    result = _sr_mod.respawn_and_verify(
        "restarter", "cmd", Path("/repo"), verify=verify, deadline_s=5.0, interval_s=0.01
    )
    assert result.alive is False
    assert "t-restarter" in result.detail
    assert probed["n"] == 0


def test_respawn_and_verify_stops_polling_on_a_terminal_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign occupant appearing mid-respawn ends the poll at once.

    The respawned daemon has already died on the bound port, so the rest of the
    20s deadline is spent waiting for a process that will not yield — ~45s per
    round of exactly that is what set a Windows box's 2-minute watchdog cadence."""
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    attempts = {"n": 0}

    def verify() -> DaemonProbe:
        attempts["n"] += 1
        return DaemonProbe.port_taken("home='/home/ava/.ava' != ...")

    result = _sr_mod.respawn_and_verify(
        "restarter", "cmd", Path("/repo"), verify=verify, deadline_s=5.0, interval_s=0.01
    )
    assert result.terminal is True
    assert attempts["n"] == 1, "a terminal verdict must not be re-polled"


def test_run_keepalive_noops_on_a_live_daemon() -> None:
    """The common case, once every 60s per service."""
    _sr_mod.run_keepalive(
        "labeler",
        _log,
        probe=lambda: DaemonProbe.up("pid 42"),
        respawn=lambda: pytest.fail("must not respawn a live daemon"),
    )


def test_run_keepalive_respawns_a_plain_dead_daemon() -> None:
    """Regression guard on the 98-minute outage's fix: "not alive" still means
    "revive it", for every verdict except the terminal one."""
    respawns: list[int] = []
    _sr_mod.run_keepalive(
        "labeler",
        _log,
        probe=lambda: DaemonProbe.down("healthz unreachable"),
        respawn=lambda: (respawns.append(1), DaemonProbe.up("pid 42"))[1],
    )
    assert respawns == [1]


def test_run_keepalive_does_not_respawn_a_terminal_verdict(caplog) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Loud and stop, not silent give-up: ERROR + the distinct exit code, no
    respawn. A healthcheck that quietly declines to heal is the 98-minute outage's
    shape, so the report is the non-negotiable half of "stop retrying"."""
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as excinfo:  # pyright: ignore[reportUnknownMemberType]
        _sr_mod.run_keepalive(
            "ops",
            _log,
            probe=lambda: DaemonProbe.port_taken("another unit's daemon holds this port"),
            respawn=lambda: pytest.fail("must not respawn against an occupant"),
        )
    assert excinfo.value.code == EXIT_PORT_TAKEN
    assert "another unit's daemon holds this port" in caplog.text  # pyright: ignore[reportUnknownMemberType]


def test_run_keepalive_runs_the_fallback_when_no_daemon_will_be_live() -> None:
    """`on_unrevivable` is the caller's stand-in for a round that will have no live
    daemon: the respawn failed to verify, or was skipped as futile. A verified respawn
    must NOT trigger it — the daemon does its own catch-up then."""
    ran: list[str] = []
    with pytest.raises(SystemExit):
        _sr_mod.run_keepalive(
            "restarter",
            _log,
            probe=lambda: DaemonProbe.port_taken("occupied"),
            respawn=lambda: pytest.fail("must not respawn"),
            on_unrevivable=lambda: ran.append("terminal"),
        )
    with pytest.raises(SystemExit):
        _sr_mod.run_keepalive(
            "restarter",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: DaemonProbe.down("still unreachable"),
            on_unrevivable=lambda: ran.append("respawn-failed"),
        )
    _sr_mod.run_keepalive(
        "restarter",
        _log,
        probe=lambda: DaemonProbe.down("healthz unreachable"),
        respawn=lambda: DaemonProbe.up("pid 42"),
        on_unrevivable=lambda: ran.append("revived — must not happen"),
    )
    assert ran == ["terminal", "respawn-failed"]


def test_run_keepalive_never_runs_the_fallback_before_the_respawn() -> None:
    """The ordering invariant `services/healthchecks/restarter.py` documents, held
    here so it cannot be broken from a healthcheck module: the stand-in reads the DB,
    and a DB outage must not stand between a dead verdict and the respawn."""
    order: list[str] = []
    with pytest.raises(SystemExit):
        _sr_mod.run_keepalive(
            "restarter",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: (order.append("respawn"), DaemonProbe.down("still down"))[1],
            on_unrevivable=lambda: order.append("fallback"),
        )
    assert order == ["respawn", "fallback"]


def test_run_keepalive_separates_the_two_failure_exit_codes() -> None:
    """An occupant appearing during the respawn keeps the terminal code, while a
    daemon that simply did not come up keeps 1 and is retried next round."""
    with pytest.raises(SystemExit) as taken:
        _sr_mod.run_keepalive(
            "ops",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: DaemonProbe.port_taken("occupant appeared mid-respawn"),
        )
    assert taken.value.code == EXIT_PORT_TAKEN

    with pytest.raises(SystemExit) as failed:
        _sr_mod.run_keepalive(
            "ops",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: DaemonProbe.down("still unreachable"),
        )
    assert failed.value.code == EXIT_RESPAWN_FAILED


def test_respawn_is_native_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-switch the service respawn is native; there is no shell-backend
    cleanup leg at all — the native path has no legacy dependency."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")

    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)

    ok = respawn_service("x", "cmd", Path("/repo"))
    assert ok is True
    assert service.launched, "the native launch must still happen"


def test_respawn_judges_the_checkout_not_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service may run from a subdirectory of its checkout — the frontend runs
    npm inside ``<checkout>/ui/web`` — and the launch-site guard judges the
    checkout, not that working directory. Judging the working directory read the
    prod home's OWN frontend as a foreign checkout and refused every legitimate
    restart, leaving the frontend the one service that could not self-heal."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    from shared import paths as _paths

    monkeypatch.setattr(_paths, "ava_home", lambda: Path.home() / ".ava")
    source = Path.home() / ".ava" / "source"

    ok = respawn_service("frontend", "npm run start", source / "ui" / "web", checkout=source)

    assert ok is True
    # The session still starts in ui/web/ — only the guard's subject moved.
    assert service.launched[0][2] == source / "ui" / "web"
    # The contrast that names the bug: judged by the working directory alone,
    # the prod home's own frontend is read as a foreign checkout and refused.
    assert respawn_service("frontend", "npm run start", source / "ui" / "web") is False


def test_respawn_checkout_arg_cannot_launder_a_dev_checkout(
    monkeypatch: pytest.MonkeyPatch,
    caplog,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """``checkout`` names the checkout being judged; it does not exempt anything.
    A dev checkout is refused whether or not the working directory sits inside
    it, so the frontend fix cannot become a way around Task #966."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    from shared import paths as _paths

    monkeypatch.setattr(_paths, "ava_home", lambda: Path.home() / ".ava")
    worktree = Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt"
    with caplog.at_level(logging.ERROR, logger="shared.service_respawn"):  # pyright: ignore[reportUnknownMemberType]
        ok = respawn_service("frontend", "x", worktree / "ui" / "web", checkout=worktree)
    assert ok is False
    assert "01:13 worktree accident" in caplog.text  # pyright: ignore[reportUnknownMemberType]


def test_respawn_refuses_prod_home_from_a_dev_checkout(
    monkeypatch: pytest.MonkeyPatch,
    caplog,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """Task #966, respawn side: a prod-home unit must never respawn a daemon
    from a dev checkout's code — the respawn would re-bind the daemon to a
    checkout that routine cleanup can delete out from under it. Refuses
    BEFORE any session-backend call."""
    from shared import paths as _paths

    monkeypatch.setattr(_paths, "ava_home", lambda: Path.home() / ".ava")
    with caplog.at_level(logging.ERROR, logger="shared.service_respawn"):  # pyright: ignore[reportUnknownMemberType]
        ok = respawn_service("gateway", "x", Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt")
    assert ok is False
    assert "01:13 worktree accident" in caplog.text  # pyright: ignore[reportUnknownMemberType]
