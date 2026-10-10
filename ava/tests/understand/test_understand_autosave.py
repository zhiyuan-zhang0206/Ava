"""ava.understand auto-save to .exec_output; split from ava/tests/understand/test_understand.py (task #4922)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ava.tests.understand.provider_support import ProviderCapture
from ava.tests.understand.provider_support import mock_deepseek as mock_deepseek
from ava.tests.understand.provider_support import understand_clock as understand_clock
from ava.understand import understand
from base.clock import Clock
from tests.fixtures.pin_agent import pin_agent, pin_no_identity

pytestmark = pytest.mark.usefixtures("sdk_model_owner", "understand_clock")

# ── auto-save output ────────────────────────────────────────────────────────


def test_single_result_saved_to_exec_output(
    mock_deepseek: ProviderCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-call understand saves result to .exec_output/ in workspace."""

    # Use a temp workspace dir so we can inspect it
    agent_id = 2139
    ws = tmp_path / "fake_ws"
    ws.mkdir(parents=True)

    # Monkeypatch workspace_dir and agent_id
    pin_agent(agent_id, clock_factory=Clock.from_settings)

    def _fake_workspace(aid: int) -> Path:
        return ws

    monkeypatch.setattr("base.paths.workspace_dir", _fake_workspace)

    understand([{"prompt": "summarize please", "text": "hello world"}])

    # Check that .exec_output/ was created and contains the result
    exec_dir = ws / ".exec_output"
    assert exec_dir.is_dir()
    files = list(exec_dir.glob("understand_*.txt"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "fake answer" in content
    assert "summarize please" in content


def test_batch_results_saved_to_exec_output(
    mock_deepseek: ProviderCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch understand saves each result individually."""

    agent_id = 2139
    ws = tmp_path / "batch_ws"
    ws.mkdir(parents=True)
    pin_agent(agent_id, clock_factory=Clock.from_settings)

    def _fake_workspace(aid: int) -> Path:
        return ws

    monkeypatch.setattr("base.paths.workspace_dir", _fake_workspace)

    targets = [
        {"prompt": "question one", "text": "material one"},
        {"prompt": "question two", "text": "material two"},
    ]
    understand(targets)

    exec_dir = ws / ".exec_output"
    files = sorted(exec_dir.glob("understand_*.txt"))
    assert len(files) == 2
    assert "question one" in files[0].read_text()
    assert "question two" in files[1].read_text()


def test_auto_save_prunes_old_files(
    mock_deepseek: ProviderCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old understand output files are pruned, keeping the 20 most recent results."""

    agent_id = 2139
    ws = tmp_path / "prune_ws"
    ws.mkdir(parents=True)
    pin_agent(agent_id, clock_factory=Clock.from_settings)

    def _fake_workspace(aid: int) -> Path:
        return ws

    monkeypatch.setattr("base.paths.workspace_dir", _fake_workspace)

    # Pre-create many old files, then force every mtime to the same value.
    # This is the condition the pruner has to be right under: files written in
    # quick succession land on one filesystem timestamp, so mtime cannot order
    # them and the surviving set would come down to glob order. The name's
    # fixed-width `%Y%m%d_%H%M%S_%f` is what actually carries the order.
    exec_dir = ws / ".exec_output"
    exec_dir.mkdir(parents=True)
    for i in range(30):
        f = exec_dir / f"understand_20240101_000000_{i:06d}_old{i}.txt"
        f.write_text(f"old result {i}")
    for f in exec_dir.iterdir():
        os.utime(f, (1_700_000_000, 1_700_000_000))

    # Run a new understand — should trigger pruning
    understand([{"prompt": "new question", "text": "new material"}])

    files = sorted(exec_dir.glob("understand_*.txt"))
    assert len(files) == 20, "the ring retains the newest 20 results"

    # Exactly which files survive is determined, not incidental. Sorted by name
    # the 30 pre-created ones precede today's, so the ring is the 19 highest
    # microsecond fields plus the file just written; old0..old10 are pruned.
    names = [f.name for f in files]
    assert [n for n in names if "_old" in n] == [
        f"understand_20240101_000000_{i:06d}_old{i}.txt" for i in range(11, 30)
    ]
    assert sum(1 for n in names if "_old" not in n) == 1, "the newly written file survives"


def test_auto_save_noop_without_agent_id(
    mock_deepseek: ProviderCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no agent identity is established, auto-save is skipped gracefully."""

    # Set agent_id to None — simulate non-agent process
    pin_no_identity()
    # Also ensure env var doesn't re-establish
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)

    # Should not raise
    [result] = understand([{"prompt": "test", "text": "some text"}])
    assert result == "fake answer"


def test_one_question_is_a_one_element_batch(mock_deepseek: ProviderCapture) -> None:
    """The single-question ergonomic: a one-element list, unpacked."""
    [result] = understand([{"prompt": "hello", "text": "world"}])
    assert result == "fake answer"
