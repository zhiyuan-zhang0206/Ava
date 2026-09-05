"""Approval labels and author-controlled names cannot manufacture QA evidence."""

import json
from typing import Any

import pytest

from scripts import qa_gate
from scripts.qa_receipt import QA_ACCOUNT_ID, REPOSITORY, TRUNK_ACCOUNT_ID, approved


def _pr() -> dict[str, Any]:
    return {
        "number": 42,
        "user": {"id": QA_ACCOUNT_ID, "type": "User"},
        "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": REPOSITORY}},
        "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
        "draft": False,
        "labels": [{"name": "qa-approved"}],
    }


def _comment(pr: dict[str, Any], verdict: str = "approved") -> dict[str, Any]:
    body = {
        "ava_qa_version": 1,
        "pr_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "verdict": verdict,
        "asserted_ava_reviewer": "405",
    }
    return {
        "id": 1,
        "user": {"id": QA_ACCOUNT_ID},
        "updated_at": "2026-09-03T00:00:00Z",
        "body": "```ava-qa\n" + json.dumps(body) + "\n```",
    }


def test_current_receipt_requires_label_and_authentication() -> None:
    pr = _pr()
    receipt = _comment(pr)
    assert approved(pr, [receipt], [])
    receipt["user"]["id"] = 1
    assert not approved(pr, [receipt], [])
    receipt["user"]["id"] = QA_ACCOUNT_ID
    pr["labels"] = []
    assert not approved(pr, [receipt], [])


def test_adopted_json_receipt_allows_only_string_time_and_note_metadata() -> None:
    pr = _pr()
    receipt = _comment(pr)
    value = {
        "ava_qa_version": 1,
        "pr_number": 42,
        "head_sha": "a" * 40,
        "verdict": "approved",
        "asserted_ava_reviewer": "3242",
        "time": "2026-09-02T18:34:16Z",
        "note": "current-SHA receipt",
    }
    receipt["body"] = "```json\n" + json.dumps(value) + "\n```"
    assert approved(pr, [receipt], [])
    receipt["body"] = "```json\n" + json.dumps(value | {"bypass": True}) + "\n```"
    assert not approved(pr, [receipt], [])
    receipt["body"] = "```json\n" + json.dumps(value | {"time": 1}) + "\n```"
    assert not approved(pr, [receipt], [])


def test_synchronize_does_not_carry_old_approval() -> None:
    pr = _pr()
    receipt = _comment(pr)
    pr["head"]["sha"] = "b" * 40
    assert not approved(pr, [receipt], [])
    assert not approved(pr, [], [])


def test_edit_delete_and_revocation_recompute_evidence() -> None:
    pr = _pr()
    receipt = _comment(pr)
    assert approved(pr, [receipt], [])
    assert not approved(pr, [], [])
    assert not approved(pr, [_comment(pr, "revoked")], [])
    revoked = _comment(pr, "revoked")
    revoked.update(id=2, updated_at="2026-09-03T00:00:01Z")
    assert not approved(pr, [receipt, revoked], [])


@pytest.mark.parametrize("state", ["DISMISSED", "CHANGES_REQUESTED"])
def test_review_veto_does_not_use_old_submitted_time(state: str) -> None:
    pr = _pr()
    review = {
        "id": 3,
        "user": {"id": QA_ACCOUNT_ID},
        "commit_id": pr["head"]["sha"],
        "state": state,
        "submitted_at": "2026-09-02T00:00:00Z",
    }
    assert not approved(pr, [_comment(pr)], [review])
    review["state"] = "APPROVED"
    assert approved(pr, [], [review])


@pytest.mark.parametrize("suffix", ["", "-bisection"])
def test_forged_queue_prefix_cannot_bypass_qa(suffix: str) -> None:
    pr = _pr()
    pr["labels"] = []
    pr["draft"] = True
    # PR #1795 used this ref with the bisection suffix after splitting a red batch.
    pr["head"]["ref"] = "trunk-merge/pr-1790/1449ac46-d2cb-40d1-922a-e3a11e4e7bca" + suffix
    assert not approved(pr, [], [])
    pr["user"] = {"id": TRUNK_ACCOUNT_ID, "type": "Bot"}
    assert approved(pr, [], [])
    pr["head"]["repo"]["full_name"] = "attacker/Ava"
    assert not approved(pr, [], [])


@pytest.mark.parametrize("suffix", ["", "-bisection"])
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("user", "id"), QA_ACCOUNT_ID),
        (("user", "type"), "User"),
        (("head", "repo", "full_name"), "attacker/Ava"),
        (("base", "repo", "full_name"), "attacker/Ava"),
        (("base", "ref"), "feature"),
        (("draft",), False),
        (("draft",), 1),
    ],
)
def test_queue_exemption_requires_every_trust_guard(
    suffix: str, path: tuple[str, ...], value: Any
) -> None:
    pr = _pr()
    pr.update(user={"id": TRUNK_ACCOUNT_ID, "type": "Bot"}, draft=True, labels=[])
    pr["head"]["ref"] = "trunk-merge/pr-1790/1449ac46-d2cb-40d1-922a-e3a11e4e7bca" + suffix
    field = pr
    for key in path[:-1]:
        field = field[key]
    field[path[-1]] = value
    assert not approved(pr, [], [])


@pytest.mark.parametrize(
    "suffix", ["-bisection-extra", "-bisection-bisection", "/extra", "\n", "-BISECTION"]
)
def test_queue_exemption_rejects_unrecognized_suffixes(suffix: str) -> None:
    pr = _pr()
    pr.update(user={"id": TRUNK_ACCOUNT_ID, "type": "Bot"}, draft=True, labels=[])
    pr["head"]["ref"] = "trunk-merge/pr-1790/1449ac46-d2cb-40d1-922a-e3a11e4e7bca" + suffix
    assert not approved(pr, [], [])


def test_sha_global_status_refuses_shared_open_pr_head(monkeypatch: pytest.MonkeyPatch) -> None:
    pr = _pr()
    writes: list[tuple[str, ...]] = []

    def api(path: str, *args: str) -> Any:
        if path.startswith("statuses/"):
            writes.append(args)
            return {}
        assert path == "pulls/42"
        return pr

    def records(path: str) -> list[dict[str, Any]]:
        if path.startswith("issues/"):
            return [_comment(pr)]
        if path.startswith("commits/"):
            return [{"number": number, "state": "open", "head": pr["head"]} for number in (42, 43)]
        return []

    monkeypatch.setattr(qa_gate, "_api", api)
    monkeypatch.setattr(qa_gate, "_records", records)
    qa_gate.evaluate(42)
    assert any("state=failure" in args for args in writes)
    assert not any("state=success" in args for args in writes)
