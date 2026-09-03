"""Tests for scripts/qa_gate.py — the exact-head QA status publisher."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "qa_gate.py"
_spec = importlib.util.spec_from_file_location("qa_gate_under_test", _SCRIPT)
assert _spec and _spec.loader
qa_gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = qa_gate
_spec.loader.exec_module(qa_gate)

sys.path.insert(0, str(_REPO_ROOT))
from scripts import qa_receipt  # noqa: E402

SHA = "d" * 40


def _receipt_comment(verdict: str = "approved") -> dict:
    receipt = {
        "ava_qa_version": 1,
        "pr_number": 42,
        "head_sha": SHA,
        "verdict": verdict,
        "asserted_ava_reviewer": "3242",
        "time": "2026-09-04T04:00:00+08:00",
    }
    return {
        "id": 1001,
        "user": {"id": qa_receipt.QA_ACCOUNT_ID},
        "body": "```ava-qa\n" + json.dumps(receipt) + "\n```",
        "updated_at": "2026-09-04T04:00:00Z",
    }


def _pull(head_sha: str = SHA) -> dict:
    return {
        "number": 42,
        "user": {"id": 111, "type": "User"},
        "head": {
            "sha": head_sha,
            "ref": "ava-405-qa",
            "repo": {"full_name": "zhiyuan-zhang0206/Ava"},
        },
        "base": {"ref": "main", "repo": {"full_name": "zhiyuan-zhang0206/Ava"}},
        "draft": False,
        "labels": [{"name": "qa-approved"}],
    }


class _Harness:
    """Fake gh-api backend.

    `seq_paths`: endpoint -> list of responses consumed in call order
    (used for `_api` reads like the PR object).
    `pages_paths`: endpoint -> list-of-pages payload returned AS-IS on every
    call (used for `_records` reads, which flatten one page level).
    """

    def __init__(self, seq_paths: dict[str, Any], pages_paths: dict[str, Any]) -> None:
        self.seq_paths = seq_paths
        self.pages_paths = pages_paths
        self.status_posts: list[dict] = []
        self.calls: list[str] = []

    def __call__(self, path: str, *args: str) -> Any:
        self.calls.append(path)
        if path.startswith("statuses/"):
            return []
        for key, payload in self.pages_paths.items():
            if path.startswith(key):
                return payload
        for key, responses in self.seq_paths.items():
            if path == key:
                return responses.pop(0)
        raise AssertionError(f"unexpected api path: {path}")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _run(seq_paths: dict[str, Any], pages_paths: dict[str, Any]) -> _Harness:
        h = _Harness(seq_paths, pages_paths)

        def _api(path: str, *args: str) -> Any:
            payload = h(path, *args)
            if path.startswith("statuses/") and "--method" in args:
                kv = dict(item.split("=", 1) for item in args if "=" in item)
                h.status_posts.append({"state": kv["state"], "description": kv["description"]})
                return {}
            return payload

        monkeypatch.setattr(qa_gate, "_api", _api)
        return h

    return _run


def _base(comments: list[dict], reviews: list[dict] | None = None) -> tuple[dict, dict]:
    seq = {"pulls/42": [_pull(), _pull()]}
    pages = {
        "commits/" + SHA + "/pulls": [[{"number": 42, "state": "open", "head": {"sha": SHA}}]],
        "issues/42/comments": [comments],
        "pulls/42/reviews": [reviews if reviews is not None else []],
    }
    return seq, pages


def test_happy_path_publishes_success(run: Any) -> None:
    h = run(*_base([_receipt_comment()]))
    qa_gate.evaluate(42)
    assert h.status_posts[-1] == {
        "state": "success",
        "description": "Current HEAD has authorized QA evidence",
    }


def test_missing_receipt_publishes_failure(run: Any) -> None:
    h = run(*_base([]))
    qa_gate.evaluate(42)
    assert h.status_posts[-1] == {
        "state": "failure",
        "description": "Missing current-head QA approval",
    }


def test_revoked_receipt_publishes_failure(run: Any) -> None:
    h = run(*_base([_receipt_comment("revoked")]))
    qa_gate.evaluate(42)
    assert h.status_posts[-1]["state"] == "failure"


def test_head_change_publishes_failure_before_evidence_read(run: Any) -> None:
    h = run({"pulls/42": [_pull(), _pull("e" * 40)]}, {})
    qa_gate.evaluate(42)
    assert h.status_posts[-1] == {
        "state": "failure",
        "description": "HEAD changed while QA was evaluated",
    }


def test_shared_head_publishes_failure(run: Any) -> None:
    h = run(
        {"pulls/42": [_pull(), _pull()]},
        {
            "commits/" + SHA + "/pulls": [
                [
                    {"number": 42, "state": "open", "head": {"sha": SHA}},
                    {"number": 43, "state": "open", "head": {"sha": SHA}},
                ]
            ]
        },
    )
    qa_gate.evaluate(42)
    assert h.status_posts[-1]["state"] == "failure"
    assert "Ambiguous shared HEAD" in h.status_posts[-1]["description"]


def test_evidence_is_read_after_the_head_guards_immediately_before_write(run: Any) -> None:
    """The debounce contract (#2443): the verdict is computed from a fresh
    evidence read made AFTER the head checks, as late as possible before the
    terminal write — a verdict from an older snapshot can no longer be the
    last write."""
    h = run(*_base([_receipt_comment()]))
    qa_gate.evaluate(42)
    head_guard_idx = next(i for i, c in enumerate(h.calls) if c.startswith("commits/" + SHA))
    comments_idx = next(i for i, c in enumerate(h.calls) if c.startswith("issues/42/comments"))
    assert head_guard_idx < comments_idx
    assert h.status_posts[0]["state"] == "pending"
    assert h.status_posts[-1]["state"] == "success"
