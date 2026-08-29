"""refresh_test_durations: the committed durations format and safety paths.

The split balance depends on the .test_durations format staying compact
(sorted keys, 3-decimal values, one trailing newline) and the file never
being left truncated: every backend shard loads it at collection time, so a
corrupt file breaks CI repo-wide. These tests lock the format contract, the
atomic-write guarantee, and the failure semantics (backend abort, e2e
fallback).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import refresh_test_durations as refresh


def test_load_durations_missing_file_is_empty(tmp_path: Path) -> None:
    assert refresh._load_durations(tmp_path / "missing.json") == {}


def test_load_durations_corrupt_json_is_empty(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"tests/a/test_x.py::test_broken": 1.')
    assert refresh._load_durations(bad) == {}


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


def test_write_durations_is_atomic_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".test_durations"
    monkeypatch.setattr(refresh, "_DURATIONS_PATH", target)
    refresh._write_durations({"tests/a::test_x": 1.5})

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="interrupted"):
        refresh._write_durations({"tests/a::test_y": 2.5})

    # The committed file is untouched and no temp file is left behind.
    assert target.read_text() == '{"tests/a::test_x":1.5}\n'
    assert list(tmp_path.iterdir()) == [target]


def _fake_runner_factory(backend_data: dict[str, float], e2e_data: dict[str, float]):
    """Build a _run_suite fake that records durations into the given paths."""

    def _fake_run_suite(
        targets: list[str],
        workers: int,
        durations_path: Path,
        *,
        coverage: bool,
    ) -> int:
        data = e2e_data if any(t.startswith("tests/e2e") for t in targets) else backend_data
        durations_path.write_text(json.dumps(data))
        return 0

    return _fake_run_suite


def test_main_aborts_on_backend_failure_and_leaves_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".test_durations"
    target.write_text('{"tests/a::test_x": 1.5}\n')
    monkeypatch.setattr(refresh, "_DURATIONS_PATH", target)

    def _fail(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(refresh, "_run_suite", _fail)

    assert refresh.main() == 1
    assert target.read_text() == '{"tests/a::test_x": 1.5}\n'


def test_main_keeps_previous_e2e_entries_when_e2e_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".test_durations"
    target.write_text(
        json.dumps({"tests/agent/test_a.py::test_one": 1.0, "tests/e2e/test_b.py::test_two": 5.0})
    )
    monkeypatch.setattr(refresh, "_DURATIONS_PATH", target)
    fake = _fake_runner_factory(
        backend_data={"tests/agent/test_a.py::test_one": 1.1},
        e2e_data={},
    )
    monkeypatch.setattr(refresh, "_run_suite", fake)

    assert refresh.main() == 0
    data = json.loads(target.read_text())
    assert data == {"tests/agent/test_a.py::test_one": 1.1, "tests/e2e/test_b.py::test_two": 5.0}


def test_main_merges_both_suite_durations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / ".test_durations"
    target.write_text("{}")
    monkeypatch.setattr(refresh, "_DURATIONS_PATH", target)
    fake = _fake_runner_factory(
        backend_data={"tests/agent/test_a.py::test_one": 1.1},
        e2e_data={"tests/e2e/test_b.py::test_two": 0.5},
    )
    monkeypatch.setattr(refresh, "_run_suite", fake)

    assert refresh.main() == 0
    data = json.loads(target.read_text())
    assert data == {
        "tests/agent/test_a.py::test_one": 1.1,
        "tests/e2e/test_b.py::test_two": 0.5,
    }
