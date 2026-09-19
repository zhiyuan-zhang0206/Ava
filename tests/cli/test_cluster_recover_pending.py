"""`ava cluster recover-pending` — the operator surface over the recovery seat.

The seat itself is driven against the real journal in
`tests/ops/test_publication_recovery.py`; here the CLI contract is pinned: exit
codes, what lands on stdout versus stderr, and that the verb is a thin trigger —
it reports the stranded publication it found, then calls the op, and mutates
nothing itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import ops.publication_recovery as _pr
from cli.commands import _cluster_recover_pending as _entry
from ops.cluster import ClusterUpdateInProgress
from ops.publication_recovery import PendingRecoveryState
from shared.managed_writer_barrier import RolloutIdentity


def _abandoned() -> RolloutIdentity:
    return RolloutIdentity(
        holder="gateway:pid123",
        acquired_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        target_sha="e" * 40,
    )


def _state(*, pending: bool) -> PendingRecoveryState:
    abandoned = _abandoned() if pending else None
    return PendingRecoveryState(
        pending=pending,
        abandoned=abandoned,
        abandoned_operation_json=abandoned.model_dump(mode="json") if abandoned else None,
        abandoned_units=(),
        candidate_digest="f" * 64 if pending else None,
        current_publication_id=None,
        lease=None,
        stale_holder=None,
        stale_holder_probed_gone=None,
        stale_holder_held_for_s=None,
    )


def test_nothing_pending_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_pr, "read_pending_recovery_state", lambda: _state(pending=False))

    assert _entry.cmd_cluster_recover_pending() == 0

    captured = capsys.readouterr()
    assert "no pending publication is journaled" in captured.out
    assert captured.err == ""


def test_a_refused_seat_exits_nonzero_and_names_the_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = "pending-publication recovery requires a fresh complete writer closure"
    monkeypatch.setattr(_pr, "read_pending_recovery_state", lambda: _state(pending=True))

    def _refuse() -> dict[str, object]:
        raise ClusterUpdateInProgress(refusal)

    monkeypatch.setattr(_pr, "pending_publication_recovery_op", _refuse)

    assert _entry.cmd_cluster_recover_pending() == 1

    captured = capsys.readouterr()
    assert "✗" in captured.err and refusal in captured.err
    # The operator sees what is stranded before the verdict.
    assert "pending managed-writer publication from an interrupted rollout" in captured.out
    assert "gateway:pid123" in captured.out
    assert "eeeeeee" in captured.out  # the 7-char target prefix


def test_an_unreadable_journal_refuses_before_reaching_the_op(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    op_calls: list[bool] = []

    def _unreadable() -> PendingRecoveryState:
        raise ClusterUpdateInProgress(
            "managed-writer publication evidence is unreadable (fail-closed); recovery refused"
        )

    monkeypatch.setattr(_pr, "read_pending_recovery_state", _unreadable)
    monkeypatch.setattr(_pr, "pending_publication_recovery_op", lambda: op_calls.append(True))

    assert _entry.cmd_cluster_recover_pending() == 1

    captured = capsys.readouterr()
    assert "unreadable" in captured.err
    assert op_calls == []


def test_a_completed_recovery_reports_the_new_holder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The success branch the closure producer will drive once it is connected."""
    monkeypatch.setattr(_pr, "read_pending_recovery_state", lambda: _state(pending=True))
    monkeypatch.setattr(
        _pr,
        "pending_publication_recovery_op",
        lambda: {
            "recovered": True,
            "abandoned_holder": "gateway:pid123",
            "new_holder": "macmini:pid7",
            "challenge": "c" * 36,
            "units": 1,
        },
    )

    assert _entry.cmd_cluster_recover_pending() == 0

    captured = capsys.readouterr()
    assert "macmini:pid7" in captured.out
    assert "agent births stay frozen" in captured.out
