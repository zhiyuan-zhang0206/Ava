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

import shared.log as _shared_log
import shared.service_respawn as _sr_mod
from shared.daemon_health import EXIT_PORT_TAKEN, DaemonProbe
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
        self.kill_calls: list[tuple[str, bool, float]] = []
        self.launched: list[tuple[str, str, Path, dict[str, str]]] = []
        self.new_ok = True

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        self.killed.append(name)
        self.kill_calls.append((name, graceful, timeout))
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
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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


def test_respawn_can_give_a_session_a_bounded_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector can request a short SIGTERM window before the backend escalates."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)

    assert respawn_service(
        "otel-collector", "otelcol --config config.yaml", Path("/repo"), graceful_timeout_s=5.0
    )

    assert service.kill_calls == [("t-otel-collector", True, 5.0)]


# ── source-switch window (an update is mid-checkout) ───────────────────────


def test_respawn_holds_back_while_source_is_mid_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While an update's checkout is replacing the tree file by file, a respawn
    could import a half-written module — the win 2026-08-12/13 defect class. The
    respawn holds back (False) and the backend is not even touched; the update's
    own `ava start` relaunches the service, and the caller's probe retries next
    round if it did not."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
    service = _FakeBackend("service")
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: service)
    monkeypatch.setattr(_sr_mod, "is_switching", lambda: False)

    ok = respawn_service("browser", ".venv/bin/python -m services.browser.daemon", Path("/repo"))
    assert ok is True
    assert len(service.launched) == 1


def test_respawn_returns_false_when_backend_launch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend refusing the launch → returns False, does not raise."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "respawn_service", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]

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


def test_run_keepalive_waits_for_consecutive_failures_before_respawning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway's transient DB degradation must survive one watchdog round."""
    respawns: list[int] = []

    def _run() -> None:
        _sr_mod.run_keepalive(
            "gateway-threshold-test",
            _log,
            probe=lambda: DaemonProbe.down("healthz degraded"),
            respawn=lambda: (respawns.append(1), DaemonProbe.up("pid 42"))[1],
            consecutive_failures_before_respawn=2,
        )

    with caplog.at_level(logging.WARNING):
        _run()
    assert respawns == []
    assert "probe failed (1/2) — not respawning yet" in caplog.text
    _run()
    assert respawns == [1]


def test_run_keepalive_success_resets_consecutive_failure_count() -> None:
    """A recovered probe cannot combine with an earlier transient failure."""
    label = "gateway-threshold-reset-test"
    respawns: list[int] = []

    def _respawn() -> DaemonProbe:
        respawns.append(1)
        return DaemonProbe.up("pid 42")

    _sr_mod.run_keepalive(
        label,
        _log,
        probe=lambda: DaemonProbe.down("degraded"),
        respawn=_respawn,
        consecutive_failures_before_respawn=2,
    )
    _sr_mod.run_keepalive(
        label,
        _log,
        probe=lambda: DaemonProbe.up("pid 42"),
        respawn=_respawn,
        consecutive_failures_before_respawn=2,
    )
    _sr_mod.run_keepalive(
        label,
        _log,
        probe=lambda: DaemonProbe.down("degraded"),
        respawn=_respawn,
        consecutive_failures_before_respawn=2,
    )
    assert respawns == []


def test_run_keepalive_does_not_respawn_a_terminal_verdict(caplog) -> None:
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
    must NOT trigger it — the daemon does its own catch-up then.

    Since #1941 a failed respawn schedules the backoff and returns, but the
    fallback must still run on that round (the restarter's stand-in keeps moving
    `restarting` rows while the daemon stays down)."""
    ran: list[str] = []
    with pytest.raises(SystemExit):
        _sr_mod.run_keepalive(
            "restarter-terminal",
            _log,
            probe=lambda: DaemonProbe.port_taken("occupied"),
            respawn=lambda: pytest.fail("must not respawn"),
            on_unrevivable=lambda: ran.append("terminal"),
        )
    _sr_mod.run_keepalive(
        "restarter-failed",
        _log,
        probe=lambda: DaemonProbe.down("healthz unreachable"),
        respawn=lambda: DaemonProbe.down("still unreachable"),
        on_unrevivable=lambda: ran.append("respawn-failed"),
    )
    _sr_mod.run_keepalive(
        "restarter-revived",
        _log,
        probe=lambda: DaemonProbe.down("healthz unreachable"),
        respawn=lambda: DaemonProbe.up("pid 42"),
        on_unrevivable=lambda: ran.append("revived — must not happen"),
    )
    assert ran == ["terminal", "respawn-failed"]


def test_run_keepalive_never_runs_the_fallback_before_the_respawn() -> None:
    """The ordering invariant `services/healthchecks/restarter.py` documents, held
    here so it cannot be broken from a healthcheck module: the stand-in reads the DB,
    and a DB outage must not stand between a dead verdict and the respawn. The
    failed respawn no longer exits (#1941) but the order stays: respawn first,
    fallback after."""
    order: list[str] = []
    _sr_mod.run_keepalive(
        "restarter",
        _log,
        probe=lambda: DaemonProbe.down("healthz unreachable"),
        respawn=lambda: (order.append("respawn"), DaemonProbe.down("still down"))[1],
        on_unrevivable=lambda: order.append("fallback"),
    )
    assert order == ["respawn", "fallback"]


def test_run_keepalive_separates_the_two_failure_exit_codes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An occupant appearing during the respawn keeps the terminal code, while a
    daemon that simply did not come up no longer exits (#1941): it schedules the
    backoff and returns, with the failure reported as a WARNING naming the next
    attempt."""
    with pytest.raises(SystemExit) as taken:
        _sr_mod.run_keepalive(
            "ops-terminal-mid-respawn",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: DaemonProbe.port_taken("occupant appeared mid-respawn"),
        )
    assert taken.value.code == EXIT_PORT_TAKEN

    with caplog.at_level(logging.WARNING):
        _sr_mod.run_keepalive(
            "ops-failed-respawn",
            _log,
            probe=lambda: DaemonProbe.down("healthz unreachable"),
            respawn=lambda: DaemonProbe.down("still unreachable"),
        )
    assert "daemon restart FAILED (still unreachable) — next respawn attempt in 60s" in caplog.text


def test_respawn_is_native_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-switch the service respawn is native; there is no shell-backend
    cleanup leg at all — the native path has no legacy dependency."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
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
    caplog,
) -> None:
    """``checkout`` names the checkout being judged; it does not exempt anything.
    A dev checkout is refused whether or not the working directory sits inside
    it, so the frontend fix cannot become a way around Task #966."""
    monkeypatch.setattr(_sr_mod, "session_name", lambda svc: f"t-{svc}")  # pyright: ignore[reportUnknownArgumentType]
    from shared import paths as _paths

    monkeypatch.setattr(_paths, "ava_home", lambda: Path.home() / ".ava")
    worktree = Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt"
    with caplog.at_level(logging.ERROR, logger="shared.service_respawn"):  # pyright: ignore[reportUnknownMemberType]
        ok = respawn_service("frontend", "x", worktree / "ui" / "web", checkout=worktree)
    assert ok is False
    assert "01:13 worktree accident" in caplog.text  # pyright: ignore[reportUnknownMemberType]


def test_respawn_refuses_prod_home_from_a_dev_checkout(
    monkeypatch: pytest.MonkeyPatch,
    caplog,
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


# ── #1941: exponential backoff + circuit breaker (flap regression) ─────────


class _FakeClock:
    """Controllable stand-in for time.monotonic — rounds advance by stepping now."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _AlertRecorder:
    """Stands in for shared.log.logger; records every structured call so the test
    can assert the hold alert fired exactly once with the right event name."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def debug(self, *_args: object, **_kwargs: object) -> None:
        pass

    def error(self, *_args: object, **_kwargs: object) -> None:
        pass

    def alerts(self) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == "respawn_breaker_open"]


def _round(
    clock: _FakeClock,
    seconds: float,
    respawns: list[float],
    fallbacks: list[str],
    probe_down: bool = True,
) -> None:
    """One keepalive round at `seconds` on the fake clock: probe DOWN (unless
    probe_down), respawn that never verifies alive, and a recording fallback."""
    clock.now = seconds

    def _respawn() -> DaemonProbe:
        respawns.append(clock.now)
        return DaemonProbe.down("still unreachable")

    _sr_mod.run_keepalive(
        "flap",
        _log,
        probe=lambda: (
            DaemonProbe.down("healthz unreachable") if probe_down else DaemonProbe.up("pid 42")
        ),
        respawn=_respawn,
        on_unrevivable=lambda: fallbacks.append("unrevivable"),
    )


def test_run_keepalive_backs_off_exponentially_and_holds_after_breaker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1941 flap regression — mock probe constant DOWN, respawn never alive:

    - respawn calls grow exponentially sparse (t=0, 60, 180 with a 60s base), not
      one per round — the #927 shape (GCS unreachable 2h+) that used to earn a
      kill+restart every 60s forever;
    - once breaker_rounds (5) consecutive non-alive rounds pass, respawns stop
      entirely and the round holds, WARNINGing the hold age each round;
    - the hold alert (event=respawn_breaker_open) fires exactly once per episode;
    - the on_unrevivable fallback still runs after every failed respawn, never
      before it.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_sr_mod, "_monotonic", clock)
    recorder = _AlertRecorder()
    monkeypatch.setattr(_shared_log, "logger", recorder)
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_backoff_cap_seconds", 10000.0)
    respawns: list[float] = []
    fallbacks: list[str] = []

    with caplog.at_level(logging.WARNING):
        for t in range(13):
            _round(clock, float(t * 60), respawns, fallbacks)

    # Exponential sparseness: 60s, then 120s, then the breaker holds forever.
    assert respawns == [0.0, 60.0, 180.0]
    # The fallback ran after each failed respawn — never instead of one.
    assert fallbacks == ["unrevivable", "unrevivable", "unrevivable"]
    # Exactly one hold alert for the whole episode.
    assert len(recorder.alerts()) == 1
    alert = recorder.alerts()[0]
    assert alert["label"] == "flap"
    assert alert["rounds"] == 5
    # The round that tripped the breaker already reports the hold, then every
    # round after it WARNINGs the (growing) hold age instead of respawning.
    assert caplog.text.count("respawn held for") == 9
    assert "respawn held for 0s" in caplog.text
    assert "respawn held for 300s" in caplog.text
    # One backoff-skip round between the 2nd and 3rd attempt.
    assert caplog.text.count("backing off after a failed respawn") == 1
    assert "next attempt in 60s" in caplog.text


def test_run_keepalive_alive_round_resets_backoff_counter_and_breaker(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reverse direction: one probe-alive round fully resets the per-label state —
    the second failure episode starts the backoff from the base (no stale delay
    from the first episode) and earns its own hold alert when its breaker trips."""
    clock = _FakeClock()
    monkeypatch.setattr(_sr_mod, "_monotonic", clock)
    recorder = _AlertRecorder()
    monkeypatch.setattr(_shared_log, "logger", recorder)
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_backoff_cap_seconds", 10000.0)
    respawns: list[float] = []
    fallbacks: list[str] = []

    # Episode 1: failed respawns at t=0, 60, 180 (backoff skip at t=120), the
    # breaker tripping at its 5th non-alive round (t=240 → alert #1), one held
    # round, then recovery.
    with caplog.at_level(logging.WARNING):
        _round(clock, 0.0, respawns, fallbacks)
        _round(clock, 60.0, respawns, fallbacks)
        _round(clock, 120.0, respawns, fallbacks)
        _round(clock, 180.0, respawns, fallbacks)
        _round(clock, 240.0, respawns, fallbacks)
        _round(clock, 300.0, respawns, fallbacks)
        _round(clock, 360.0, respawns, fallbacks, probe_down=False)
        # Episode 2: the FIRST down round respawns immediately — a stale backoff
        # deadline (episode 1's last attempt set next=t=420... with the reset it is
        # cleared) and the open breaker must not delay or block it.
        _round(clock, 420.0, respawns, fallbacks)
        _round(clock, 480.0, respawns, fallbacks)
        _round(clock, 540.0, respawns, fallbacks)
        _round(clock, 600.0, respawns, fallbacks)
        # Episode 2's breaker trips at its own 5th non-alive round → alert #2.
        _round(clock, 660.0, respawns, fallbacks)
        _round(clock, 720.0, respawns, fallbacks)

    assert respawns == [0.0, 60.0, 180.0, 420.0, 480.0, 600.0]
    assert len(recorder.alerts()) == 2, "one alert per episode, not one per round"
    assert caplog.text.count("respawn held for") == 4


def test_run_keepalive_backoff_delay_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2^n growth stops at the configured cap: with base 60s and cap 100s the
    third respawn lands at t=160 (60+100), not t=180 (60+120) as uncapped growth
    would."""
    clock = _FakeClock()
    monkeypatch.setattr(_sr_mod, "_monotonic", clock)
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_backoff_cap_seconds", 100.0)
    respawns: list[float] = []
    fallbacks: list[str] = []
    _round(clock, 0.0, respawns, fallbacks)
    _round(clock, 60.0, respawns, fallbacks)
    _round(clock, 120.0, respawns, fallbacks)  # 160-120=40s of backoff left → skip
    _round(clock, 160.0, respawns, fallbacks)  # cap'd delay elapsed → attempt
    assert respawns == [0.0, 60.0, 160.0]


def test_run_keepalive_verified_alive_respawn_resets_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A respawn that VERIFIES alive is the same healing signal as a probe-alive
    round: the backoff deadline set by an earlier failed respawn must not delay
    the next episode's first respawn."""
    clock = _FakeClock()
    monkeypatch.setattr(_sr_mod, "_monotonic", clock)
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_backoff_cap_seconds", 10000.0)
    respawns: list[float] = []
    fallbacks: list[str] = []

    # Failed respawn at t=0 (schedules next at t=60), then a respawn that
    # verifies alive at t=60 — and a failed one at t=120, which must happen
    # immediately (no stale 60s window from the t=0 attempt).
    clock.now = 0.0
    _sr_mod.run_keepalive(
        "verified-reset",
        _log,
        probe=lambda: DaemonProbe.down("down"),
        respawn=lambda: (respawns.append(0.0), DaemonProbe.down("still down"))[1],
        on_unrevivable=lambda: fallbacks.append("f1"),
    )
    clock.now = 60.0
    _sr_mod.run_keepalive(
        "verified-reset",
        _log,
        probe=lambda: DaemonProbe.down("down"),
        respawn=lambda: (respawns.append(60.0), DaemonProbe.up("pid 42"))[1],
        on_unrevivable=lambda: fallbacks.append("must-not"),
    )
    clock.now = 120.0
    _sr_mod.run_keepalive(
        "verified-reset",
        _log,
        probe=lambda: DaemonProbe.down("down"),
        respawn=lambda: (respawns.append(120.0), DaemonProbe.down("still down"))[1],
        on_unrevivable=lambda: fallbacks.append("f2"),
    )

    assert respawns == [0.0, 60.0, 120.0], (
        "the t=120 attempt must not be delayed by the t=0 backoff deadline — "
        "the verified-alive respawn reset it"
    )
    assert fallbacks == ["f1", "f2"]


def test_run_keepalive_rejects_a_breaker_at_or_below_the_respawn_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breaker that would trip the round the respawn threshold is met — or
    before — is a dead configuration: the breaker check runs before the threshold
    branch, so breaker_rounds == threshold would hold without a single respawn
    attempt. Refuse both at the same place the other policy parameters are
    validated."""
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_breaker_rounds", 2)
    with pytest.raises(ValueError, match="watchdog_respawn_breaker_rounds"):
        _sr_mod.run_keepalive(
            "bad-config-below",
            _log,
            probe=lambda: DaemonProbe.down("down"),
            respawn=lambda: DaemonProbe.up("pid 42"),
            consecutive_failures_before_respawn=3,
        )
    # The == boundary is a dead config too (zero respawn rounds before the hold).
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_breaker_rounds", 3)
    with pytest.raises(ValueError, match="watchdog_respawn_breaker_rounds"):
        _sr_mod.run_keepalive(
            "bad-config-equal",
            _log,
            probe=lambda: DaemonProbe.down("down"),
            respawn=lambda: DaemonProbe.up("pid 42"),
            consecutive_failures_before_respawn=3,
        )
    # One round past the threshold is the tightest valid configuration: exactly
    # one respawn attempt before the breaker holds.
    monkeypatch.setattr(_sr_mod.settings.services, "watchdog_respawn_breaker_rounds", 4)
    _sr_mod.run_keepalive(
        "good-config",
        _log,
        probe=lambda: DaemonProbe.down("down"),
        respawn=lambda: DaemonProbe.up("pid 42"),
        consecutive_failures_before_respawn=3,
    )
