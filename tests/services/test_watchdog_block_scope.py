"""Round-blocking is SCOPED to the services the blocking reason applies to.

The schema controller's catch-all arm (a dead / unreachable DB) used to return one
boolean that meant "skip everything", so a Postgres outage also stopped `browser`
and `browser-mcp` from being revived — two agent-runner services that open no DB
connection at boot or at runtime. A DB outage took out the recovery path for an
unrelated Chrome crash, for a reason that had nothing to do with Chrome.

Now a controller reports a `BlockScope` (how wide its finding is) and each service
declares `ServiceSpec.requires_db` (what it needs); the watchdog matches the two in
`_checks_for_round`. These tests pin both directions:

- a DB-scoped block still revives the DB-free services, driven through a REAL
  `SchemaController` + `ControllerManager` against a connect() that raises, so the
  defect is pinned at the seam that produced it rather than against a fake verdict;
- a DB-dependent service is still held back — reviving one under a dead DB just
  crash-loops it once every 60s in its own `assert_schema_current`;
- and the classification itself, so flipping a service's `requires_db` has to be a
  deliberate edit rather than a side effect.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import pytest

import shared.db
from ops.controllers.base import BlockScope
from ops.controllers.schema import SchemaController
from ops.manager import ControllerManager
from ops.spec import build_services
from services.watchdog import daemon as wd


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which healthchecks a round actually ran, keeping the REAL roster
    derivation (so `requires_db` comes from each ServiceSpec) while stubbing the
    callables — the browser probe would otherwise dial CDP and respawn sessions.

    The recorded name is the healthcheck module's last segment (`browser`,
    `browser_mcp`, `restarter`, `ops`), which is what `_resolve_healthcheck` is
    handed.
    """
    calls: list[str] = []

    def _fake_resolve(module: str) -> Callable[[], None]:
        def _run() -> None:
            calls.append(module.rsplit(".", 1)[-1])

        return _run

    monkeypatch.setattr(wd, "_resolve_healthcheck", _fake_resolve)
    monkeypatch.setattr(
        wd,
        "permissions_helper_healthcheck",
        lambda: calls.append("permissions_helper"),
    )
    monkeypatch.setattr(wd, "read_skipped", set)
    # The browser pair is config/capability-gated; force it in so this is about
    # block scope, not about gating.
    monkeypatch.setattr(wd.settings.services, "browser_enabled", True)
    monkeypatch.setattr(wd.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    monkeypatch.setattr("ops.spec.browser_mcp_incapability", lambda: None)
    monkeypatch.setattr("ops.spec.runner_mode", lambda: "process")
    return calls


def _dead_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every DB connection attempt fail, the way an unreachable Postgres does."""

    def _refused(*_a: object, **_kw: object) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(shared.db, "connect", _refused)


async def test_db_outage_still_revives_the_browser_pair(
    ran: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round whose schema reconcile hit an unreachable DB must still run the
    healthchecks of the services that never touch it — and must NOT run the ones that
    do, which would spawn daemons that die in `assert_schema_current`."""
    _dead_db(monkeypatch)
    monkeypatch.setattr(wd, "_manager", ControllerManager([SchemaController()]))

    await wd._tick("agent-runner")

    assert ran == [
        "permissions_helper",
        "browser",
        "browser_mcp",
        "mcp_daemon",
        "otel_collector",
    ], (
        "the DB-free services must keep being revived through a DB outage; "
        "the DB-dependent restarter/ops must not"
    )


async def test_healthy_db_revives_the_whole_agent_runner_roster(
    ran: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse baseline: with the schema aligned, nothing is held back."""
    monkeypatch.setattr("ops.controllers.schema.check_schema_version", lambda _conn: None)  # pyright: ignore[reportUnknownArgumentType]
    # Pin computer-mcp's platform gate "available" (env-independent roster; CI
    # hosts lack the permissions helper and would drop the service).
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)

    class _NoopConn:
        def __enter__(self) -> _NoopConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

    monkeypatch.setattr(shared.db, "connect", lambda *_a, **_kw: _NoopConn())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(wd, "_manager", ControllerManager([SchemaController()]))

    await wd._tick("agent-runner")

    assert ran == [
        "permissions_helper",
        "restarter",
        "page_server",
        "ops",
        "browser",
        "browser_mcp",
        "computer_mcp",
        "mcp_daemon",
        "otel_collector",
    ]


async def test_db_outage_does_not_revive_the_dbs_users_into_a_starved_round(
    ran: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse of the fix, pinned deliberately because it is the contested half.

    An unreachable DB does NOT start reviving restarter/ops. It is tempting to argue
    that "DB unreachable" is no evidence a revived daemon would meet a newer schema —
    true, but it does not follow that the revive is safe: `assert_schema_current` opens
    its own connection, so unreachability alone fails the boot check. And the cost is
    not just an untidy crash loop — every DB-dependent healthcheck respawns via
    `respawn_and_verify` (20s deadline) while a round runs its checks sequentially in a
    60s interval, so doomed revives would starve exactly the DB-free services this
    scope keeps alive.
    """
    _dead_db(monkeypatch)
    monkeypatch.setattr(wd, "_manager", ControllerManager([SchemaController()]))

    await wd._tick("agent-runner")

    assert "restarter" not in ran and "ops" not in ran


def test_every_db_dependent_healthcheck_pays_a_verify_deadline() -> None:
    """The premise of the argument above, asserted rather than assumed: the services
    held back are precisely the ones whose respawn waits on a verify, so reviving them
    into a dead DB is what would overrun the round. If a future healthcheck stops
    using `respawn_and_verify`, this fails and the reasoning gets revisited."""
    import inspect

    from shared import service_respawn

    assert service_respawn._VERIFY_DEADLINE_S >= 10.0  # the cost this argument rests on
    for module in (
        "services.healthchecks.restarter",
        "services.healthchecks.ops",
        "services.healthchecks.gateway",
        "services.healthchecks.labeler",
        "services.healthchecks.heartbeat",
    ):
        src = inspect.getsource(importlib.import_module(module))
        assert "respawn_and_verify" in src, f"{module} no longer waits on a verify"


# ─── _checks_for_round: the one place a scope meets the roster ────────────────


def test_db_scoped_block_holds_back_exactly_the_dbs_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "read_skipped", set)
    monkeypatch.setattr(wd.settings.services, "browser_enabled", True)
    monkeypatch.setattr(wd.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: None)
    monkeypatch.setattr("ops.spec.browser_mcp_incapability", lambda: None)

    kept = {c.name for c in wd._checks_for_round("agent-runner", BlockScope.DB_DEPENDENT)}
    assert kept == {
        "brew-pin",
        "permissions-helper",
        "browser",
        "browser-mcp",
        "mcp-daemon",
        "otel-collector",
    }


def test_db_scoped_block_keeps_db_free_pseudo_checks_and_drops_pg_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-free pseudo-checks survive an outage; the pg-backup spec does not."""
    monkeypatch.setattr(wd, "read_skipped", set)

    kept = {c.name for c in wd._checks_for_round("gateway", BlockScope.DB_DEPENDENT)}
    assert "redis-acl" in kept
    assert "brew-pin" in kept
    assert "pg-backup" not in kept


def test_host_scoped_block_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paused / mid-update host does no work at all — and returns before the roster
    is even built, so the round costs nothing (no capability probes either)."""

    def _unexpected(_role: str) -> list[wd._Check]:
        raise AssertionError("BlockScope.ALL must not even build the roster")

    monkeypatch.setattr(wd, "_checks_for_capability", _unexpected)
    assert wd._checks_for_round("agent-runner", BlockScope.ALL) == []


def test_unblocked_round_gets_the_full_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    roster = [wd._Check("a", lambda: None, requires_db=True)]
    monkeypatch.setattr(wd, "_checks_for_capability", lambda _role: roster)  # pyright: ignore[reportUnknownArgumentType]
    assert wd._checks_for_round("gateway", BlockScope.NONE) == roster


def test_unknown_scope_explodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new BlockScope member must force a decision here rather than defaulting to
    "run everything" (unsafe) or "run nothing" (the bug this replaced)."""
    monkeypatch.setattr(wd, "_checks_for_capability", lambda _role: [])  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(ValueError, match="unhandled block scope"):
        wd._checks_for_round("gateway", "sideways")  # type: ignore[arg-type]


# ─── the classification itself ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("session", "requires_db"),
    [
        # Every one of these calls assert_schema_current at boot and then reads or
        # writes the DB, so a revive under a dead/drifted DB is a 60s crash loop.
        ("gateway", True),
        ("labeler", True),
        ("heartbeat", True),
        ("events-maintenance", True),
        ("restarter", True),
        ("ops", True),
        ("task-maintenance", True),
        # No DB connection anywhere in these: browser is a supervised Chrome,
        # browser-mcp a Unix-socket multiplexer in front of it, milvus a separate
        # vector store, memory-indexer a files+Milvus job (the one daemon that
        # documents skipping assert_schema_current), frontend an HTTP client of the
        # gateway.
        ("browser", False),
        ("browser-mcp", False),
        ("milvus", False),
        ("memory-indexer", False),
        ("frontend", False),
    ],
)
def test_service_db_dependence_is_declared_on_its_spec(session: str, requires_db: bool) -> None:
    spec = next(s for s in build_services() if s.session == session)
    assert spec.requires_db is requires_db
