"""The pin/schema livelock, composed (issue #1074).

The two controllers are individually correct and were jointly non-convergent. Once
prod's DB was ahead of the pinned commit, the schema controller spawned `ava cluster update`
(HEAD forward, onto a commit carrying the migrations) and the pin controller
force-checked-out back to the pin (HEAD back, onto a commit that lacks them), which
produced the same `CodeBehindSchema` next round. Nothing advances the pin, because
advancing it is a step of a *successful* update. `git reflog`: six alternating resets
in 111 minutes, and each `origin/main` leg's updater died on a `Schema ahead of code`
it had already resolved — because the yank landed mid-flight, so its trailing `ava
start` migrated and verified against a tree the updater had not checked out.

The unit tests beside this one pin each half. This file asserts the property that
only holds when both are in place: **in the incident's state, a controller round
moves nothing.**

Convergence is a pin-aware gateway rollout: it advances the pin and verifies the
fleet. Host-local heals still refuse while this split exists, and the watchdog
exposes the hold through cluster status and events.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

import shared.db
from ops.cluster_status import ClusterStatus
from ops.controllers import pin, schema, schema_mismatch, update_trigger
from ops.controllers.base import BlockScope
from shared.api_contracts.status import MachineStatus
from shared.migrations import CodeBehindSchema

_PIN = "1a90f95d33a145d1df24d17fec0a604f14084b5f"


class _FakeConn:
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


def _relation(value: str):
    """A typed prod_source_pin_relation stand-in (the fixture shas are fake, so
    real git ancestry cannot decide them)."""

    def _r(_pin: str, _head: str) -> str:
        return value

    return _r


@pytest.fixture
def db_ahead_of_the_pin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list]:
    """Prod's 2026-07-31 state: HEAD is the cluster pin, and the DB has migrations
    that commit does not carry. Every spawn path is a spy — anything recorded here is
    a checkout this round would have moved."""
    spawns: dict[str, list[str]] = {"schema": [], "pin": []}

    def _behind(_conn: object) -> None:
        raise CodeBehindSchema(
            "DB has 5 migration(s) this checkout lacks: ['20260731T041500_a', ...]"
        )

    monkeypatch.setattr(schema, "check_schema_version", _behind)
    monkeypatch.setattr(shared.db, "connect", lambda *_a, **_kw: _FakeConn())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(schema, "_schema_heal_attempt_path", lambda: tmp_path / "schema_heal")
    monkeypatch.setattr(
        schema,
        "trigger_update",
        lambda: spawns["schema"].append("gateway") or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        schema,
        "_spawn_update_locally",
        lambda _d: spawns["schema"].append("local") or True,  # pyright: ignore[reportUnknownArgumentType]
    )

    # Both controllers read (pin, HEAD) through the same `read_pin_and_head`, and
    # here HEAD IS the pin. `schema.pin_is_the_blocker` imports it from this module at
    # call time, so one patch covers both controllers.
    monkeypatch.setattr("shared.cluster_drift.running_from_prod_source", lambda: True)
    monkeypatch.setattr(pin, "read_pin_and_head", lambda: (_PIN, _PIN))
    # No lease at all — the incident had none. The pin controller reads the lease
    # object rather than a bare holder string so it can ask whether a settle hold is
    # waiting for this host (issue #1020); `None` means "nothing held" either way.
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    # No recorded update: the recent-update guard passes through (this test's DB
    # is faked, so an unpatched read would defer on an unreadable record).
    monkeypatch.setattr("shared.last_update.read_last_update", lambda: None)
    monkeypatch.setattr(pin, "_pin_heal_attempt_path", lambda: tmp_path / "pin_heal")
    # The fixture's shas are fake, so real git ancestry cannot decide them; the
    # tests below intend a strictly-behind host (the incident's mid-checkout
    # state), which is the heal-eligible relation.
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("behind"))
    monkeypatch.setattr(
        pin,
        "trigger_update",
        lambda target_sha=None: spawns["pin"].append(target_sha) or True,  # pyright: ignore[reportUnknownArgumentType, reportArgumentType]
    )

    update_trigger.reset_cooldown()
    return spawns


def test_a_round_in_the_incident_state_moves_no_checkout(
    db_ahead_of_the_pin: dict[str, list], caplog: pytest.LogCaptureFixture
) -> None:
    """Neither controller spawns, and the round is still reported as blocked with the
    reason named — the escalation the six identical rc=1 rounds never produced."""
    with caplog.at_level(logging.ERROR):
        blocks, detail = schema.schema_reconcile()
    assert blocks is BlockScope.DB_DEPENDENT
    assert detail is not None and _PIN[:7] in detail
    assert pin.check_pin_drift() is False, "on-pin: the pin controller has nothing to do"
    assert db_ahead_of_the_pin == {"schema": [], "pin": []}
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_the_pin_does_not_undo_the_update_the_schema_heal_spawned(
    db_ahead_of_the_pin: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mid-flight half, which is what corrupted every `origin/main` leg. Put the
    host where the schema heal has already moved the checkout forward and its updater
    is still running: the pin controller now sees an off-pin HEAD with no lease (a
    watchdog-spawned updater takes none) and must still stand back."""
    monkeypatch.setattr(pin, "read_pin_and_head", lambda: (_PIN, "9b1343d2"))  # mid-checkout
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")

    assert pin.check_pin_drift() is False
    assert db_ahead_of_the_pin["pin"] == []


def test_an_off_pin_host_with_nothing_running_still_self_heals(
    db_ahead_of_the_pin: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is a deferral, not a disablement: with no update in flight, an
    off-pin runner converges to the pin exactly as before."""
    monkeypatch.setattr(pin, "read_pin_and_head", lambda: (_PIN, "9b1343d2"))

    assert pin.check_pin_drift() is True
    assert db_ahead_of_the_pin["pin"] == [_PIN]


def test_pin_behind_schema_is_visible_on_status_even_when_local_code_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mismatch = schema_mismatch.classify(
        {"baseline", "new"}, {"baseline", "new"}, {"baseline"}, "a" * 40
    )
    assert mismatch is not None and mismatch.kind == "pin-behind-schema"
    monkeypatch.setattr(schema_mismatch, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(schema_mismatch, "detect", lambda: mismatch)
    monkeypatch.setattr(schema_mismatch, "machine_name", lambda: "company-mini")

    assert schema_mismatch.observe("agent-runner", mismatch, ["ava-agent-host"]) == 1
    assert schema_mismatch.observe("agent-runner", mismatch, ["ava-agent-host"]) == 2
    state = schema_mismatch.status()
    assert state is not None
    assert (state.kind, state.machine, state.consecutive_blocked_rounds) == (
        "pin-behind-schema",
        "company-mini",
        2,
    )
    assert state.held_back_services == ["ava-agent-host"]
    assert "ava cluster update" in state.detail

    local = ClusterStatus(
        machine_name="company-mini",
        serve_gateway=False,
        serve_agent_runner=True,
        paused=False,
        schema_mismatch=state,
    )
    assert local.model_dump(mode="json")["schema_mismatch"]["kind"] == "pin-behind-schema"
    roster = MachineStatus(
        name="company-mini",
        serve_gateway=False,
        serve_agent_runner=True,
        gateway_url="http://cm",
        up_since_at=datetime(2026, 9, 24, tzinfo=UTC),
        online=True,
        paused=False,
        schema_mismatch=state,
    )
    assert roster.model_dump(mode="json")["schema_mismatch"]["held_back_services"] == [
        "ava-agent-host"
    ]
    from cli.commands.cluster import _schema_mismatch_banner

    banner = _schema_mismatch_banner([roster])
    assert "company-mini" in banner[0]
    assert "2 consecutive blocked" in banner[0]
    assert "ava-agent-host" in banner[0]


def test_schema_block_streak_resets_only_after_a_healthy_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(schema_mismatch, "ava_home", lambda: tmp_path)
    mismatch = schema_mismatch.classify(
        {"baseline", "new"}, {"baseline", "new"}, {"baseline"}, "a" * 40
    )
    assert mismatch is not None
    assert schema_mismatch.observe("agent-runner", mismatch, ["ava-agent-host"]) == 1
    assert schema_mismatch.observe("agent-runner", mismatch, ["ava-agent-host"]) == 2
    changed = schema_mismatch.classify(
        {"baseline", "new", "newer"}, {"baseline", "new", "newer"}, {"baseline"}, "a" * 40
    )
    assert changed is not None
    assert schema_mismatch.observe("agent-runner", changed, ["ava-agent-host"]) == 3
    monkeypatch.setattr(schema_mismatch, "detect", lambda: changed)
    changed_state = schema_mismatch.status()
    assert changed_state is not None and changed_state.consecutive_blocked_rounds == 3
    schema_mismatch.clear("agent-runner")
    assert schema_mismatch.observe("agent-runner", mismatch, ["ava-agent-host"]) == 1


def test_mismatch_classification_preserves_local_drift_categories() -> None:
    cases: tuple[tuple[set[str], set[str], str], ...] = (
        ({"old"}, {"new"}, "divergent"),
        ({"old"}, set(), "schema-ahead-of-code"),
        (set(), {"new"}, "schema-behind-code"),
    )
    for applied, required, expected in cases:
        mismatch = schema_mismatch.classify(applied, required, None, None)
        assert mismatch is not None and mismatch.kind == expected
