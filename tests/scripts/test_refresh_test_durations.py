"""refresh_test_durations: the committed durations format and trim contract.

The split balance depends on the .test_durations format staying compact
(sorted keys, 3-decimal values, one trailing newline): a format change would
rewrite the whole file on the next refresh and pollute every shard PR diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import refresh_test_durations as refresh


def test_load_durations_missing_file_is_empty(tmp_path: Path) -> None:
    assert refresh._load_durations(tmp_path / "missing.json") == {}


def test_load_durations_rejects_non_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(SystemExit, match="shape"):
        refresh._load_durations(bad)


def test_load_durations_rejects_non_numeric_values(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"some::test": "fast"}')
    with pytest.raises(SystemExit, match="non-numeric"):
        refresh._load_durations(bad)


def test_write_durations_committed_format_with_trim_and_rounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".test_durations"
    monkeypatch.setattr(refresh, "_DURATIONS_PATH", target)

    kept = refresh._write_durations(
        {
            "tests/a/test_x.py::test_slow": 37.517338,
            "tests/a/test_x.py::test_subsecond": 0.1994,  # rounds below 0.2 -> dropped
            "tests/a/test_x.py::test_boundary": 0.2004,  # rounds to 0.2 -> kept
            "tests/a/test_x.py::test_decimal": 1.23456,
        }
    )

    content = target.read_text()
    assert content == (
        '{"tests/a/test_x.py::test_boundary":0.2,'
        '"tests/a/test_x.py::test_decimal":1.235,'
        '"tests/a/test_x.py::test_slow":37.517}\n'
    )
    assert sorted(kept) == [
        "tests/a/test_x.py::test_boundary",
        "tests/a/test_x.py::test_decimal",
        "tests/a/test_x.py::test_slow",
    ]
    data = json.loads(content)
    assert list(data) == sorted(data)
