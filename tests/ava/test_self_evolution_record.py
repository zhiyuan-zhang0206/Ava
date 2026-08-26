"""Unit tests for the self-evolution record builder (reference/record.py).

Same import pattern as test_self_evolution_daily_scan.py: the reference dir is
a script dir, not a package, so the module is imported via importlib.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REF_DIR = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-evolution"
    / "reference"
)


@pytest.fixture()
def record() -> Any:
    sys.path.insert(0, str(REF_DIR))
    try:
        return cast(Any, importlib.import_module("record"))
    finally:
        sys.path.remove(str(REF_DIR))


@pytest.mark.parametrize(
    ("event", "is_ok", "is_fail"),
    [
        ("exec", True, False),
        ("exec_failed", False, True),
        ("exec_timeout", False, True),
        ("exec_node_timeout", False, True),
        ("exec_cancelled", False, True),
        ("exec_subprocess_aborted", False, True),
        ("exec_subprocess_killed", False, True),
        ("exec_result_envelope_invalid", False, True),
        # informational events are outcomes neither ok nor fail
        ("exec_envelope", False, False),
        ("exec_start", False, False),
        ("exec_output", False, False),
        ("exec_output_chunk", False, False),
        ("turn_end", False, False),
    ],
)
def test_exec_outcome_classification(record: Any, event: str, is_ok: bool, is_fail: bool) -> None:
    assert record._is_exec_ok(event) is is_ok
    assert record._is_exec_fail(event) is is_fail
