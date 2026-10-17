from __future__ import annotations

import os
from pathlib import Path

import pytest

from meiva.tests.fixtures.pin_agent import pin_no_identity
from meiva.tests.namespaces.understand.provider_support import ProviderCapture
from meiva.tests.namespaces.understand.provider_support import mock_deepseek as mock_deepseek
from meiva.tests.namespaces.understand.provider_support import understand_deps as understand_deps
from meiva.understand.answer import UnderstandDeps

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


def test_single_result_saved_to_exec_output(mock_deepseek: ProviderCapture, tmp_path: Path) -> None:

    ws = tmp_path / "fake_ws"
    mock_deepseek.configure(output_dir=lambda: ws / ".exec_output")

    mock_deepseek.understand([{"prompt": "summarize please", "text": "hello world"}])

    exec_dir = ws / ".exec_output"
    assert exec_dir.is_dir()
    files = list(exec_dir.glob("understand_*.txt"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "fake answer" in content
    assert "summarize please" in content


def test_batch_results_saved_to_exec_output(mock_deepseek: ProviderCapture, tmp_path: Path) -> None:

    ws = tmp_path / "batch_ws"
    mock_deepseek.configure(output_dir=lambda: ws / ".exec_output")

    targets = [
        {"prompt": "question one", "text": "material one"},
        {"prompt": "question two", "text": "material two"},
    ]
    mock_deepseek.understand(targets)

    exec_dir = ws / ".exec_output"
    files = sorted(exec_dir.glob("understand_*.txt"))
    assert len(files) == 2
    assert "question one" in files[0].read_text()
    assert "question two" in files[1].read_text()


def test_auto_save_prunes_old_files(mock_deepseek: ProviderCapture, tmp_path: Path) -> None:

    ws = tmp_path / "prune_ws"
    mock_deepseek.configure(output_dir=lambda: ws / ".exec_output")

    exec_dir = ws / ".exec_output"
    exec_dir.mkdir(parents=True)
    for i in range(30):
        f = exec_dir / f"understand_20240101_000000_{i:06d}_old{i}.txt"
        f.write_text(f"old result {i}")
    for f in exec_dir.iterdir():
        os.utime(f, (1_700_000_000, 1_700_000_000))

    mock_deepseek.understand([{"prompt": "new question", "text": "new material"}])

    files = sorted(exec_dir.glob("understand_*.txt"))
    assert len(files) == 20, "the ring retains the newest 20 results"

    names = [f.name for f in files]
    assert [n for n in names if "_old" in n] == [
        f"understand_20240101_000000_{i:06d}_old{i}.txt" for i in range(11, 30)
    ]
    assert sum(1 for n in names if "_old" not in n) == 1, "the newly written file survives"


def test_auto_save_noop_without_agent_id(
    mock_deepseek: ProviderCapture, understand_deps: UnderstandDeps, monkeypatch: pytest.MonkeyPatch
) -> None:

    pin_no_identity()
    monkeypatch.delenv("MEIVA_AGENT_ID", raising=False)

    assert understand_deps.output_dir() is None
    mock_deepseek.configure(output_dir=understand_deps.output_dir)

    [result] = mock_deepseek.understand([{"prompt": "test", "text": "some text"}])
    assert result == "fake answer"


def test_one_question_is_a_one_element_batch(mock_deepseek: ProviderCapture) -> None:
    [result] = mock_deepseek.understand([{"prompt": "hello", "text": "world"}])
    assert result == "fake answer"
