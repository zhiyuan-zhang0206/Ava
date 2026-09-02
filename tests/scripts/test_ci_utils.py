from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci_utils.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("ci_utils", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_utils = _load_script()


def _no_cooldown(_: str, __: str) -> int:
    return 0


def _all_green(_: str | int, *, repo: str | None = None) -> Any:
    return ci_utils.CIResult(verdict=ci_utils.CIStatus.ALL_PASSED)


def _all_green_with_trunk_queue_check(_: str | int, *, repo: str | None = None) -> Any:
    return ci_utils.CIResult(
        verdict=ci_utils.CIStatus.ALL_PASSED,
        trunk_checks=[{"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS"}],
    )


def _no_sleep(_: float) -> None:
    return None


def _labels_runner(labels: list[str], calls: list[list[str]]):
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"labels": [{"name": label} for label in labels]}),
            stderr="",
        )

    return run


@pytest.mark.parametrize(
    ("priority", "expected"),
    [("urgent", 0), ("high", 10), ("medium", 100), ("low", 200)],
)
def test_trunk_priority_maps_cli_names_to_submit_values(priority: str, expected: int) -> None:
    assert ci_utils._trunk_priority(priority) == expected


def test_queue_resolution_prefers_flag_then_environment_then_trunk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_QUEUE", "trunk")
    assert ci_utils._resolve_queue("mergify") == "mergify"
    assert ci_utils._resolve_queue(None) == "trunk"

    monkeypatch.delenv("CI_QUEUE")
    assert ci_utils._resolve_queue(None) == "trunk"


class _TrunkResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> _TrunkResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _urlopen_sequence(
    responses: list[_TrunkResponse | urllib.error.URLError],
    seen_requests: list[urllib.request.Request],
):
    response_iter = iter(responses)

    def urlopen(request: urllib.request.Request, *, timeout: float) -> _TrunkResponse:
        seen_requests.append(request)
        response = next(response_iter)
        if isinstance(response, urllib.error.URLError):
            raise response
        return response

    return urlopen


def test_trunk_queue_submits_and_reports_merged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", _no_cooldown)
    monkeypatch.setattr(ci_utils, "check_ci", _all_green)
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _TrunkResponse({"accepted": True}),
                _TrunkResponse({"state": "pending"}),
                _TrunkResponse({"state": "merged"}),
            ],
            requests,
        ),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    rc = ci_utils._trunk_merge_flow(
        "42",
        "zhiyuan-zhang0206/Ava",
        "medium",
        every=1,
        timeout=0,
        token="trunk-token",  # noqa: S106 — test fixture
    )

    assert rc == 0
    assert capsys.readouterr().out == "PR #42 merged by the Trunk merge queue\n"
    assert requests[0].full_url == "https://api.trunk.io/v1/submitPullRequest"
    assert requests[0].get_header("X-api-token") == "trunk-token"
    assert requests[0].data is not None
    assert json.loads(cast(bytes, requests[0].data)) == {
        "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
        "pr": {"number": 42},
        "targetBranch": "main",
        "priority": 100,
        "noBatch": False,
    }
    assert requests[1].full_url == "https://api.trunk.io/v1/getSubmittedPullRequest"
    assert requests[1].data is not None
    assert json.loads(cast(bytes, requests[1].data)) == {
        "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
        "pr": {"number": 42},
        "targetBranch": "main",
    }


def test_trunk_queue_reports_failed_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "failed", "reason": "red CI"})], requests),
    )

    rc = ci_utils._watch_trunk_enqueue(
        "42",
        "zhiyuan-zhang0206/Ava",
        every=1,
        deadline=None,
        timeout=0,
        token="trunk-token",  # noqa: S106 — test fixture
    )

    assert rc == 1
    assert "red CI" in capsys.readouterr().err


def test_trunk_queue_times_out_while_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "testing"})], requests),
    )

    rc = ci_utils._watch_trunk_enqueue(
        "42",
        "zhiyuan-zhang0206/Ava",
        every=1,
        deadline=0,
        timeout=1,
        token="trunk-token",  # noqa: S106 — test fixture
    )

    assert rc == 1


def test_trunk_queue_stops_after_consecutive_status_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [urllib.error.URLError("offline")] * ci_utils.MAX_CONSECUTIVE_ERRORS,
            requests,
        ),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    rc = ci_utils._watch_trunk_enqueue(
        "42",
        "zhiyuan-zhang0206/Ava",
        every=1,
        deadline=None,
        timeout=0,
        token="trunk-token",  # noqa: S106 — test fixture
    )

    assert rc == 3
    assert len(requests) == ci_utils.MAX_CONSECUTIVE_ERRORS


def test_trunk_submit_retries_once_after_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [_TrunkResponse({}, status=503), _TrunkResponse({"accepted": True})], requests
        ),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    assert (
        ci_utils._submit_trunk(
            "42",
            "zhiyuan-zhang0206/Ava",
            "medium",
            token="trunk-token",  # noqa: S106 — test fixture
        )
        == 0
    )
    assert len(requests) == 2


def test_trunk_submit_returns_enqueue_error_after_second_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({}, status=500)] * 2, requests),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    assert (
        ci_utils._submit_trunk(
            "42",
            "zhiyuan-zhang0206/Ava",
            "medium",
            token="trunk-token",  # noqa: S106 — test fixture
        )
        == 4
    )
    assert len(requests) == 2


def test_trunk_merge_requires_token_before_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRUNK_API_TOKEN", raising=False)

    assert ci_utils.main(["42", "--merge", "--queue", "trunk"]) == 3
    assert "TRUNK_API_TOKEN" in capsys.readouterr().err


def test_real_trunk_queue_checks_do_not_block_all_green_predicate() -> None:
    result = ci_utils.CIResult(verdict=ci_utils.CIStatus.ALL_PASSED)

    ci_utils._partition_checks(
        [
            {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS", "conclusion": ""},
            {
                "name": "Trunk Merge Queue (main)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
        ],
        result,
    )

    assert result.pending == []
    assert result.failed == []
    assert result.trunk_checks == [
        {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS", "conclusion": ""},
        {
            "name": "Trunk Merge Queue (main)",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
        },
    ]


def test_json_query_includes_trunk_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_check = {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS"}
    monkeypatch.setattr(ci_utils, "check_ci", _all_green_with_trunk_queue_check)

    assert ci_utils._query_once("42", "zhiyuan-zhang0206/Ava", as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["trunk_checks"] == [queue_check]


def test_trunk_flow_refuses_to_submit_without_qa_approved_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", _no_cooldown)
    monkeypatch.setattr(ci_utils, "check_ci", _all_green)
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner([], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [_TrunkResponse({"accepted": True}), _TrunkResponse({"state": "merged"})], requests
        ),
    )

    assert (
        ci_utils._trunk_merge_flow(
            "42",
            "zhiyuan-zhang0206/Ava",
            "medium",
            every=1,
            timeout=0,
            token="trunk-token",  # noqa: S106 — test fixture
        )
        == 1
    )
    assert requests == []
    assert "PR #42 lacks the qa-approved label — not submitting to Trunk" in capsys.readouterr().err


def test_trunk_flow_submits_when_qa_approved_label_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", _no_cooldown)
    monkeypatch.setattr(ci_utils, "check_ci", _all_green)
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [_TrunkResponse({"accepted": True}), _TrunkResponse({"state": "merged"})], requests
        ),
    )

    assert (
        ci_utils._trunk_merge_flow(
            "42",
            "zhiyuan-zhang0206/Ava",
            "medium",
            every=1,
            timeout=0,
            token="trunk-token",  # noqa: S106 — test fixture
        )
        == 0
    )
    assert label_calls == [
        ["gh", "pr", "view", "42", "--repo", "zhiyuan-zhang0206/Ava", "--json", "labels"]
    ]
    assert len(requests) == 2


def test_trunk_flow_resumes_watch_when_submit_reports_already_queued(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", _no_cooldown)
    monkeypatch.setattr(ci_utils, "check_ci", _all_green)
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [_TrunkResponse({}, status=409), _TrunkResponse({"state": "merged"})], requests
        ),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    assert (
        ci_utils._trunk_merge_flow(
            "42",
            "zhiyuan-zhang0206/Ava",
            "medium",
            every=1,
            timeout=0,
            token="trunk-token",  # noqa: S106 — test fixture
        )
        == 0
    )
    assert len(requests) == 2
    assert "PR #42 already in the Trunk merge queue — resuming watch" in capsys.readouterr().err


class _PlainTextResponse(_TrunkResponse):
    """A 200 response whose body is plain text, like submitPullRequest's "OK"."""

    def read(self) -> bytes:
        return b"OK"


def test_trunk_submit_accepts_plain_text_ok_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """submitPullRequest answers 200 with a plain-text 'OK' body (verified live
    2026-09-01); a 200 must count as success regardless of body shape, or a
    successful submit would be misread as an error and retried into a 409."""
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", _no_cooldown)
    monkeypatch.setattr(ci_utils, "check_ci", _all_green)
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _PlainTextResponse({}),
                _TrunkResponse({"state": "merged"}),
            ],
            requests,
        ),
    )
    monkeypatch.setattr(ci_utils.time, "sleep", _no_sleep)

    rc = ci_utils._trunk_merge_flow(
        "42",
        "zhiyuan-zhang0206/Ava",
        "medium",
        every=1,
        timeout=0,
        token="trunk-token",  # noqa: S106 — test fixture
    )

    assert rc == 0
    assert capsys.readouterr().out == "PR #42 merged by the Trunk merge queue\n"
