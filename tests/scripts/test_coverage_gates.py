"""Unit tests for scripts/coverage_gates.py — the backend CI coverage gates.

The script parses coverage.json (coverage.py's JSON report) and enforces two
tiers: the combined core-domain line-rate gate and per-risk-domain minimum
line floors for ops/services/ava_builtins. These tests build synthetic
reports, so no real coverage data is involved.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "coverage_gates.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("coverage_gates", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gates = _load_script()


@pytest.fixture(autouse=True)
def _no_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from the script's calibrated floor table."""
    monkeypatch.setattr(gates, "FLOORS", {})


def _files(classes: dict[str, tuple[int, int]]) -> dict[str, dict]:
    """A synthetic coverage.json `files` map: {path: (covered, valid)}."""
    return {
        path: {"summary": {"covered_lines": covered, "num_statements": valid}}
        for path, (covered, valid) in classes.items()
    }


_CORE = ("agent", "ava", "cli", "gateway", "shared", "ui")


def _core_classes(covered: int = 9, valid: int = 10) -> dict[str, tuple[int, int]]:
    return dict.fromkeys(_CORE, (covered, valid))


def test_core_gate_passes_and_prints_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gates, "FLOORS", {"ops": 0.0, "services": 0.0, "ava_builtins": 0.0})
    files = _files(
        {**_core_classes(), "ops": (4, 10), "services": (3, 10), "ava_builtins": (2, 10)}
    )
    assert gates.check(files, threshold=85.0) == 0
    out = capsys.readouterr().out
    assert "core domains agent+ava+cli+gateway+shared+ui: 90.0%" in out
    assert "ops" in out and "services" in out and "ava_builtins" in out


def test_core_gate_fails_below_threshold(capsys: pytest.CaptureFixture[str]) -> None:
    files = _files(_core_classes(covered=8, valid=10))
    assert gates.check(files, threshold=85.0) == 1
    out = capsys.readouterr().out
    assert "coverage gates FAILED" in out
    assert "80.0% below 85.0%" in out


def test_domain_floor_fails_below_minimum(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gates, "FLOORS", {"ops": 50.0})
    files = _files({"ops/deploy_spawn.py": (4, 10)})
    assert gates.check(files, threshold=85.0) == 1
    out = capsys.readouterr().out
    assert "ops coverage 40.0% below floor 50.0%" in out


def test_subdomain_floor_aggregates_over_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A floor on "services/pitr" covers every file under that prefix,
    independent of other services subdomains."""
    monkeypatch.setattr(gates, "FLOORS", {"services/pitr": 50.0})
    files = _files(
        {
            **_core_classes(),
            "services/pitr/uploader.py": (8, 10),
            "services/pitr/state.py": (4, 10),
            "services/backup.py": (9, 10),
        }
    )
    assert gates.check(files, threshold=85.0) == 0
    out = capsys.readouterr().out
    assert "services/pitr" in out and "60.0%" in out


def test_zero_line_floored_domain_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A floored domain absent from the report must fail loudly (renamed
    package / broken source glob), never pass vacuously."""
    monkeypatch.setattr(gates, "FLOORS", {"ops": 10.0})
    files = _files({"agent": (9, 10)})
    assert gates.check(files, threshold=85.0) == 1
    out = capsys.readouterr().out
    assert "ops has no measured lines" in out


def test_zero_floor_still_fails_missing_domain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The existence check is independent of the floor value: even at
    floor 0.0 a vanished domain must fail — the calibration-state table
    cannot mask a broken source glob."""
    monkeypatch.setattr(gates, "FLOORS", {"ops": 0.0})
    files = _files(_core_classes())
    assert gates.check(files, threshold=85.0) == 1
    out = capsys.readouterr().out
    assert "ops has no measured lines" in out


def test_zero_floor_passes_present_domain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The floor boundary is inclusive: a present domain at exactly its
    floor passes (rate >= floor, 0.0 >= 0.0)."""
    monkeypatch.setattr(gates, "FLOORS", {"ops": 0.0})
    files = _files({**_core_classes(), "ops/deploy_spawn.py": (0, 10)})
    assert gates.check(files, threshold=85.0) == 0
    out = capsys.readouterr().out
    assert "coverage gates passed" in out
    assert "floor 0.0% ok" in out


def test_env_threshold_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BACKEND_COVERAGE_THRESHOLD keeps its ci.yml wiring: the script reads
    the same env the old inline gate read."""
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": _files(_core_classes(covered=8, valid=10))}))
    monkeypatch.setattr(sys, "argv", ["coverage_gates", str(report)])
    assert gates.main() == 1  # 80% below the 85% default
    monkeypatch.setenv("BACKEND_COVERAGE_THRESHOLD", "80")
    assert gates.main() == 0
