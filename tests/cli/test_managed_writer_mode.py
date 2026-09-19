"""The managed-writer enable point: one mode decision per rollout (task #4121).

Covers the gate's truth table (off / active / blocked), the fail-closed guard
semantics (absent / not-exactly-True), the read-once cache, the recorded
evidence (rollout log line + telemetry field + `managed_writer_blocked` event),
the read point inside the orchestration (a blocked decision still runs the
legacy flow), the `ava cluster status` bit, and the single-read static pin.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cli.commands import _managed_writer_mode as mode_mod
from shared import rollout_telemetry

_CHECKED = "cli.commands._update_normal_release.CHECKED_ACTIVATION_READY"
_WIRING = "cli.commands._update_publication.MANAGED_WRITER_WIRING_COMPLETE"


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """The decision is cached per process; each test starts from no decision."""
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()
    yield
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mode_mod, "_config_enabled", lambda: True)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mode_mod, "_config_enabled", lambda: False)


def _set_guard(monkeypatch: pytest.MonkeyPatch, target: str, value: object = True) -> None:
    monkeypatch.setattr(target, value, raising=False)


def _clear_guard(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.delattr(target, raising=False)


def _ready_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr("shared.audit_events.insert_event_log", _capture)
    return events


# ── truth table ──────────────────────────────────────────────────────────────


def test_off_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    assert mode_mod.effective_managed_writer_mode() == mode_mod.ManagedWriterMode("off")


def test_active_when_enabled_and_both_guards_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_guards(monkeypatch)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("active", ())


def test_checked_activation_absent_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("blocked", ("checked_activation_not_ready",))


@pytest.mark.parametrize("value", [False, 1, "True", None])
def test_guard_requires_exactly_true(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED, value)
    _set_guard(monkeypatch, _WIRING)
    assert mode_mod.effective_managed_writer_mode().blocked_reasons == (
        "checked_activation_not_ready",
    )


def test_wiring_incomplete_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("blocked", ("wiring_incomplete",))


def test_both_guards_missing_join_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    assert mode_mod.effective_managed_writer_mode().blocked_reasons == (
        "checked_activation_not_ready",
        "wiring_incomplete",
    )


def test_guard_reader_treats_import_error_as_not_ready() -> None:
    assert mode_mod._guard_ready("no.such.module_anywhere", "X") is False


# ── read-once semantics ──────────────────────────────────────────────────────


def test_decide_reads_the_switch_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[bool] = []

    def _counting() -> bool:
        reads.append(True)
        return False

    monkeypatch.setattr(mode_mod, "_config_enabled", _counting)
    first = mode_mod.decide_managed_writer_mode()
    second = mode_mod.decide_managed_writer_mode()
    assert first == second == mode_mod.ManagedWriterMode("off")
    assert len(reads) == 1

    mode_mod._reset_for_tests()
    mode_mod.decide_managed_writer_mode()
    assert len(reads) == 2


def test_accessor_is_none_before_the_read_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    assert mode_mod.managed_writer_mode() is None
    assert mode_mod.decide_managed_writer_mode().state == "off"
    assert mode_mod.managed_writer_mode() == mode_mod.ManagedWriterMode("off")


# ── recorded evidence ────────────────────────────────────────────────────────


def test_blocked_decision_emits_one_event_with_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _capture_events(monkeypatch)
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    mode_mod.decide_managed_writer_mode()
    mode_mod.decide_managed_writer_mode()  # re-entry must not emit a second event
    assert events == [
        {
            "event_type": "managed_writer_blocked",
            "agent_id": None,
            "source": "system",
            "payload": {"reason": "wiring_incomplete"},
        }
    ]
    out = capsys.readouterr().out
    assert "managed-writer mode: blocked \u2014 wiring incomplete; running the legacy flow" in out


def test_off_decision_records_no_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture_events(monkeypatch)
    _disable(monkeypatch)
    assert mode_mod.decide_managed_writer_mode().state == "off"
    assert events == []


def test_active_decision_records_no_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture_events(monkeypatch)
    _ready_guards(monkeypatch)
    assert mode_mod.decide_managed_writer_mode().state == "active"
    assert events == []


def test_blocked_decision_lands_in_the_rollout_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    collector = rollout_telemetry.activate()
    mode_mod.decide_managed_writer_mode()
    assert collector.summary()["managed_writer"] == {
        "state": "blocked",
        "reasons": ["wiring_incomplete"],
    }


def test_off_decision_lands_in_the_rollout_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    collector = rollout_telemetry.activate()
    mode_mod.decide_managed_writer_mode()
    assert collector.summary()["managed_writer"] == {"state": "off", "reasons": []}


def test_no_collector_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    mode_mod.decide_managed_writer_mode()  # must not raise without a collector


def test_blocked_event_is_a_registered_audit_name() -> None:
    from shared.events.contract import EVENTS

    assert EVENTS["managed_writer_blocked"].category == "audit"


# ── the read point inside the orchestration ──────────────────────────────────


def _stub_orchestration(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Reach the end of a real (non-dry-run) rollout without a live cluster."""
    from cli import commands as _cli
    from cli.commands import update as _up

    stopped: list[str] = []
    monkeypatch.setattr(_up, "_rollout_preflight", lambda _repo, **_kw: (None, False, "target-sha"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_begin_update_record", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_resolve_fanout_targets", lambda **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_run_preflight_fetch", lambda *_a, **_k: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_a, **_k: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 130.0)
    monkeypatch.setattr(_up, "_snapshot_known_good", lambda **_kw: ("old", set[str](), None))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_a, **_k: stopped.append("stop") or (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    return stopped


def _run_inner() -> int:
    from cli.commands import update as _up

    return _up._run_gateway_orchestration_inner(
        Path("/unused"),
        origin="test-origin",
        deploy_capability={
            "deploy_holder": "test",
            "deploy_acquired_at": "2026-08-25T00:00:00+00:00",
        },
    )


def test_blocked_still_runs_the_legacy_flow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switch on + guard missing -> the legacy flow runs unchanged, blocked marked."""
    events = _capture_events(monkeypatch)
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    stopped = _stub_orchestration(monkeypatch)

    assert _run_inner() == 0
    assert stopped == ["stop"]
    decided = mode_mod.managed_writer_mode()
    assert decided is not None and decided.state == "blocked"
    assert [event["event_type"] for event in events] == ["managed_writer_blocked"]
    assert "managed-writer mode: blocked" in capsys.readouterr().out


def test_decision_precedes_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    _stub_orchestration(monkeypatch)
    from cli.commands import update as _up

    original = _up._build_prepare_gate
    observed: list[object] = []

    def _probe(*args: Any, **kwargs: Any) -> Any:
        observed.append(mode_mod.managed_writer_mode())
        return original(*args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_up, "_build_prepare_gate", _probe)
    assert _run_inner() == 0
    assert observed == [mode_mod.ManagedWriterMode("off")]


def test_active_rollout_runs_the_flow_and_keeps_the_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_guards(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)
    assert _run_inner() == 0
    assert stopped == ["stop"]
    assert mode_mod.managed_writer_mode() == mode_mod.ManagedWriterMode("active")


def test_off_rollout_records_no_event_and_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _capture_events(monkeypatch)
    _disable(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)
    assert _run_inner() == 0
    assert stopped == ["stop"]
    assert events == []
    assert "managed-writer mode: off" in capsys.readouterr().out


# ── the status bit ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | list[Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("fake error response")

    def json(self) -> dict[str, Any] | list[Any]:
        return self._payload


def _patch_roster(monkeypatch: pytest.MonkeyPatch, roster: list[dict[str, Any]]) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _fake_get(url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(roster)

    monkeypatch.setattr("httpx.get", _fake_get)


def _machine_row() -> dict[str, Any]:
    from datetime import UTC, datetime

    from gateway.schemas import MachineStatus

    base = MachineStatus(
        name="test-host",
        serve_gateway=True,
        serve_agent_runner=True,
        gateway_url="http://gw:8000",
        up_since_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
        online=True,
        paused=False,
    )
    return base.model_dump(mode="json")  # pyright: ignore[reportUnknownMemberType]


def test_status_off_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _cli

    _disable(monkeypatch)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    assert "managed-writer" not in capsys.readouterr().out


def test_status_shows_effective_blocked_not_just_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switch on + guard missing renders blocked -- never a plain 'on'."""
    from cli import commands as _cli

    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    out = capsys.readouterr().out
    assert (
        "managed-writer: blocked \u2014 checked activation not ready; running the legacy flow"
        in out
    )


def test_status_shows_active(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _cli

    _ready_guards(monkeypatch)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    assert "managed-writer: active" in capsys.readouterr().out


# ── the single-read static pin ───────────────────────────────────────────────


_SOURCE_ROOTS = ("cli", "shared", "agent", "gateway", "ops", "ava", "ava_builtins", "services")
_CANONICAL_READERS = {
    "cli/commands/_managed_writer_mode.py",
    "shared/config/gateway.py",
}


def test_switch_is_read_only_in_the_gate_module() -> None:
    """Exactly one production read site; any other reader is a read-point bypass."""
    root = Path(__file__).resolve().parents[2]
    offenders: dict[str, list[int]] = {}
    for source_root in _SOURCE_ROOTS:
        for path in sorted((root / source_root).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in _CANONICAL_READERS:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if "update_managed_writer" in line:
                    offenders.setdefault(rel, []).append(lineno)
    assert offenders == {}, (
        f"update_managed_writer is read outside the enable point: {offenders} -- "
        "consumers take the recorded decision, never the switch."
    )
