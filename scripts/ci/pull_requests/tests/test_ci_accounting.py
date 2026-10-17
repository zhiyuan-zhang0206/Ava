from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import accounting


def _gh_queue(responses: list[tuple[str, str]]) -> Host:
    def run(argv: Sequence[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        joined = " ".join(str(c) for c in argv)
        for idx, (key, out) in enumerate(responses):
            if key in joined:
                responses.pop(idx)
                return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 1, "", "unmatched: " + joined)

    return fake_host(run)


def _runs(payload: list[dict[str, object]]) -> str:
    return json.dumps({"total_count": len(payload), "workflow_runs": payload})


def _run(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 101,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-09-06T10:00:00Z",
        "head_branch": "meiva-5811-demo",
        "head_commit_msg": "",
        "pull_requests": [],
    }
    base.update(overrides)
    base["head_commit"] = {"message": base.pop("head_commit_msg")}
    return base


def _jobs(*rows: tuple[str | None, str | None, str | None]) -> str:
    return json.dumps(
        {
            "total_count": len(rows),
            "jobs": [
                {
                    "id": index,
                    "name": name,
                    "status": "completed",
                    "conclusion": "cancelled" if started is None else "success",
                    "started_at": started,
                    "completed_at": completed,
                }
                for index, (name, started, completed) in enumerate(rows)
            ],
        }
    )


_WINDOW_SINCE = "2026-09-06T08:00Z"
_WINDOW_UNTIL = "2026-09-06T12:00Z"


def test_pr_run_attribution_and_minute_split() -> None:
    host = _gh_queue(
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
            ("pr view 42 ", "[Meiva-5811] feat(ci): script queue ops (task #2571)"),
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
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["agent_id"] is None
    assert entry["task_id"] == 2571
    assert entry["pr_number"] == 42
    assert entry["linux_minutes"] == 6
    assert entry["macos_minutes"] == 1
    assert entry["jobs"] == 3


def test_trunk_synthetic_pr_resolves_to_real_pr() -> None:
    host = _gh_queue(
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
            ("pr view 42 ", "feat(gateway): schedule catch-up (task #2492)"),
            ("runs/102/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["pr_number"] == 42
    assert entries[0]["agent_id"] is None
    assert entries[0]["task_id"] == 2492


def test_push_run_attributes_from_commit_subject() -> None:
    host = _gh_queue(
        [
            (
                "actions/runs",
                _runs(
                    [
                        _run(
                            id=103,
                            head_branch="main",
                            head_commit_msg="feat(web): refine graph (task #2561)",
                        )
                    ]
                ),
            ),
            ("runs/103/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["agent_id"] is None
    assert entries[0]["task_id"] == 2561


def test_unattributed_run() -> None:
    host = _gh_queue(
        [
            (
                "actions/runs",
                _runs([_run(id=104, head_branch="main", head_commit_msg="merge main")]),
            ),
            ("runs/104/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["agent_id"] is None


def test_append_ledger_is_idempotent(tmp_path: Path) -> None:
    host = _gh_queue(
        [
            (
                "actions/runs",
                _runs([_run(id=105, head_commit_msg="[Meiva-5811] x")]),
            ),
            ("runs/105/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    ledger = tmp_path / "ledger.jsonl"
    assert accounting.append_ledger(ledger, entries) == 1
    assert accounting.append_ledger(ledger, entries) == 0
    loaded = accounting.load_ledger(ledger)
    assert set(loaded) == {105}


def test_report_rollup_days_and_cost() -> None:
    today = datetime.now(UTC).date()
    recent = (today - timedelta(days=1)).isoformat()
    stale = (today - timedelta(days=30)).isoformat()
    entries: list[dict[str, Any]] = [
        {
            "run_id": 1,
            "day": recent,
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
            "day": recent,
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
            "day": stale,
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
    rows = accounting.report_rows(ledger, days=7, agent=None)
    assert len(rows) == 2
    top = rows[0]
    assert top["agent_id"] == 5811
    assert top["linux_minutes"] == 100
    assert top["macos_minutes"] == 10
    assert top["est_usd"] == pytest.approx(100 * 0.006 + 10 * 0.062)
    only = accounting.report_rows(ledger, days=7, agent=5814)
    assert only[0]["agent_id"] == 5814
    assert only[0]["linux_minutes"] == 50


def test_trunk_branch_without_pr_payload_attributes_by_branch() -> None:
    host = _gh_queue(
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
            ("pr view 42 ", "feat(gateway): schedule catch-up (task #2492)"),
            ("runs/106/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["pr_number"] == 42
    assert entries[0]["agent_id"] is None
    assert entries[0]["task_id"] == 2492


def test_main_push_run_resolves_pr_via_merge_commit() -> None:
    host = _gh_queue(
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
            ("commits/dddd3333dddd3333dddd3333dddd3333dddd3333/pulls", '[{"number":1872}]'),
            ("pr view 1872 ", "fix: hosted continuations (task #2566)"),
            ("runs/107/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["pr_number"] == 1872
    assert entries[0]["agent_id"] is None
    assert entries[0]["task_id"] == 2566


def test_branch_name_attributes_nonprefix_subject() -> None:
    host = _gh_queue(
        [
            (
                "actions/runs",
                _runs(
                    [
                        _run(
                            id=108,
                            head_branch="meiva-5810-workfailed-bounds",
                            head_commit_msg="Bound the work-failed webhook text columns (task #2531)",
                        )
                    ]
                ),
            ),
            ("runs/108/jobs", _jobs()),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert entries[0]["agent_id"] is None
    assert entries[0]["task_id"] == 2531


def test_cancelled_before_start_jobs_bill_zero() -> None:
    host = _gh_queue(
        [
            (
                "actions/runs",
                _runs(
                    [
                        _run(
                            id=109,
                            head_branch="main",
                            head_commit_msg="fix(ci): null job timestamps (task #2673)",
                        )
                    ]
                ),
            ),
            (
                "runs/109/jobs",
                _jobs(
                    ("backend shard (1/16)", "2026-09-06T10:00:00Z", "2026-09-06T10:04:00Z"),
                    ("cancelled before start (started null)", None, "2026-09-06T10:02:00Z"),
                    ("cancelled before start (both null)", None, None),
                ),
            ),
        ]
    )
    entries = accounting.collect(accounting.DEFAULT_REPO, _WINDOW_SINCE, _WINDOW_UNTIL, host=host)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["agent_id"] is None
    assert entry["task_id"] == 2673
    assert entry["linux_minutes"] == 4
    assert entry["jobs"] == 3


def test_job_minutes_null_contract() -> None:
    assert accounting.job_minutes(None, "2026-09-06T10:05:00Z") == 0
    assert accounting.job_minutes("2026-09-06T10:00:00Z", None) == 0
    assert accounting.job_minutes(None, None) == 0
    assert accounting.job_minutes("2026-09-06T10:00:00Z", "2026-09-06T10:04:00Z") == 4
