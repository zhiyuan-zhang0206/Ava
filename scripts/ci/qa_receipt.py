"""Exact-head QA authority from GitHub records, not an enduring label.

The allowed shared GitHub account authenticates account authorization only.
asserted_ava_reviewer is attribution supplied by that account, not independent
proof of an Ava agent's identity. No secret or signing service is introduced.
"""

import json
import re
from typing import Any

QA_ACCOUNT_ID = 87293881
TRUNK_ACCOUNT_ID = 85644782
REPOSITORY = "zhiyuan-zhang0206/Ava"
_RECEIPT = re.compile(r"\A```(?:ava-qa|json)\n(.*?)\n```\s*\Z", re.DOTALL)
_FIELDS = {"ava_qa_version", "pr_number", "head_sha", "verdict", "asserted_ava_reviewer"}
_METADATA = {"time", "note"}


def trusted_queue_pr(pr: dict[str, Any]) -> bool:
    """A branch prefix alone is never a queue credential."""
    return (
        pr["user"]["id"] == TRUNK_ACCOUNT_ID
        and pr["user"]["type"] == "Bot"
        and pr["head"]["repo"]["full_name"] == REPOSITORY
        and pr["base"]["repo"]["full_name"] == REPOSITORY
        and pr["base"]["ref"] == "main"
        and pr["draft"] is True
        and re.fullmatch(r"trunk-merge/pr-[0-9]+/[0-9a-f-]{36}(?:-bisection)?", pr["head"]["ref"])
        is not None
    )


def _receipt(body: str, pr: dict[str, Any]) -> str | None:
    match = _RECEIPT.fullmatch(body)
    if match is None:
        return None
    try:
        value = json.loads(match[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not set(value) >= _FIELDS or set(value) - _FIELDS - _METADATA:
        return None
    if any(not isinstance(value[key], str) for key in _METADATA & set(value)):
        return None
    if (
        type(value["ava_qa_version"]) is not int
        or value["ava_qa_version"] != 1
        or type(value["pr_number"]) is not int
        or value["pr_number"] != pr["number"]
        or value["head_sha"] != pr["head"]["sha"]
        or not re.fullmatch(r"[0-9a-f]{40}", str(value["head_sha"]))
        or value["verdict"] not in ("approved", "revoked")
        or not isinstance(value["asserted_ava_reviewer"], str)
        or not value["asserted_ava_reviewer"].strip()
    ):
        return None
    return value["verdict"]


def approved(
    pr: dict[str, Any],
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> bool:
    """Require label AND latest current-head authorized evidence.

    Deletion removes a receipt from the fetched record set. Edits replace its
    content/time. A dismissed or changes-requested current-head review vetoes
    approval until a later explicit receipt/review. Records for another head
    are never carried forward on synchronize.
    """
    if trusted_queue_pr(pr):
        return True
    if not any(label["name"] == "qa-approved" for label in pr["labels"]):
        return False
    evidence: list[tuple[str, int, bool]] = []
    for comment in comments:
        if comment["user"]["id"] != QA_ACCOUNT_ID:
            continue
        verdict = _receipt(comment["body"], pr)
        if verdict is not None:
            evidence.append((comment["updated_at"], comment["id"], verdict == "approved"))
    relevant_reviews = [
        review
        for review in reviews
        if review["user"]["id"] == QA_ACCOUNT_ID
        and review["commit_id"] == pr["head"]["sha"]
        and review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
    ]
    # GitHub's review submitted_at is NOT its dismissal timestamp. A current
    # dismissal must not lose to a receipt merely because it was submitted
    # earlier. Require a new actual review to clear an existing review veto.
    if (
        relevant_reviews
        and max(relevant_reviews, key=lambda item: item["id"])["state"] != "APPROVED"
    ):
        return False
    for review in relevant_reviews:
        if review["user"]["id"] != QA_ACCOUNT_ID or review["commit_id"] != pr["head"]["sha"]:
            continue
        state = review["state"]
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            evidence.append((review["submitted_at"], review["id"], state == "APPROVED"))
    return bool(evidence) and max(evidence)[2]
