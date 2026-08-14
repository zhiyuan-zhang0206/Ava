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
from pathlib import Path

import pytest

from ops.controllers.base import BlockScope
from services.watchdog import daemon as wd


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
    monkeypatch.setattr(wd, "_checks_for_capability", lambda _role: fake_checks)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        lambda _r: [  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
        lambda _r: [  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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
    monkeypatch.setattr(wd, "_is_running", lambda _pidfile: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

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


def test_checks_for_capability_gateway_returns_gateway_set() -> None:
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
    # computer-mcp is gated on the platform's permissions-helper capability, not
    # on a setting — pin it "available" so the roster is env-independent (CI
    # hosts lack the helper and would otherwise drop the service).
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)
    names = [c.name for c in wd._checks_for_capability("agent-runner")]
    # Order follows build_services() (restarter before page-server before ops);
    # browser/browser-mcp gated out above; computer-mcp / mcp-daemon are plain
    # agent-runner services (no browser gate).
    assert names == [
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
