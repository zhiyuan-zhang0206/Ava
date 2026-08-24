"""`cmd_rollback` cluster-wide sequencing and incomplete-settle behaviour."""

from __future__ import annotations

import pytest

from cli.commands import _cluster_rollback as _rb
from cli.commands._update_phase_b import POLL_OK, POLL_STALLED, PollVerdict

_SNAP = {"00000000T000000_baseline"}


@pytest.fixture
def _seams(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[dict[str, object]]]:
    """Wire rollback's cross-host seams and record their observable order."""
    order: list[str] = []
    phase_b_calls: list[dict[str, object]] = []

    def _stop_the_world(
        _runners: list[tuple[str, str | None]], *, mode: str = "smooth"
    ) -> tuple[set[str], bool]:
        assert mode == "smooth"
        order.append("stop-the-world")
        return {"runner-a"}, True

    def _set_cluster_target(_sha: str, *, set_by: str | None = None) -> None:
        assert set_by is not None
        order.append("pin-write")

    def _phase_b_and_poll(
        targets: list[tuple[str, str | None]],
        *,
        target_sha: str | None,
        restart_only: bool,
        force_reap: bool = False,
        mode: str = "smooth",
    ) -> dict[str, PollVerdict]:
        phase_b_calls.append(
            {
                "targets": targets,
                "target_sha": target_sha,
                "restart_only": restart_only,
                "force_reap": force_reap,
                "mode": mode,
            }
        )
        order.append("fan-out")
        return {"runner-a": PollVerdict(POLL_OK)}

    def _notify_owner(_text: str) -> None:
        order.append("owner-notify")

    def _set_last_known_good(_sha: str, *, set_by: str | None = None) -> None:
        assert set_by is not None
        order.append("set-known-good")

    monkeypatch.setattr(_rb, "_resolve_rollback_target", lambda _to: "TARGETSHA")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_validate_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "acquire_update_lock", lambda _h, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "release_update_lock", lambda _h: order.append("release-lock"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "settle_update_lock", lambda _h, **_kw: order.append("settle-lock"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "git_head_sha", lambda: "FROMSHA")
    monkeypatch.setattr(_rb, "current_schema_state", lambda: _SNAP)
    monkeypatch.setattr(_rb, "_list_agent_runners", lambda: [("runner-a", "http://runner-a")])
    monkeypatch.setattr(_rb, "_stop_the_world", _stop_the_world)
    monkeypatch.setattr(_rb, "_run_rollback", lambda *_a, **_k: order.append("local-rollback") or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "set_cluster_target_sha", _set_cluster_target)
    monkeypatch.setattr(_rb, "clear_pending_known_good", lambda: order.append("clear-pending"))
    monkeypatch.setattr(_rb, "_phase_b_targets", lambda runners: runners)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_phase_b_and_poll", _phase_b_and_poll)
    monkeypatch.setattr(_rb, "_still_converging", lambda _polls: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_fan_out", lambda *_a, **_kw: order.append("resume"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_notify_agents_of_rollback", lambda _f, _t: order.append("notify"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_notify_owner", _notify_owner)
    monkeypatch.setattr(_rb, "_note_rollback_on_last_update", lambda _f, _t: order.append("note"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "set_last_known_good_sha", _set_last_known_good)
    monkeypatch.setattr(_rb, "machine_name", lambda: "test-host")
    monkeypatch.setattr("cli.commands._repo._repo_root", lambda: _rb.Path("."))
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: order.append("unpause-local"))
    return order, phase_b_calls


def test_rollback_orders_stop_local_pin_fanout_then_notify(
    _seams: tuple[list[str], list[dict[str, object]]],
) -> None:
    """The old pin is written before Phase B, so missed runners self-heal to it."""
    order, phase_b_calls = _seams

    assert _rb.cmd_rollback(require_confirmation=False) == 0

    assert order.index("stop-the-world") < order.index("local-rollback")
    assert order.index("local-rollback") < order.index("pin-write")
    assert order.index("pin-write") < order.index("fan-out")
    assert order.index("fan-out") < order.index("notify")
    assert phase_b_calls == [
        {
            "targets": [("runner-a", "http://runner-a")],
            "target_sha": "TARGETSHA",
            "restart_only": False,
            "force_reap": False,
            "mode": "none",
        }
    ]


def test_rollback_force_reaps_runners_when_agents_do_not_quiesce(
    _seams: tuple[list[str], list[dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiesce timeout must force-reap stragglers during runner rollback."""
    _order, phase_b_calls = _seams

    def _stop_the_world(
        _runners: list[tuple[str, str | None]], *, mode: str = "smooth"
    ) -> tuple[set[str], bool]:
        assert mode == "smooth"
        return {"runner-a"}, False

    monkeypatch.setattr(_rb, "_stop_the_world", _stop_the_world)

    assert _rb.cmd_rollback(require_confirmation=False) == 0

    assert phase_b_calls[0]["force_reap"] is True


def test_rollback_writes_target_pin_after_a_successful_local_leg(
    _seams: tuple[list[str], list[dict[str, object]]],
) -> None:
    """Cluster pin write-back makes watchdog reconciliation converge missed runners."""
    order, _phase_b_calls = _seams

    assert _rb.cmd_rollback(require_confirmation=False) == 0

    assert order.count("pin-write") == 1
    assert order.index("clear-pending") > order.index("local-rollback")


def test_keep_pin_skips_pin_write_and_runner_fanout(
    _seams: tuple[list[str], list[dict[str, object]]],
) -> None:
    """Gateway-only rollback preserves the current runner target and pin."""
    order, phase_b_calls = _seams

    assert _rb.cmd_rollback(require_confirmation=False, keep_pin=True) == 0

    assert "pin-write" not in order
    assert "fan-out" not in order
    assert phase_b_calls == []


def test_incomplete_runner_rollback_holds_lease_and_resumes(
    _seams: tuple[list[str], list[dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acked runner that does not return gets a settle hold and a resume attempt."""
    order, _phase_b_calls = _seams
    owner_messages: list[str] = []

    def _stalled_phase_b(
        _targets: list[tuple[str, str | None]],
        *,
        target_sha: str | None,
        restart_only: bool,
        force_reap: bool = False,
        mode: str = "smooth",
    ) -> dict[str, PollVerdict]:
        assert target_sha == "TARGETSHA"
        assert restart_only is False
        assert force_reap is False
        assert mode == "none"
        return {"runner-a": PollVerdict(POLL_STALLED)}

    monkeypatch.setattr(_rb, "_phase_b_and_poll", _stalled_phase_b)
    monkeypatch.setattr(_rb, "_still_converging", lambda _polls: ["runner-a"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rb, "_notify_owner", owner_messages.append)

    assert _rb.cmd_rollback(require_confirmation=False) == 1

    assert "settle-lock" in order
    assert "release-lock" not in order
    assert "resume" in order
    assert len(owner_messages) == 1
    assert owner_messages[0].startswith(
        "[cluster-rollback] cluster rolled back FROMSHA -> TARGETS (trigger: auto)"
    )
    assert "still converging" in owner_messages[0]
    assert "runner-a" in owner_messages[0]


def test_already_at_target_is_a_successful_noop(
    _seams: tuple[list[str], list[dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated auto-rollback does not loop when the target is already checked out."""
    _order, _phase_b_calls = _seams

    def _already_at_target(_sha: str) -> None:
        raise ValueError("target commit TARGETS is the current HEAD -- nothing to roll back to")

    monkeypatch.setattr(_rb, "_validate_rollout_target", _already_at_target)

    assert _rb.cmd_rollback(require_confirmation=False) == 0


def test_set_known_good_still_sets_lkg_and_clears_pending(
    _seams: tuple[list[str], list[dict[str, object]]],
) -> None:
    """Manual LKG override remains available after the observation-window change."""
    order, _phase_b_calls = _seams

    assert _rb.cmd_rollback(require_confirmation=False, set_known_good=True) == 0

    assert "set-known-good" in order
    assert "clear-pending" in order


def test_rollback_parser_accepts_keep_pin() -> None:
    """The gateway-only escape hatch reaches the command rather than argparse's error path."""
    from cli.main import _build_parser

    args = _build_parser().parse_args(["cluster", "rollback", "--keep-pin", "--yes"])

    assert args.keep_pin is True
