"""Native cycle proofs cannot lose failures or confuse retained process births."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.preview import linux_cycle, local
from tests.lifecycle.preview import test_local

repo = test_local.repo


@pytest.mark.parametrize("field", ["ports", "hashes", "service_path"])
def test_changed_identity_refuses_success(field: str) -> None:
    before = {"ports": {"gateway": 10000}, "hashes": {".env": "before"}, "service_path": "tools"}
    after = before | {field: "changed"}
    with pytest.raises(RuntimeError, match=r"changed|environments differ"):
        linux_cycle.preserved(before, after)


@pytest.mark.parametrize("same", [False, True])
def test_birth_comparison_checks_each_generation_not_only_pid(same: bool) -> None:
    first = {
        "births": {
            "root": {"pid": 100, "birth": 1, "starttime": 10},
            "gateway": {"pid": 101, "birth": 2, "starttime": 20},
        }
    }
    mixed = {
        "births": {
            "root": first["births"]["root"],
            "gateway": {"pid": 101, "birth": 3, "starttime": 30},
        }
    }
    with pytest.raises(RuntimeError, match="Expected"):
        linux_cycle.births(first, mixed, "births", same=same)
    linux_cycle.births(first, first, "births", same=True)
    recycled = {
        "births": {
            "root": {"pid": 100, "birth": 4, "starttime": 40},
            "gateway": {"pid": 101, "birth": 5, "starttime": 50},
        }
    }
    linux_cycle.births(first, recycled, "births", same=False)


def test_clock_movement_neither_loses_retention_nor_certifies_replacement() -> None:
    original = {"births": {"root": {"pid": 100, "birth": 1, "starttime": 10}}}
    moved = {"births": {"root": {"pid": 100, "birth": 3601, "starttime": 10}}}
    linux_cycle.births(original, moved, "births", same=True)
    with pytest.raises(RuntimeError, match="Expected replaced"):
        linux_cycle.births(original, moved, "births", same=False)


def test_cycle_controller_remains_standard_library_only() -> None:
    root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [sys.executable, "-S", "-c", "import scripts.preview.linux_cycle"],
        cwd=root,
        check=True,
        timeout=10,
    )


def test_dropped_service_never_counts_as_replaced() -> None:
    with pytest.raises(RuntimeError, match="roster"):
        linux_cycle.births({"births": {"root": 1}}, {"births": {}}, "births", same=False)


@pytest.mark.parametrize("failed_phase", ["initial", "ordinary", "manager", "cleanup", None])
def test_cleanup_cannot_erase_a_failed_phase(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_phase: str | None
) -> None:
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    python = preview.source / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    (preview.run / "config.json").write_text("{}")
    cycle = linux_cycle.LinuxCycle(preview)
    reached: list[str] = []

    def operation(phase: str) -> dict[str, Any]:
        reached.append(phase)
        if failed_phase == phase:
            raise RuntimeError(f"failure in {phase}")
        return {}

    def initial(_preview: local.Preview, *, keep: bool) -> None:
        operation("initial")

    def manager(_initial: dict[str, Any]) -> None:
        operation("manager")

    def observe(_label: str, _mode: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(local, "run_preview", initial)
    monkeypatch.setattr(cycle, "ordinary", lambda: operation("ordinary"))
    monkeypatch.setattr(cycle, "manager", manager)
    monkeypatch.setattr(preview, "stop", lambda: operation("cleanup"))
    monkeypatch.setattr(cycle, "observe", observe)
    if failed_phase:
        with pytest.raises(RuntimeError, match=f"failure in {failed_phase}"):
            cycle.run()
    else:
        cycle.run()
    proof = json.loads(cycle.path.read_text())
    assert proof["result"] == ("failed" if failed_phase else "passed")
    assert reached[-1] == "cleanup"
    if failed_phase:
        assert proof["phases"][failed_phase]["result"] == "failed"
        assert "failure in " + failed_phase in proof["phases"][failed_phase]["error"]
    if failed_phase != "cleanup":
        assert proof["phases"]["cleanup"]["result"] == "passed"


def test_existing_failure_evidence_is_not_overwritten(repo: Path, tmp_path: Path) -> None:
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    path = preview.run / "cycle-proof.json"
    path.write_text('{"result":"failed"}\n')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="evidence already exists"):
        linux_cycle.LinuxCycle(preview)
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing", ["source/.venv/bin/python", "config.json"])
def test_missing_cleanup_evidence_refuses_a_green_cycle(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    cycle = linux_cycle.LinuxCycle(preview)
    for relative in ("source/.venv/bin/python", "config.json"):
        path = preview.run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def initial(_preview: local.Preview, *, keep: bool) -> None:
        pass

    def manager(_initial: dict[str, Any]) -> None:
        (preview.run / missing).unlink()

    monkeypatch.setattr(local, "run_preview", initial)
    monkeypatch.setattr(cycle, "ordinary", dict)
    monkeypatch.setattr(cycle, "manager", manager)
    cleaned: list[bool] = []
    monkeypatch.setattr(preview, "stop", lambda: cleaned.append(True))
    with pytest.raises(RuntimeError, match="disappeared before cleanup"):
        cycle.run()
    proof = json.loads(cycle.path.read_text())
    assert proof["result"] == "failed"
    assert proof["phases"]["cleanup"]["result"] == "failed"
    assert cleaned == ([True] if missing == "config.json" else [])


@pytest.mark.parametrize("changed", ["host", "shell", "generation", "record_sha256", "absent"])
def test_manager_stop_must_retain_the_explicit_terminal(changed: str) -> None:
    original = {
        "host": {"pid": 1, "birth": 10},
        "shell": {"pid": 2, "birth": 11},
        "generation": "owner",
        "record_sha256": "record",
    }
    before = {"terminals": {linux_cycle.TERMINAL: original}}
    after = {
        "terminals": {}
        if changed == "absent"
        else {linux_cycle.TERMINAL: original | {changed: "different"}}
    }
    linux_cycle.retained_terminal(before, before)
    with pytest.raises(RuntimeError, match=r"disappeared|changed terminal"):
        linux_cycle.retained_terminal(before, after)
