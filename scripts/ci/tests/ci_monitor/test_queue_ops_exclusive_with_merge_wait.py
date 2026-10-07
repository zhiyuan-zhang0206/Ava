# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownMemberType = warning
"""Ci monitor cases: queue ops exclusive with merge wait."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.ci import commands as ci_utils
from scripts.ci import monitor, owner_operations, status
from scripts.ci.tests.test_ci_monitor import (
    CIStatus,
    _aged,
    _check,
    _diag_check,
    _diag_job,
    _diag_jobs_payload,
    _diag_pr_view,
    _diag_runs,
    _install_probe,
    _install_results_poller,
    _limbo_result,
    _TrunkResponse,
    _urlopen_sequence,
)
from scripts.ci.tests.test_ci_monitor import (
    diag_gh as diag_gh,
)
from scripts.ci.tests.test_ci_monitor import (
    gh as gh,
)
from scripts.ci.tests.test_ci_monitor import (
    has_workflows as has_workflows,
)
from scripts.ci.tests.test_ci_monitor import (
    no_sleep as no_sleep,
)
from scripts.ci.tests.test_ci_monitor import (
    poll as poll,
)


def test_queue_ops_exclusive_with_merge_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["--queue-status", "--merge"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--wait"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--queue-status"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--json"])


def test_diagnose_merge_conflict(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(mergeable="CONFLICTING")),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "merge_conflict" in out
    assert "rebase on origin/main" in out


def test_diagnose_lint_hard_limit(diag_gh, monkeypatch, capsys) -> None:
    check = "backend structure (pre-commit lint + codegen freshness)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            ("/logs", "scripts/host.py file is 812 lines, over the 800-line hard ceiling"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "lint hard limit" in out
    assert "split the file" in out


def test_diagnose_first_load_budget(diag_gh, monkeypatch, capsys) -> None:
    check = "Production build + first-load JavaScript budget"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            ("/logs", "First Load JS shared by all is 512 kB (budget 500 kB)"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "first-load JavaScript budget" in capsys.readouterr().out


def test_diagnose_visual_regression(diag_gh, monkeypatch, capsys) -> None:
    check = "e2e shard (3/4)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            ("/logs", "toMatchImageSnapshot failed: baseline image differs"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "visual regression" in capsys.readouterr().out


def test_diagnose_truncate_lint_delta_case(diag_gh, monkeypatch, capsys) -> None:
    """The #1871 delta case: truncate-isolation lint's comment-stripping
    regex falsely captured the word delta — a deterministic lint failure."""
    check = "lint truncate isolation"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            (
                "/logs",
                "truncate-isolation lint: comment-stripping regex captured CREATE TABLE delta",
            ),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "truncate-isolation lint" in capsys.readouterr().out


def test_diagnose_clock_lattice_gateway_case(diag_gh, monkeypatch, capsys) -> None:
    """The #1871 gateway closure case: a constant outside its family module."""
    check = "lint clock lattice"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            ("/logs", "lattice-vocabulary clock constant defined outside its family module"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "clock-lattice lint" in capsys.readouterr().out


def test_diagnose_test_failure_uses_native_evidence(diag_gh, monkeypatch, capsys) -> None:
    """Native failures are diagnosed without consulting a quarantine list."""
    check = "backend shard (4/16)"
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    calls: list[urllib.request.Request] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "testing"})], calls),
    )
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            (
                "/logs",
                "FAILED tests/agent/test_consumer_guard.py::test_consumer_guard_queue_backpressure",
            ),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "unclassified CI failure" in out
    assert "known flake" not in out
    assert "Flaky DB" not in out
    assert "state=testing" in out
    assert len(calls) == 1
    assert "getSubmittedPullRequest" in calls[0].full_url


def test_diagnose_runner_network_flake(diag_gh, monkeypatch, capsys) -> None:
    check = "backend (pytest + pyright)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", _diag_runs()),
            ("/jobs", _diag_jobs_payload(_diag_job(check))),
            ("/logs", "apt-get install failed: archive cache is empty — no offline fallback"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "runner-side network flake" in capsys.readouterr().out


def test_diagnose_reports_synthetic_test_pr(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view()),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            (
                "state=all",
                json.dumps(
                    [
                        {
                            "number": 1875,
                            "state": "closed",
                            "head": {
                                "ref": "trunk-merge/pr-1871/e65e4a02-9624-435e-853a-8fb467af307c"
                            },
                        }
                    ]
                ),
            ),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "synthetic test PR #1875" in out
    assert "trunk-merge/pr-1871/" in out


def test_diagnose_stale_base(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view()),
            ("--json baseRefOid --jq", "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "stale_base" in capsys.readouterr().out


def test_diagnose_json_machine_readable(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check("lint clock lattice")])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == "1871"
    assert payload["checks"][0]["classification"] == "deterministic lint failure"
    assert "issues" in payload and "synthetic_test_prs" in payload


def test_diagnose_exclusive_and_requires_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--diagnose", "--wait"])
    with pytest.raises(SystemExit):
        ci_utils.main(["--diagnose"])


def test_diagnose_merged_pr_reports_no_conflict_or_stale_base(diag_gh, monkeypatch, capsys) -> None:
    """A merged PR has mergeable=UNKNOWN and a base behind main by definition —
    neither may be reported as a diagnosable problem."""
    view = json.loads(_diag_pr_view(mergeable="UNKNOWN"))
    view["state"] = "MERGED"
    diag_gh(
        [
            ("--json mergeable", json.dumps(view)),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "merge_conflict" not in out
    assert "stale_base" not in out


def test_ci_usage_prints_per_agent_rollup(monkeypatch, tmp_path, capsys) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "run_id": 1,
                # Window-relative day: the rollup window is a rolling
                # `--ci-usage-days` x 24h, so a hard-coded "recent" day ages out
                # of it (2026-09-13: the 2026-09-06 fixtures aged out at 00:00
                # UTC and turned every backend shard red).
                "day": (datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
                "agent_id": 5811,
                "linux_minutes": 100,
                "macos_minutes": 10,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(ci_utils, "DEFAULT_LEDGER", ledger)
    assert ci_utils.main(["--ci-usage"]) == 0
    out = capsys.readouterr().out
    assert "#5811: 1 runs" in out
    assert "est $1.22" in out


def test_ci_usage_json_machine_readable(monkeypatch, tmp_path, capsys) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "run_id": 1,
                "day": (datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
                "agent_id": 5811,
                "linux_minutes": 10,
                "macos_minutes": 1,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(ci_utils, "DEFAULT_LEDGER", ledger)
    assert ci_utils.main(["--ci-usage", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["agent_id"] == 5811
    assert rows[0]["linux_minutes"] == 10


def test_ci_usage_exclusive_and_no_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--ci-usage"])
    with pytest.raises(SystemExit):
        ci_utils.main(["--ci-usage", "--wait"])


def test_limbo_runs_flags_aged_queued_zero_job_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    _install_probe(
        monkeypatch,
        [{"id": 91, "name": "Native root lifetime proof", "created_at": _aged(910)}],
        {91: 0},
        calls=calls,
    )
    got = status._limbo_runs("abc1234", "o/r")
    assert got is not None
    assert [r["id"] for r in got] == [91]
    assert got[0]["name"] == "Native root lifetime proof"
    assert got[0]["age_s"] >= 900
    # the runs probe filters queued-only server-side; the jobs probe confirms zero.
    assert 'select(.status == "queued")' in calls[0][-1]
    assert calls[1][2].endswith("/runs/91/jobs")


def test_limbo_runs_skips_fresh_and_progressing_runs(monkeypatch) -> None:
    _install_probe(
        monkeypatch,
        [
            {"id": 92, "name": "fresh", "created_at": _aged(120)},  # below the age bound
            {"id": 93, "name": "progressing", "created_at": _aged(1200)},  # has jobs
            {"id": 94, "name": "stuck", "created_at": _aged(1200)},
        ],
        {93: 3, 94: 0},
    )
    result = status._limbo_runs("abc1234", "o/r")
    assert result is not None
    assert [r["id"] for r in result] == [94]


def test_limbo_runs_probe_failures_never_read_as_limbo(monkeypatch) -> None:
    # An unanswerable runs probe is None (missing evidence), never [].
    _install_probe(monkeypatch, [], {}, runs_rc=1)
    assert status._limbo_runs("abc1234", "o/r") is None
    # Unreadable output is the same: not evidence of limbo.
    _install_probe(monkeypatch, "not json", {}, runs_rc=0)
    assert status._limbo_runs("abc1234", "o/r") is None
    # A candidate whose job count cannot be read is skipped, not assumed stuck.
    _install_probe(
        monkeypatch,
        [{"id": 95, "name": "stuck", "created_at": _aged(1200)}],
        {95: 0},
        jobs_rc={95: 1},
    )
    assert status._limbo_runs("abc1234", "o/r") == []


def test_limbo_runs_skips_unparseable_created_at_and_non_int_id(monkeypatch) -> None:
    calls: list[list[str]] = []
    _install_probe(
        monkeypatch,
        [
            {"id": 96, "name": "no-created", "created_at": None},
            {"id": "97", "name": "str-id", "created_at": _aged(1200)},
            {"id": 98, "name": "garbage-created", "created_at": "yesterday"},
        ],
        {},
        calls=calls,
    )
    assert status._limbo_runs("abc1234", "o/r") == []
    assert len(calls) == 1  # no jobs probe ran for any candidate


def test_check_ci_attaches_limbo_to_pending(gh: Any, has_workflows: Any, monkeypatch) -> None:
    gh(
        [_check("backend (pytest + pyright)", "SUCCESS")],
        scheduled=["Example proof A"],
    )
    has_workflows(True)
    stuck = [{"id": 91, "name": "Example proof A", "age_s": 1500}]
    monkeypatch.setattr(status, "_limbo_runs", lambda *_a, **_k: stuck)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["Example proof A"]
    assert r.limbo == stuck
    assert "GitHub limbo" in r.summary()


def test_check_ci_empty_rollup_with_scheduled_run_probes_limbo(
    gh: Any, has_workflows: Any, monkeypatch
) -> None:
    gh([], scheduled=["CI"])
    has_workflows(True)
    monkeypatch.setattr(
        status, "_limbo_runs", lambda *_a, **_k: [{"id": 92, "name": "CI", "age_s": 1200}]
    )
    r = status.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.limbo == [{"id": 92, "name": "CI", "age_s": 1200}]


def test_check_ci_limbo_probe_none_stays_plain_pending(
    gh: Any, has_workflows: Any, monkeypatch
) -> None:
    gh([_check("backend", "SUCCESS")], scheduled=["CI"])
    has_workflows(True)
    monkeypatch.setattr(status, "_limbo_runs", lambda *_a, **_k: None)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.limbo == []


def test_query_once_json_includes_limbo(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        status,
        "check_ci",
        lambda *_a, **_k: _limbo_result(
            pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 700}]
        ),
    )
    assert ci_utils.main(["1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["limbo"] == [{"id": 91, "name": "CI", "age_s": 700}]


def test_report_limbo_names_once_per_key(capsys) -> None:
    result = _limbo_result(pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 660}])
    key = monitor._report_limbo(result, None)
    first = capsys.readouterr()
    assert key == (91,)
    assert "GitHub limbo" in first.err
    assert "(#91," in first.err
    # The same state does not re-print on every poll.
    assert monitor._report_limbo(result, key) == key
    assert capsys.readouterr().err == ""


def test_wait_names_limbo_runs_and_says_the_block(no_sleep, monkeypatch, capsys) -> None:
    _install_results_poller(
        monkeypatch,
        _limbo_result(pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 1500}]),
    )
    assert ci_utils.main(["1243", "--wait", "--timeout", "1"]) == 1
    err = capsys.readouterr().err
    assert "GitHub limbo" in err
    assert "the block is" in err
    assert "91" in err


@pytest.mark.parametrize("mode", [[], ["--wait"], ["--merge"]])
def test_removed_force_flag_is_rejected(
    mode: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        ci_utils.main(["42", *mode, "--force"])
    assert error.value.code == 2
    assert "unrecognized arguments: --force" in capsys.readouterr().err


@pytest.mark.parametrize("pending", [["CI"], ["CI", "docs lint"]])
def test_limbo_never_enqueues_or_claims_green(
    no_sleep: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pending: list[str],
) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    _install_results_poller(
        monkeypatch,
        _limbo_result(pending=pending, limbo=[{"id": 91, "name": "CI", "age_s": 1500}]),
    )
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(status.time, "monotonic", lambda: next(clock))
    submissions: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        owner_operations, "_submit_trunk", lambda *a, **_k: submissions.append(a) or 0
    )
    assert ci_utils.main(["42", "--merge", "--timeout", "1"]) == 1
    assert submissions == []
    output = capsys.readouterr()
    assert "CI green" not in output.out
    assert "GitHub limbo" in output.err
    assert "remain pending, never green" in output.err


@pytest.mark.parametrize("arguments", [["42"], ["42", "--wait"]])
def test_read_only_cli_does_not_load_owner_operations(arguments: list[str]) -> None:
    # A fresh interpreter proves the import boundary even when this test module
    # has imported the owner tools for their separate contract tests.
    code = "\n".join(
        [
            "import importlib.abc, json, os, sys",
            f"sys.path.insert(0, {str(Path.cwd())!r})",
            "class RejectOwnerImports(importlib.abc.MetaPathFinder):",
            "    def find_spec(self, fullname, path, target=None):",
            "        if fullname in ('scripts.ci.owner_operations', 'scripts.ci.trunk_api'):",
            "            raise AssertionError('read-only CLI imported owner operations: ' + fullname)",
            "sys.meta_path.insert(0, RejectOwnerImports())",
            "os.environ.pop('TRUNK_API_TOKEN', None)",
            "os.environ['CI_QUEUE'] = 'unrelated-owner-queue'",
            "from scripts import ci_utils",
            "from scripts.ci import status",
            "status.check_ci = lambda *a, **k: status.CIResult(status.CIStatus.ALL_PASSED)",
            f"raise SystemExit(ci_utils.main({arguments!r}))",
        ]
    )
    result = subprocess.run(  # noqa: S603 - hermetic code built from fixed test inputs
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "green" in result.stdout


@pytest.mark.parametrize(
    "next_verdict", [CIStatus.PENDING, CIStatus.NO_WORKFLOW_RUNS, CIStatus.ERROR]
)
def test_owner_rechecks_genuine_green_before_submission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], next_verdict: CIStatus
) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    _install_results_poller(
        monkeypatch, status.CIResult(CIStatus.ALL_PASSED), status.CIResult(next_verdict)
    )
    monkeypatch.setattr(owner_operations, "_queue_cooldown_seconds", lambda *_a: 0)
    submissions: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        owner_operations, "_submit_trunk", lambda *a, **_k: submissions.append(a) or 0
    )
    assert ci_utils.main(["42", "--merge"]) == (3 if next_verdict is CIStatus.ERROR else 1)
    assert submissions == []
    assert "submitted" not in capsys.readouterr().out


def test_merge_and_rerun_conflict_is_rejected_before_polling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")

    def unexpected_poll(*_a: Any, **_k: Any) -> status.CIResult:
        raise AssertionError("invalid owner command polled CI")

    monkeypatch.setattr(status, "check_ci", unexpected_poll)
    with pytest.raises(SystemExit) as error:
        ci_utils.main(["42", "--merge", "--rerun-failed-jobs"])
    assert error.value.code == 2
    assert "exclusive" in capsys.readouterr().err
