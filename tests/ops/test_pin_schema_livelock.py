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

Real convergence still needs a human — advance the pin, or roll the schema back — and
that is the decision, not a gap: auto-advancing the pin would let any DB drift move
the cluster onto unreviewed code. What changes is that the cluster now waits loudly
in one place instead of flapping its checkout between two commits.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import shared.db
from ops.controllers import pin, schema, update_trigger
from ops.controllers.base import BlockScope
from shared.migrations import CodeBehindSchema

_PIN = "1a90f95d33a145d1df24d17fec0a604f14084b5f"


class _FakeConn:
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


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
