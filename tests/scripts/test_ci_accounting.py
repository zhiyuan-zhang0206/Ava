"""Tests for scripts/ci_accounting.py — per-agent CI minute attribution."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci_accounting.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("ci_accounting", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_accounting = _load_script()


def _gh_queue(responses: list[tuple[str, str]]):
    """Subprocess stub: first entry whose key appears in the command wins."""

    def run(cmd: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(str(c) for c in cmd)
        for idx, (key, out) in enumerate(responses):
            if key in joined:
                responses.pop(idx)
                return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 1, "", "unmatched: " + joined)

    return run


def _runs(payload: list[dict[str, object]]) -> str:
    return json.dumps(payload)


def _run(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 101,
        "name": "CI",
        "conclusion": "success",
        "created_at": "2026-09-06T10:00:00Z",
        "head_branch": "ava-5811-demo",
        "head_commit_msg": "",
        "pull_requests": [],
    }
    base.update(overrides)
    return base


def _jobs(*rows: tuple[str, str, str]) -> str:
    return json.dumps(
        [
            {"name": name, "started": started, "completed": completed}
            for name, started, completed in rows
        ]
    )


_WINDOW_SINCE = "2026-09-06T08:00Z"
_WINDOW_UNTIL = "2026-09-06T12:00Z"


def test_pr_run_attribution_and_minute_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-triggered run: identity from the PR title; minutes split linux/macos
    with ceil billing (30s macOS job = 1 minute)."""
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                pull_requests=[{"number": 42}],
                                head_commit_msg="",
                            )
                        ]
                    ),
                ),
                ("pr view 42 ", "[Ava-5811] feat(ci): script queue ops (task #2571)"),
                (
                    "runs/101/jobs",
                    _jobs(
                        ("backend shard (1/16)", "2026-09-06T10:00:00Z", "2026-09-06T10:04:00Z"),
                        (
                            "permissions helper signing smoke (macOS)",
                            "2026-09-06T10:00:00Z",
                            "2026-09-06T10:00:30Z",
                        ),
                        (
                            "cold-offline (ubuntu-24.04)",
                            "2026-09-06T10:04:00Z",
                            "2026-09-06T10:05:30Z",
                        ),
                    ),
                ),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["agent_id"] == 5811
    assert entry["task_id"] == 2571
    assert entry["pr_number"] == 42
    assert entry["linux_minutes"] == 6
    assert entry["macos_minutes"] == 1
    assert entry["jobs"] == 3


def test_trunk_synthetic_pr_resolves_to_real_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic trunk-merge/pr-42/<uuid> run attributes to real PR 42."""
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                id=102,
                                head_branch="trunk-merge/pr-42/e65e4a02-9624-435e-853a-8fb467af307c",
                                pull_requests=[{"number": 77}],
                            )
                        ]
                    ),
                ),
                ("pr view 42 ", "[Ava-5814] feat(gateway): schedule catch-up (task #2492)"),
                ("runs/102/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["pr_number"] == 42
    assert entries[0]["agent_id"] == 5814


def test_push_run_attributes_from_commit_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                id=103,
                                head_branch="main",
                                head_commit_msg="[Ava-5814] feat(web): refine graph (task #2561)",
                            )
                        ]
                    ),
                ),
                ("runs/103/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["agent_id"] == 5814
    assert entries[0]["task_id"] == 2561


def test_unattributed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs([_run(id=104, head_branch="main", head_commit_msg="merge main")]),
                ),
                ("runs/104/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["agent_id"] is None


def test_append_ledger_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs([_run(id=105, head_commit_msg="[Ava-5811] x")]),
                ),
                ("runs/105/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    ledger = tmp_path / "ledger.jsonl"
    assert ci_accounting.append_ledger(ledger, entries) == 1
    assert ci_accounting.append_ledger(ledger, entries) == 0
    loaded = ci_accounting.load_ledger(ledger)
    assert set(loaded) == {105}


def test_report_rollup_days_and_cost() -> None:
    entries = [
        {
            "run_id": 1,
            "day": "2026-09-06",
            "agent_id": 5811,
            "task_id": None,
            "pr_number": None,
            "name": "CI",
            "head_branch": "x",
            "conclusion": "success",
            "linux_minutes": 100,
            "macos_minutes": 10,
            "jobs": 5,
        },
        {
            "run_id": 2,
            "day": "2026-09-06",
            "agent_id": 5814,
            "task_id": None,
            "pr_number": None,
            "name": "CI",
            "head_branch": "y",
            "conclusion": "success",
            "linux_minutes": 50,
            "macos_minutes": 0,
            "jobs": 5,
        },
        {
            "run_id": 3,
            "day": "2026-08-01",
            "agent_id": 5811,
            "task_id": None,
            "pr_number": None,
            "name": "CI",
            "head_branch": "z",
            "conclusion": "success",
            "linux_minutes": 999,
            "macos_minutes": 0,
            "jobs": 5,
        },
    ]
    ledger = {e["run_id"]: e for e in entries}
    rows = ci_accounting.report_rows(ledger, days=7, agent=None)
    assert len(rows) == 2  # the 2026-08-01 entry is outside the window
    top = rows[0]
    assert top["agent_id"] == 5811
    assert top["linux_minutes"] == 100
    assert top["macos_minutes"] == 10
    assert top["est_usd"] == pytest.approx(100 * 0.006 + 10 * 0.062)  # pyright: ignore[reportUnknownMemberType]
    only = ci_accounting.report_rows(ledger, days=7, agent=5814)
    assert only[0]["agent_id"] == 5814
    assert only[0]["linux_minutes"] == 50


def test_trunk_branch_without_pr_payload_attributes_by_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue test runs on trunk-merge/pr-<n> branches have no pull_requests
    payload; the branch name resolves the real PR."""
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                id=106,
                                head_branch="trunk-merge/pr-42/ffffffff-ffff-ffff-ffff-ffffffffffff",
                                pull_requests=[],
                                head_commit_msg="merge-tree commit",
                            )
                        ]
                    ),
                ),
                ("pr view 42 ", "[Ava-5814] feat(gateway): schedule catch-up (task #2492)"),
                ("runs/106/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["pr_number"] == 42
    assert entries[0]["agent_id"] == 5814


def test_main_push_run_resolves_pr_via_merge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A push-to-main CI run with a non-convention commit subject attributes
    through the merge commit's associated PR."""
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                id=107,
                                head_branch="main",
                                head_sha="dddd3333dddd3333dddd3333dddd3333dddd3333",
                                head_commit_msg="Preserve hosted agent continuations",
                            )
                        ]
                    ),
                ),
                ("commits/dddd3333dddd3333dddd3333dddd3333dddd3333/pulls", "[1872]"),
                ("pr view 1872 ", "[Ava-5814] fix: hosted continuations (task #2566)"),
                ("runs/107/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["pr_number"] == 1872
    assert entries[0]["agent_id"] == 5814


def test_branch_name_attributes_nonprefix_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conventional-commit subject without [Ava-N] attributes through the
    agent worktree branch name."""
    monkeypatch.setattr(
        ci_accounting.subprocess,
        "run",
        _gh_queue(
            [
                (
                    "actions/runs",
                    _runs(
                        [
                            _run(
                                id=108,
                                head_branch="ava-5810-workfailed-bounds",
                                head_commit_msg="Bound the work-failed webhook text columns (task #2531)",
                            )
                        ]
                    ),
                ),
                ("runs/108/jobs", _jobs()),
            ]
        ),
    )
    entries = ci_accounting.collect(ci_accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL)
    assert entries[0]["agent_id"] == 5810
    assert entries[0]["task_id"] == 2531
