"""validate-before-kill: the gateway orchestration vets the rollout target's
migrations/ layout BEFORE Phase A pauses anything.

A target whose migrations/ has a duplicate / non-contiguous version only fails the
migrate step at boot — after the stop has taken the host down (the 2026-06-17
dup-0049 outage). `_vet_rollout_target` refuses such a target with every host still
serving. These tests cover the helper itself and its wiring into the orchestration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import commands as _cli
from cli.commands import _update_git as _ug
from cli.commands import update as _up
from shared.migrations import MigrationLayoutError


def _raise_layout(*_a, **_k):
    raise MigrationLayoutError("duplicate migration name: '20260719T143000_add-foo'")


# ─── the helper itself ────────────────────────────────────────────────────────


def test_vet_rollout_target_refuses_broken_layout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", _raise_layout)  # pyright: ignore[reportUnknownArgumentType]
    assert _ug._vet_rollout_target("deadbeefcafe") == 1
    assert "broken migration layout" in capsys.readouterr().err


def test_vet_rollout_target_passes_valid_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    assert _ug._vet_rollout_target("deadbeefcafe") is None


# ─── wiring into the orchestration (refuse before any pause) ──────────────────


def _stub_full_orchestration(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    """Backend change → full orchestration; pin a fake target so no real git runs;
    record every kill/pause step so the test can assert ordering."""
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_up, "_resolve_rollout_target", lambda **_kw: "deadbeefcafe")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: order.append("pause_local"))
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: order.append("quiesce") or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda *_a, **_k: order.append("local_update") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )


def test_orchestration_refuses_broken_target_before_any_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    _stub_full_orchestration(monkeypatch, order)
    monkeypatch.setattr(
        _up,
        "_vet_rollout_target",
        lambda _sha: 1,  # pyright: ignore[reportUnknownArgumentType]
    )  # broken target  # pyright: ignore[reportUnknownArgumentType]
    # a fan-out running would mean we paused remote hosts before refusing
    monkeypatch.setattr(_cli, "_fan_out", lambda *_a, **_k: order.append("fan_out") or [])  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")

    assert rc == 1
    assert order == [], f"refusal must precede any pause/stop/quiesce; got {order}"


def test_orchestration_proceeds_when_target_layout_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vet is a gate, not a hard stop: a valid target runs the orchestration
    normally (single-host path: pause → quiesce → local update)."""
    order: list[str] = []
    _stub_full_orchestration(monkeypatch, order)
    monkeypatch.setattr(
        _up,
        "_vet_rollout_target",
        lambda _sha: None,  # pyright: ignore[reportUnknownArgumentType]
    )  # valid  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")

    assert rc == 0
    assert order == ["pause_local", "quiesce", "local_update"]
