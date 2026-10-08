"""ava.understand batch (concurrent) mode: validation, order preservation, concurrency caps, and error propagation; split from ava/tests/understand/test_understand.py (task #4922)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.tests.understand._understand_helpers import mock_deepseek as mock_deepseek
from ava.tests.understand._understand_helpers import understand_mod

# ── batch (concurrent) mode ─────────────────────────────────────────────────


def test_batch_targets_validation() -> None:
    """targets must be a list of dicts with prompt + exactly one of paths/text."""
    # targets not a list
    with pytest.raises(TypeError, match="takes a list of target dicts"):
        understand_mod.understand("not a list")  # type: ignore[arg-type]

    # target not a dict
    with pytest.raises(TypeError, match="must be a dict"):
        understand_mod.understand(["not a dict"])  # type: ignore[list-item]

    # missing prompt
    with pytest.raises(ValueError, match="missing required key 'prompt'"):
        understand_mod.understand([{"paths": ["x.txt"]}])

    # both paths and text
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand_mod.understand([{"prompt": "p", "paths": ["x.txt"], "text": "y"}])

    # neither paths nor text
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand_mod.understand([{"prompt": "p"}])


def test_batch_text_concurrent(mock_deepseek: dict[str, Any]) -> None:
    """Multiple text prompts run concurrently — all complete, order preserved."""

    # Make each invoke return a unique answer so we can verify order
    def _tracking_invoke(messages: Any):
        # The prompt is in the second text block
        prompt_text = messages[0].content[1]["text"]
        response = MagicMock()
        response.content = f"answer to: {prompt_text}"
        response.response_metadata = {}
        return response

    mock_deepseek["llm"].invoke.side_effect = _tracking_invoke

    targets = [
        {"prompt": "first question", "text": "material A"},
        {"prompt": "second question", "text": "material B"},
        {"prompt": "third question", "text": "material C"},
    ]
    results = understand_mod.understand(targets)
    assert isinstance(results, list)
    assert len(results) == 3  # pyright: ignore[reportUnknownArgumentType] — dynamic module members
    assert results[0] == "answer to: first question"
    assert results[1] == "answer to: second question"
    assert results[2] == "answer to: third question"


def test_batch_mixed_paths_and_text(mock_deepseek: dict[str, Any], tmp_path: Path) -> None:
    """Batch targets can mix paths= and text= sources."""
    f = tmp_path / "readme.md"
    f.write_text("# Project\nDescription here.", encoding="utf-8")

    calls_by_prompt = {}

    def _tracking_invoke(messages: Any):
        prompt_text = messages[0].content[1]["text"]
        material = messages[0].content[0]["text"]
        calls_by_prompt[prompt_text] = material
        response = MagicMock()
        response.content = f"ok: {prompt_text}"
        response.response_metadata = {}
        return response

    mock_deepseek["llm"].invoke.side_effect = _tracking_invoke

    targets: list[dict[str, str | list[str]]] = [
        {"prompt": "summarize", "text": "inline text"},
        {"prompt": "extract", "paths": [str(f)]},
        {"prompt": "analyze", "text": "more inline"},
    ]
    results = understand_mod.understand(targets)
    assert len(results) == 3
    assert results[0] == "ok: summarize"
    assert results[1] == "ok: extract"
    assert results[2] == "ok: analyze"
    # Verify the paths-based target read the file correctly (dict lookup, order-independent)
    assert calls_by_prompt["extract"] == "# Project\nDescription here."


def test_batch_default_concurrency_uses_the_shared_ceiling(mock_deepseek: dict[str, Any]) -> None:
    """Omitted `max_concurrent` must not exceed the public default cap.

    The default executor can have fewer workers than the request ceiling, so
    the contract is an upper bound rather than a saturation guarantee.
    """
    import threading
    import time

    n = DEFAULT_BATCH_MAX_CONCURRENT + 2
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def _tracking_invoke(messages: Any):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        response = MagicMock()
        response.content = "ok"
        response.response_metadata = {}
        return response

    mock_deepseek["llm"].invoke.side_effect = _tracking_invoke

    targets = [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(n)]
    results = understand_mod.understand(targets)
    assert results == ["ok"] * n
    assert peak <= DEFAULT_BATCH_MAX_CONCURRENT


def test_max_concurrent_validation(mock_deepseek: dict[str, Any]) -> None:
    """max_concurrent must be a positive int or None — bad values fail fast
    before any model call."""
    with pytest.raises(ValueError, match="at least 1"):
        understand_mod.understand([{"prompt": "x", "text": "y"}], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        understand_mod.understand([{"prompt": "x", "text": "y"}], max_concurrent=-2)
    with pytest.raises(TypeError, match="int or None"):
        understand_mod.understand(
            [{"prompt": "x", "text": "y"}],
            max_concurrent="4",  # type: ignore[arg-type]
        )


def test_max_concurrent_caps_inflight_calls(mock_deepseek: dict[str, Any]) -> None:
    """max_concurrent=N keeps at most N model calls in flight — the observed
    peak never exceeds the ceiling, and the batch still completes in order."""
    import threading
    import time

    n_targets, ceiling = 6, 2
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def _tracking_invoke(messages: Any):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        response = MagicMock()
        response.content = "ok"
        response.response_metadata = {}
        return response

    mock_deepseek["llm"].invoke.side_effect = _tracking_invoke
    targets = [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(n_targets)]
    results = understand_mod.understand(targets, max_concurrent=ceiling)
    assert results == ["ok"] * n_targets
    assert peak == ceiling, f"peak in-flight {peak} exceeded ceiling {ceiling}"


def test_max_concurrent_one_serializes(mock_deepseek: dict[str, Any]) -> None:
    """max_concurrent=1 runs every target strictly one after another — peak
    in-flight is exactly 1."""
    import threading
    import time

    lock = threading.Lock()
    inflight = 0
    peak = 0

    def _tracking_invoke(messages: Any):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.02)
        with lock:
            inflight -= 1
        response = MagicMock()
        response.content = "ok"
        response.response_metadata = {}
        return response

    mock_deepseek["llm"].invoke.side_effect = _tracking_invoke
    targets = [{"prompt": f"q{i}", "text": f"m{i}"} for i in range(3)]
    results = understand_mod.understand(targets, max_concurrent=1)
    assert results == ["ok"] * 3
    assert peak == 1


def test_batch_error_propagates(mock_deepseek: dict[str, Any], tmp_path: Path) -> None:
    """When one target fails, the error propagates immediately (asyncio.gather behavior)."""
    f = tmp_path / "exists.txt"
    f.write_text("content", encoding="utf-8")

    targets: list[dict[str, str | list[str]]] = [
        {"prompt": "q1", "text": "ok"},
        {"prompt": "q2", "paths": [str(tmp_path / "nonexistent.txt")]},
        {"prompt": "q3", "text": "also ok"},
    ]
    with pytest.raises(FileNotFoundError):
        understand_mod.understand(targets)
