"""A cross-process reader must never observe an unpublished handoff snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.lm.pricing import cost_usd
from base.packages.plugin_providers import build_model_catalog
from tests.components.base.poll_until import poll_until
from tests.e2e.fakes.scenarios import budget_handoff

_WRITER = Path(__file__).with_name("child_budget_writer.py")


@pytest.mark.parametrize(
    ("transition", "writes"),
    [("goal supervisor", 2), ("dynamic workflow orchestrator", 3), ("pause", 1)],
)
def test_readers_keep_the_previous_snapshot_until_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str, writes: int
) -> None:
    def scenario_root(_kind: str) -> Path:
        return tmp_path

    monkeypatch.setattr(budget_handoff, "scratch_root", scenario_root)
    (tmp_path / "owner").write_text("1")
    initial = {
        "status": "running",
        "goal_met": False,
        "artifacts": [],
        "remaining": ["unfinished unit"],
        "peers": [],
    }
    state = tmp_path / "1.json"
    state.write_text(json.dumps(initial))
    code = (
        budget_handoff.pause_code(1, 1)
        if transition == "pause"
        else budget_handoff.prepare_code(transition, 1)
    )
    process = subprocess.Popen(  # noqa: S603 — fixed interpreter and generated fixture code
        [sys.executable, str(_WRITER), str(tmp_path), code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for index in range(1, writes + 1):
            poll_until(
                lambda index=index: ((tmp_path / f"ready-{index}").exists(), process.poll()),
                timeout=10,
                interval=0.01,
                what="writer opens its unpublished snapshot",
            )
            assert json.loads(state.read_text()) == json.loads(
                (tmp_path / f"previous-{index}").read_text()
            )
            (tmp_path / f"release-{index}").touch()
    finally:
        for index in range(1, writes + 1):
            (tmp_path / f"release-{index}").touch()
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise
    assert process.returncode == 0, stdout + stderr
    saved = json.loads(state.read_text())
    assert saved["status"] == ("paused" if transition == "pause" else "running")
    assert saved["peers"] == ([] if transition == "pause" else [2])


@pytest.mark.parametrize("model", ["deepseek-flash", budget_handoff.BUDGET_MODEL])
def test_handoff_budget_across_the_daily_price_boundary(model: str) -> None:
    """Only a fixed-price scenario guarantees four calls below and six above its limit."""
    prices = build_model_catalog().prices
    before = datetime(2026, 10, 11, 9, 59, 59, tzinfo=UTC)
    after = datetime(2026, 10, 11, 10, 0, 0, tzinfo=UTC)
    limit = cost_usd(model, 50, 25, 0, prices=prices, at=before)
    initial_call = cost_usd(model, 10, 5, 0, prices=prices, at=before)
    later_call = cost_usd(model, 10, 5, 0, prices=prices, at=after)
    assert limit is not None and initial_call is not None and later_call is not None
    assert initial_call * 4 < limit
    if model == budget_handoff.BUDGET_MODEL:
        assert initial_call == later_call
        assert later_call * 6 >= limit
    else:
        assert later_call * 6 < limit  # The original CI case can never trigger its reminder.
