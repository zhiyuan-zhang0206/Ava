"""Verify actual native literal selection logic without accessing Accessibility."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

from .. import lifecycle


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None, reason="needs Swift on macOS"
)
def test_native_selection_literal_utf16_ranges(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    body = (
        "private func axSelectionRange("
        + source.split("private func axSelectionRange(", 1)[1].split(
            "private let axActionTable:", 1
        )[0]
    )
    cases = (Path(__file__).parent / "fixtures" / "ax_selection_cases.swift").read_text()
    program = tmp_path / "selection.swift"
    program.write_text(
        "import Foundation\nimport CoreFoundation\nenum OpError: Error { case bad(String) }\n"
        + body
        + cases
    )
    result = run_bounded(["swift", str(program)], timeout=60, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
