"""Watchdog daemon behavior tests.

The reconcile gates (pause / schema / pin) moved to the ops controller-manager and
are tested in `tests/ops/`; here `_tick` is a thin delegation — run the manager, then
run the healthchecks the reported `BlockScope` leaves in scope. What remains
watchdog-specific: that delegation, resolving a scope against the roster
(`_checks_for_round`), the run()-loop sleep-first ordering, the per-capability
pidfile selection, and the build_services-derived healthcheck roster.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from ops.controllers.base import BlockScope
from services.watchdog import daemon as wd
from shared.config import settings


@pytest.fixture
def counted_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the roster with noop healthchecks that record which ran."""
    calls: list[str] = []

    def _make(name: str):  # type: ignore[no-untyped-def]
        def _fn() -> None:
            calls.append(name)

        return _fn

    fake_checks = [
        wd._Check(n, _make(n), requires_db=n != "frontend")
        for n in ("gateway", "labeler", "frontend")
    ]
    monkeypatch.setattr(wd, "_checks_for_capability", lambda _role: fake_checks)  # pyright: ignore[reportUnknownArgumentType]
    return calls


class _FakeManager:
    """Stand-in for the ControllerManager: reconcile returns a fixed BlockScope and
    records the role it was called with."""

    def __init__(self, blocks: BlockScope) -> None:
        self._blocks = blocks
        self.seen_roles: list[str] = []

    async def reconcile(self, role: str) -> BlockScope:
        self.seen_roles.append(role)
        return self._blocks


# ─── _run_check de-duplicates a persistent failure line ──────────────────────


async def test_run_check_logs_a_failure_code_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A healthcheck that keeps exiting with the SAME code used to earn a fresh
    ERROR line every round on top of the healthcheck's own — the second half of
    the browser storm (1.8k lines/day across two hosts). One line per
    (check, code) episode; quiet rounds log DEBUG."""
    monkeypatch.setattr(wd, "_last_failure_code", {})

    def _exit3() -> None:
        raise SystemExit(3)

    with caplog.at_level(logging.ERROR, logger="services.watchdog.daemon"):
        await wd._run_check("browser", _exit3)
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="services.watchdog.daemon"):
        await wd._run_check("browser", _exit3)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_run_check_reports_again_when_the_exit_code_changes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """exit 1 after exit 3 is a different condition and a new first sight — the
    codes carry the meaning (retried next round vs needs a human)."""
    monkeypatch.setattr(wd, "_last_failure_code", {})

    def _exit(code: int):
        def _fn() -> None:
            raise SystemExit(code)

        return _fn

    with caplog.at_level(logging.ERROR, logger="services.watchdog.daemon"):
        await wd._run_check("browser", _exit(3))
        await wd._run_check("browser", _exit(1))
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 2


async def test_run_check_resets_the_episode_on_a_successful_round(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A round where the check does not fail clears the memory, so the next
    failure is a new episode and reports — the dedup must never mask a fresh
    failure after a recovery."""
    monkeypatch.setattr(wd, "_last_failure_code", {})

    def _exit3() -> None:
        raise SystemExit(3)

    with caplog.at_level(logging.ERROR, logger="services.watchdog.daemon"):
        await wd._run_check("browser", _exit3)
        await wd._run_check("browser", lambda: None)  # healthy round
        await wd._run_check("browser", _exit3)
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 2


# ─── _tick delegates to the controller-manager ───────────────────────────────


async def test_tick_runs_checks_when_no_controller_blocks(
    counted_checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No controller blocks → all healthchecks run."""
    monkeypatch.setattr(wd, "_manager", _FakeManager(blocks=BlockScope.NONE))
    await wd._tick("gateway")
    assert counted_checks == ["gateway", "labeler", "frontend"]


async def test_tick_skips_checks_when_a_controller_blocks(
    counted_checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-scoped blocker (paused / update spawned / pin self-heal) → skip every
    healthcheck this round."""
    monkeypatch.setattr(wd, "_manager", _FakeManager(blocks=BlockScope.ALL))
    await wd._tick("gateway")
    assert counted_checks == []


async def test_tick_runs_only_db_free_checks_on_a_db_scoped_block(
    counted_checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB-scoped blocker holds back the services that need Postgres and lets the
    rest of the roster run — the whole point of scoping the block."""
    monkeypatch.setattr(wd, "_manager", _FakeManager(blocks=BlockScope.DB_DEPENDENT))
    await wd._tick("gateway")
    assert counted_checks == ["frontend"]  # the one requires_db=False entry


async def test_tick_passes_capability_role_to_manager(
    counted_checks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's --role capability is passed to the manager so the pin
    controller can dispatch (agent-runner acts, gateway warns)."""
    mgr = _FakeManager(blocks=BlockScope.NONE)
    monkeypatch.setattr(wd, "_manager", mgr)
    await wd._tick("agent-runner")
    assert mgr.seen_roles == ["agent-runner"]


async def test_completed_round_records_freshness_and_emits_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a full round publishes the absolute timestamp consumed by health and OTLP."""
    emitted: list[tuple[str, str, dict[str, float]]] = []

    async def _completed_tick(_role: str) -> None:
        return

    def _emit(category: str, event_name: str, *, attributes: dict[str, float]) -> None:
        emitted.append((category, event_name, attributes))

    monkeypatch.setattr(wd, "_tick", _completed_tick)
    monkeypatch.setattr(wd.time, "time", lambda: 1_725_000_000.0)
    monkeypatch.setattr(wd.telemetry, "emit", _emit)
    progress = wd._TickProgress()

    assert await wd._run_tick_with_deadline("gateway", progress) is True
    assert progress.last_completed_at == 1_725_000_000.0
    assert emitted == [
        (
            "telemetry",
            "watchdog_tick",
            {"last_tick_timestamp_seconds": 1_725_000_000.0},
        )
    ]


async def test_round_deadline_skips_an_overdue_tick_without_advancing_freshness(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tick that stops awaiting must not keep the watchdog's own health fresh.

    The regression is an indefinitely blocked controller/check preventing any
    later round while the watchdog process remains alive and looks healthy.
    """
    tick_started = threading.Event()
    release_tick = threading.Event()
    calls: list[str] = []

    async def _blocked_tick(_role: str) -> None:
        def _block() -> None:
            calls.append("tick")
            tick_started.set()
            assert release_tick.wait(timeout=1)

        await asyncio.to_thread(_block)

    monkeypatch.setattr(wd, "_tick", _blocked_tick)
    monkeypatch.setattr(wd, "_TICK_DEADLINE_S", 0.01)
    progress = wd._TickProgress()

    in_flight: asyncio.Task[None] | None = None
    try:
        with caplog.at_level(logging.ERROR, logger="services.watchdog.daemon"):
            assert await wd._run_tick_with_deadline("gateway", progress) is False
            in_flight = progress.in_flight
            assert await wd._run_tick_with_deadline("gateway", progress) is False
    finally:
        release_tick.set()
        if in_flight is not None:
            await in_flight

    assert tick_started.is_set()
    assert calls == ["tick"]
    assert progress.last_completed_at is None
    assert any("tick exceeded 0.0s" in message for message in caplog.messages)


async def test_run_exposes_the_last_completed_tick_through_role_healthz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The watchdog's HTTP health surface exposes the completed-round timestamp
    and receives the same liveness clock the deadline advances."""
    pid_path = tmp_path / "gateway-watchdog.pid"
    monkeypatch.setattr(wd.settings.services, "gateway_watchdog_pidfile", pid_path)
    monkeypatch.setattr(wd, "_is_running", lambda _pidfile: False)  # pyright: ignore[reportUnknownArgumentType]
    health_calls: list[tuple[str, object, object]] = []
    stopped: list[object] = []

    async def _start_health(name: str, *, liveness: object, extra: object) -> object:
        health_calls.append((name, liveness, extra))
        return object()

    async def _stop_health(server: object) -> None:
        stopped.append(server)

    async def _completed_tick(_role: str, progress: wd._TickProgress) -> bool:
        progress.last_completed_at = 123.0
        return True

    sleeps = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(wd, "start_health_server", _start_health)
    monkeypatch.setattr(wd, "stop_health_server", _stop_health)
    monkeypatch.setattr(wd, "_run_tick_with_deadline", _completed_tick)
    monkeypatch.setattr(wd.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await wd.run("gateway")

    assert len(health_calls) == 1
    name, liveness, extra = health_calls[0]
    assert name == "gateway_watchdog"
    assert hasattr(liveness, "is_alive") and liveness.is_alive()  # type: ignore[union-attr]
    assert callable(extra)
    assert extra() == {"last_tick_at": 123.0}  # type: ignore[operator]
    assert len(stopped) == 1


# ─── one failing healthcheck must not take the watchdog down ─────────────────


async def test_healthcheck_sys_exit_does_not_kill_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every healthcheck signals "could not revive" with `sys.exit(1)` — correct
    for cron, but `SystemExit` is a BaseException, so an `except Exception` alone
    let it unwind out of `to_thread` and take the whole daemon with it. The
    watchdog that exists to revive dead services would die on the first service it
    could not revive."""
    ran: list[str] = []

    def _boom() -> None:
        raise SystemExit(1)

    def _after() -> None:
        ran.append("after")

    monkeypatch.setattr(wd, "_manager", _FakeManager(blocks=BlockScope.NONE))
    monkeypatch.setattr(
        wd,
        "_checks_for_capability",
        lambda _r: [  # pyright: ignore[reportUnknownArgumentType]
            wd._Check("boom", _boom, requires_db=False),
            wd._Check("after", _after, requires_db=False),
        ],
    )

    await wd._tick("agent-runner")  # must not raise

    # and the rest of the roster still runs
    assert ran == ["after"]


async def test_healthcheck_exception_does_not_kill_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(wd, "_manager", _FakeManager(blocks=BlockScope.NONE))
    monkeypatch.setattr(
        wd,
        "_checks_for_capability",
        lambda _r: [  # pyright: ignore[reportUnknownArgumentType]
            wd._Check(
                "boom", lambda: (_ for _ in ()).throw(RuntimeError("nope")), requires_db=False
            ),
            wd._Check("after", lambda: ran.append("after"), requires_db=False),
        ],
    )
    await wd._tick("agent-runner")
    assert ran == ["after"]


# ─── run() loop order: sleep must precede the first _tick ─────────────────────


async def test_run_loop_sleeps_before_first_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first _tick must come AFTER the first sleep, so it does not race
    cmd_start's service startup (PR-E)."""
    calls: list[str] = []
    pid_path = tmp_path / "gateway-watchdog.pid"
    monkeypatch.setattr(wd.settings.services, "gateway_watchdog_pidfile", pid_path)
    monkeypatch.setattr(wd, "_is_running", lambda _pidfile: False)  # pyright: ignore[reportUnknownArgumentType]

    async def fake_tick(_role: str) -> None:
        calls.append("tick")

    sleep_count = [0]

    async def fake_sleep(_s: float) -> None:
        calls.append("sleep")
        sleep_count[0] += 1
        if sleep_count[0] == 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(wd, "_tick", fake_tick)
    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await wd.run("gateway")

    assert calls[0] == "sleep", f"first event should be sleep, got calls={calls}"
    assert "tick" not in calls, f"_tick must not run before the first sleep, calls={calls}"


def test_pidfile_for_role_picks_per_capability_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_pidfile_for_role maps each capability to its own pidfile, so the two
    co-located watchdogs do not clobber one shared dup-start lock."""
    gw = tmp_path / "gateway-watchdog.pid"
    ar = tmp_path / "agent-runner-watchdog.pid"
    monkeypatch.setattr(wd.settings.services, "gateway_watchdog_pidfile", gw)
    monkeypatch.setattr(wd.settings.services, "agent_runner_watchdog_pidfile", ar)
    assert wd._pidfile_for_role("gateway") == gw
    assert wd._pidfile_for_role("agent-runner") == ar
    with pytest.raises(ValueError, match="unknown watchdog role"):
        wd._pidfile_for_role("bogus")  # type: ignore[arg-type]


# ─── per-capability healthcheck selection (build_services-derived roster) ─────


def test_checks_for_capability_gateway_returns_gateway_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test locks the gateway capability's healthcheck membership and
    # ordering, so milvus must be on the set: pin the milvus memory-search
    # backend (numpy, the default, gates the milvus service out — see
    # ops.spec._gate_reason / test_milvus_gated_out_unless_milvus_backend).
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "milvus")
    names = [c.name for c in wd._checks_for_capability("gateway")]
    assert "gateway" in names
    assert "labeler" in names
    assert "milvus" in names
    # task-maintenance is kept alive: the roster is derived from build_services(),
    # where it declares a healthcheck_module.
    assert "task-maintenance" in names
    # redis-acl runs first: the daemons below it crash on AuthenticationError until
    # the per-cluster ACL user (dropped by a redis restart) is re-affirmed.
    assert names[0] == "redis-acl"
    # milvus before memory-indexer: memory-indexer cold-start connects to milvus.
    assert names.index("milvus") < names.index("memory-indexer")
    assert "ops" not in names  # ops only on agent-runner
    assert "restarter" not in names  # restarter is agent-runner-only now


def test_checks_for_capability_agent_runner_returns_ops_restarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.config.settings.services.browser_enabled", False)  # env-independent
    monkeypatch.setattr("shared.config.settings.services.permissions_helper_enabled", True)
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "process")
    # computer-mcp is gated on the platform's permissions-helper capability, not
    # on a setting — pin it "available" so the roster is env-independent (CI
    # hosts lack the helper and would otherwise drop the service).
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)
    names = [c.name for c in wd._checks_for_capability("agent-runner")]
    # Host-policy and permissions-helper checks are hand-added; the remaining order follows
    # build_services() (restarter before page-server before ops). Browser checks
    # are gated out above; computer-mcp / mcp-daemon have no browser gate.
    assert names == [
        "brew-pin",
        "permissions-helper",
        "restarter",
        "page-server",
        "ops",
        "computer-mcp",
        "mcp-daemon",
        "otel-collector",
    ]


def test_checks_for_capability_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown watchdog role"):
        wd._checks_for_capability("bogus")  # type: ignore[arg-type]


def test_checks_for_capability_excludes_durably_disabled_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service in the durable --disable-service marker is dropped from the check
    set, so the 60s round stops reviving it."""
    monkeypatch.setattr(wd, "read_skipped", lambda: {"labeler"})

    names = {c.name for c in wd._checks_for_capability("gateway")}
    assert "labeler" not in names
    assert "gateway" in names


def test_checks_for_capability_skip_normalizes_kebab_snake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker stores the kebab session name (memory-indexer); is_skipped
    normalizes kebab/snake so a snake marker would land too."""
    monkeypatch.setattr(wd, "read_skipped", lambda: {"memory-indexer"})

    names = {c.name for c in wd._checks_for_capability("gateway")}
    assert "memory-indexer" not in names
    assert "labeler" in names


def test_checks_for_capability_no_skip_keeps_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty marker → the full capability check set is returned unchanged."""
    monkeypatch.setattr(wd, "read_skipped", set)

    names = {c.name for c in wd._checks_for_capability("gateway")}
    assert {"gateway", "labeler", "memory-indexer", "frontend"} <= names


# ─── remote-managed data plane drops the local-instance repairs ──────────────


def test_checks_for_capability_gateway_skips_local_repairs_when_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-managed data plane has no local Redis ACL user or PgBouncer to
    repair — both watchdog repairs target the per-cluster local instance, so
    the gateway roster must drop them (Task #1752)."""
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava:pw@10.9.8.7:5432/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "rediss://ava:pw@10.9.8.7:6380/0")
    names = [c.name for c in wd._checks_for_capability("gateway")]
    assert "redis-acl" not in names
    assert "pgbouncer" not in names
    assert "brew-pin" in names, "host-local checks stay regardless of the data plane"
