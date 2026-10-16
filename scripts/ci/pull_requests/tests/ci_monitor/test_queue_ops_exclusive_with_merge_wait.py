from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from base.host.net.tests.fake_url_opener import FakeUrlOpener
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import commands as ci_utils
from scripts.ci.pull_requests import monitor, owner_operations, status
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    TEST_CLOCK,
    CIStatus,
    TrunkResponse,
    aged,
    diag_check,
    diag_job,
    diag_jobs_payload,
    diag_pr_view,
    diag_runs,
    install_probe,
    install_results_poller,
    limbo_result,
    status_check_rollup_entry,
    urlopen_sequence,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    diag_gh as diag_gh,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    gh as gh,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    has_workflows as has_workflows,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    no_sleep as no_sleep,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    poll as poll,
)


def test_queue_ops_exclusive_with_merge_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["--queue-status", "--merge"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--evict", "--wait"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--evict", "--queue-status"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--evict", "--json"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )


def test_diagnose_merge_conflict(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(mergeable="CONFLICTING")),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    out = capsys.readouterr().out
    assert "merge_conflict" in out
    assert "rebase on origin/main" in out


def test_diagnose_lint_hard_limit(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    check = "backend structure (pre-commit lint + codegen freshness)"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            ("/logs", "scripts/host.py file is 812 lines, over the 800-line hard ceiling"),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    out = capsys.readouterr().out
    assert "lint hard limit" in out
    assert "split the file" in out


def test_diagnose_first_load_budget(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    check = "Production build + first-load JavaScript budget"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            ("/logs", "First Load JS shared by all is 512 kB (budget 500 kB)"),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "first-load JavaScript budget" in capsys.readouterr().out


def test_diagnose_visual_regression(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    check = "e2e shard (3/4)"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            ("/logs", "toMatchImageSnapshot failed: baseline image differs"),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "visual regression" in capsys.readouterr().out


def test_diagnose_truncate_lint_delta_case(
    diag_gh: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    check = "lint truncate isolation"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            (
                "/logs",
                "truncate-isolation lint: comment-stripping regex captured CREATE TABLE delta",
            ),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "truncate-isolation lint" in capsys.readouterr().out


def test_diagnose_clock_lattice_gateway_case(
    diag_gh: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    check = "lint clock lattice"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            ("/logs", "lattice-vocabulary clock constant defined outside its family module"),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "clock-lattice lint" in capsys.readouterr().out


def test_diagnose_test_failure_uses_native_evidence(
    diag_gh: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check = "backend shard (4/16)"
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    calls: list[urllib.request.Request] = []
    open_url = urlopen_sequence([TrunkResponse({"state": "testing"})], calls)
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            (
                "/logs",
                "FAILED tests/components/agent/test_consumer_guard.py::test_consumer_guard_queue_backpressure",
            ),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=open_url) == 0
    )
    out = capsys.readouterr().out
    assert "unclassified CI failure" in out
    assert "known flake" not in out
    assert "Flaky DB" not in out
    assert "state=testing" in out
    assert len(calls) == 1
    assert "getSubmittedPullRequest" in calls[0].full_url


def test_diagnose_runner_network_flake(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    check = "backend (pytest + pyright)"
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", diag_runs()),
            ("/jobs", diag_jobs_payload(diag_job(check))),
            ("/logs", "apt-get install failed: archive cache is empty — no offline fallback"),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "runner-side network flake" in capsys.readouterr().out


def test_diagnose_reports_synthetic_test_pr(
    diag_gh: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view()),
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
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    out = capsys.readouterr().out
    assert "synthetic test PR #1875" in out
    assert "trunk-merge/pr-1871/" in out


def test_diagnose_stale_base(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view()),
            ("--json baseRefOid --jq", "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    assert "stale_base" in capsys.readouterr().out


def test_diagnose_json_machine_readable(diag_gh: Any, capsys: pytest.CaptureFixture[str]) -> None:
    host = diag_gh(
        [
            ("--json mergeable", diag_pr_view(checks=[diag_check("lint clock lattice")])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(
            ["1871", "--diagnose", "--json"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener()
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == "1871"
    assert payload["checks"][0]["classification"] == "deterministic lint failure"
    assert "issues" in payload and "synthetic_test_prs" in payload


def test_diagnose_exclusive_and_requires_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--diagnose", "--wait"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
    with pytest.raises(SystemExit):
        ci_utils.main(["--diagnose"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener())


def test_diagnose_merged_pr_reports_no_conflict_or_stale_base(
    diag_gh: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    view = json.loads(diag_pr_view(mergeable="UNKNOWN"))
    view["state"] = "MERGED"
    host = diag_gh(
        [
            ("--json mergeable", json.dumps(view)),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '{"total_count": 0, "workflow_runs": []}'),
            ("state=all", "[]"),
        ]
    )
    assert (
        ci_utils.main(["1871", "--diagnose"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener())
        == 0
    )
    out = capsys.readouterr().out
    assert "merge_conflict" not in out
    assert "stale_base" not in out


def test_ci_usage_prints_per_agent_rollup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "run_id": 1,
                "day": (datetime.now(UTC).date() - timedelta(days=1)).isoformat(),
                "agent_id": 5811,
                "linux_minutes": 100,
                "macos_minutes": 10,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(ci_utils, "DEFAULT_LEDGER", ledger)
    assert (
        ci_utils.main(["--ci-usage"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener())
        == 0
    )
    out = capsys.readouterr().out
    assert "#5811: 1 runs" in out
    assert "est $1.22" in out


def test_ci_usage_json_machine_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert (
        ci_utils.main(
            ["--ci-usage", "--json"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener()
        )
        == 0
    )
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["agent_id"] == 5811
    assert rows[0]["linux_minutes"] == 10


def test_ci_usage_exclusive_and_no_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--ci-usage"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener()
        )
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["--ci-usage", "--wait"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener()
        )


def test_limbo_runs_flags_aged_queued_zero_job_run() -> None:
    calls: list[list[str]] = []
    host = install_probe(
        [{"id": 91, "name": "Native root lifetime proof", "created_at": aged(910)}],
        {91: 0},
        calls=calls,
    )
    got = status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK)
    assert got is not None
    assert [r["id"] for r in got] == [91]
    assert got[0]["name"] == "Native root lifetime proof"
    assert got[0]["age_s"] >= 900
    assert 'select(.status == "queued")' in calls[0][-1]
    assert calls[1][2].endswith("/runs/91/jobs")


def test_limbo_runs_skips_fresh_and_progressing_runs() -> None:
    host = install_probe(
        [
            {"id": 92, "name": "fresh", "created_at": aged(120)},
            {"id": 93, "name": "progressing", "created_at": aged(1200)},
            {"id": 94, "name": "stuck", "created_at": aged(1200)},
        ],
        {93: 3, 94: 0},
    )
    result = status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK)
    assert result is not None
    assert [r["id"] for r in result] == [94]


def test_limbo_runs_probe_failures_never_read_as_limbo() -> None:
    host = install_probe([], {}, runs_rc=1)
    assert status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK) is None
    host = install_probe("not json", {}, runs_rc=0)
    assert status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK) is None
    host = install_probe(
        [{"id": 95, "name": "stuck", "created_at": aged(1200)}],
        {95: 0},
        jobs_rc={95: 1},
    )
    assert status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK) == []


def test_limbo_runs_skips_unparseable_created_at_and_non_int_id() -> None:
    calls: list[list[str]] = []
    host = install_probe(
        [
            {"id": 96, "name": "no-created", "created_at": None},
            {"id": "97", "name": "str-id", "created_at": aged(1200)},
            {"id": 98, "name": "garbage-created", "created_at": "yesterday"},
        ],
        {},
        calls=calls,
    )
    assert status.limbo_runs("abc1234", "o/r", host=host, clock=TEST_CLOCK) == []
    assert len(calls) == 1


def test_check_ci_attaches_limbo_to_pending(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = gh(
        [status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS")],
        scheduled=["Example proof A"],
    )
    has_workflows(True)
    stuck: list[status.LimboRun] = [{"id": 91, "name": "Example proof A", "age_s": 1500}]

    def fake_limbo(*_a: object, **_k: object) -> list[status.LimboRun]:
        return stuck

    monkeypatch.setattr(status, "limbo_runs", fake_limbo)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["Example proof A"]
    assert r.limbo == stuck
    assert "GitHub limbo" in r.summary()


def test_check_ci_empty_rollup_with_scheduled_run_probes_limbo(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = gh([], scheduled=["CI"])
    has_workflows(True)

    def fake_limbo(*_a: object, **_k: object) -> list[status.LimboRun]:
        return [{"id": 92, "name": "CI", "age_s": 1200}]

    monkeypatch.setattr(status, "limbo_runs", fake_limbo)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert r.limbo == [{"id": 92, "name": "CI", "age_s": 1200}]


def test_check_ci_limbo_probe_none_stays_plain_pending(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = gh([status_check_rollup_entry("backend", "SUCCESS")], scheduled=["CI"])
    has_workflows(True)

    def fake_limbo(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(status, "limbo_runs", fake_limbo)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert r.limbo == []


def test_query_once_json_includes_limbo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_check_ci(*_a: object, **_k: object) -> status.CIResult:
        return limbo_result(pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 700}])

    monkeypatch.setattr(status, "check_ci", fake_check_ci)
    assert (
        ci_utils.main(["1", "--json"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener())
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["limbo"] == [{"id": 91, "name": "CI", "age_s": 700}]


def test_report_limbo_names_once_per_key(capsys: pytest.CaptureFixture[str]) -> None:
    result = limbo_result(pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 660}])
    key = monitor.report_limbo(result, None)
    first = capsys.readouterr()
    assert key == (91,)
    assert "GitHub limbo" in first.err
    assert "(#91," in first.err
    assert monitor.report_limbo(result, key) == key
    assert capsys.readouterr().err == ""


def test_wait_names_limbo_runs_and_says_the_block(
    no_sleep: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_results_poller(
        monkeypatch,
        limbo_result(pending=["CI"], limbo=[{"id": 91, "name": "CI", "age_s": 1500}]),
    )
    assert (
        ci_utils.main(
            ["1243", "--wait", "--timeout", "1"],
            clock=no_sleep,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "GitHub limbo" in err
    assert "the block is" in err
    assert "91" in err


@pytest.mark.parametrize("mode", [[], ["--wait"], ["--merge"]])
def test_removed_force_flag_is_rejected(
    mode: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        ci_utils.main(
            ["42", *mode, "--force"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener()
        )
    assert error.value.code == 2
    assert "unrecognized arguments: --force" in capsys.readouterr().err


@pytest.mark.parametrize("pending", [["CI"], ["CI", "docs lint"]])
def test_limbo_never_enqueues_or_claims_green(
    no_sleep: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pending: list[str],
) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    install_results_poller(
        monkeypatch,
        limbo_result(pending=pending, limbo=[{"id": 91, "name": "CI", "age_s": 1500}]),
    )
    submissions: list[tuple[Any, ...]] = []

    def fake_submit_trunk(*a: object, **_k: object) -> int:
        submissions.append(a)
        return 0

    monkeypatch.setattr(owner_operations, "submit_trunk", fake_submit_trunk)
    assert (
        ci_utils.main(
            ["42", "--merge", "--timeout", "1"],
            clock=no_sleep,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
        == 1
    )
    assert submissions == []
    output = capsys.readouterr()
    assert "CI green" not in output.out
    assert "GitHub limbo" in output.err
    assert "remain pending, never green" in output.err


@pytest.mark.parametrize(
    "next_verdict", [CIStatus.PENDING, CIStatus.NO_WORKFLOW_RUNS, CIStatus.ERROR]
)
def test_owner_rechecks_genuine_green_before_submission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], next_verdict: CIStatus
) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    install_results_poller(
        monkeypatch, status.CIResult(CIStatus.ALL_PASSED), status.CIResult(next_verdict)
    )

    def fake_cooldown(*_a: object, **_k: object) -> int:
        return 0

    monkeypatch.setattr(owner_operations, "queue_cooldown_seconds", fake_cooldown)
    submissions: list[tuple[Any, ...]] = []

    def fake_submit_trunk(*a: object, **_k: object) -> int:
        submissions.append(a)
        return 0

    monkeypatch.setattr(owner_operations, "submit_trunk", fake_submit_trunk)
    assert ci_utils.main(
        ["42", "--merge"], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener()
    ) == (3 if next_verdict is CIStatus.ERROR else 1)
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
        ci_utils.main(
            ["42", "--merge", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
        )
    assert error.value.code == 2
    assert "exclusive" in capsys.readouterr().err
