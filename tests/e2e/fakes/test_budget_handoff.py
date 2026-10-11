"""A cross-process reader must never observe an unpublished handoff snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.lm.catalog import ModelCatalog
from base.lm.pricing import cost_usd
from tests.components.base.poll_until import poll_until
from tests.e2e.fakes.scenarios import budget_handoff
from tests.fixtures.model_catalog import model_catalog as model_catalog

_WRITER = """
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
import ava
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity

root = Path(sys.argv[1])
publication = 0

def hold_publication(previous):
    global publication
    publication += 1
    (root / f'previous-{publication}').write_text(previous)
    (root / f'ready-{publication}').touch()
    deadline = time.monotonic() + 10
    while not (root / f'release-{publication}').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Reader did not release publication')
        time.sleep(0.01)

original_open = Path.open
def held_open(path, mode='r', *args, **kwargs):
    is_snapshot = path.name == '1.json' and mode == 'w'
    previous = (root / '1.json').read_text() if is_snapshot else None
    stream = original_open(path, mode, *args, **kwargs)
    if is_snapshot:
        hold_publication(previous)
    return stream

original_fdopen = os.fdopen
def held_fdopen(fd, mode='r', *args, **kwargs):
    previous = (root / '1.json').read_text() if mode == 'w' else None
    stream = original_fdopen(fd, mode, *args, **kwargs)
    if mode == 'w':
        hold_publication(previous)
    return stream

Path.open = held_open
os.fdopen = held_fdopen
ava.context = AvaContext(identity=AgentIdentity(agent_id=1, owns_loop=False))
ava.agents.spawn = lambda **kwargs: 2
ava.agents.send_message = lambda *args, **kwargs: None
exec(sys.argv[2])
"""


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
        budget_handoff._pause_code(1, 1)
        if transition == "pause"
        else budget_handoff._prepare_code(transition, 1)
    )
    process = subprocess.Popen(  # noqa: S603 — fixed interpreter and generated fixture code
        [sys.executable, "-c", _WRITER, str(tmp_path), code],
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
def test_handoff_budget_across_the_daily_price_boundary(
    model_catalog: ModelCatalog, model: str
) -> None:
    """Only a fixed-price scenario guarantees four calls below and six above its limit."""
    before = datetime(2026, 10, 11, 9, 59, 59, tzinfo=UTC)
    after = datetime(2026, 10, 11, 10, 0, 0, tzinfo=UTC)
    limit = cost_usd(model, 50, 25, 0, prices=model_catalog.prices, at=before)
    initial_call = cost_usd(model, 10, 5, 0, prices=model_catalog.prices, at=before)
    later_call = cost_usd(model, 10, 5, 0, prices=model_catalog.prices, at=after)
    assert limit is not None and initial_call is not None and later_call is not None
    assert initial_call * 4 < limit
    if model == budget_handoff.BUDGET_MODEL:
        assert initial_call == later_call
        assert later_call * 6 >= limit
    else:
        assert later_call * 6 < limit  # The original CI case can never trigger its reminder.
