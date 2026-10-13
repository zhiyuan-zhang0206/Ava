# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownMemberType = warning
"""Ci monitor cases: a run cancelled before matrix expansion must not poison the verdict."""

from __future__ import annotations

from typing import Any

from scripts.ci.pull_requests import status
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    CIStatus,
    _check,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    gh as gh,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    has_workflows as has_workflows,
)


def test_pre_expansion_cancelled_run_does_not_poison_the_head(gh: Any, has_workflows: Any) -> None:
    """PR #5262 (2026-10-12): a run cancelled before matrix expansion leaves two kinds of
    CANCELLED checks — named ones a successor run re-ran green (newest-per-name dedup), and
    template-named shard checks no successor can ever repeat. Without dropping the latter, the
    head reads FAILED forever and every channel depending on the verdict stalls silently."""
    cancelled_at = "2026-10-12T15:45:03Z"
    succeeded_at = "2026-10-12T15:58:00Z"
    gh(
        [
            _check(
                "backend shard (${{ matrix.group }}/16)", "CANCELLED", completed_at=cancelled_at
            ),
            _check("e2e shard (${{ matrix.group }}/4)", "CANCELLED", completed_at=cancelled_at),
            _check("backend (pytest + pyright)", "CANCELLED", completed_at=cancelled_at),
            _check("backend (pytest + pyright)", "SUCCESS", completed_at=succeeded_at),
            _check("backend shard (3/16)", "SUCCESS", completed_at=succeeded_at),
            _check("e2e shard (1/4)", "SUCCESS", completed_at=succeeded_at),
        ]
    )
    has_workflows(True)
    r = status.check_ci("5262")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.failed == []
    assert r.completed == [
        "backend (pytest + pyright)",
        "backend shard (3/16)",
        "e2e shard (1/4)",
    ]


def test_named_cancellation_without_a_successor_stays_red(gh: Any, has_workflows: Any) -> None:
    """Boundary of the placeholder drop: it keys on the name still holding an unexpanded
    expression, not on "no same-named successor". A real job cancelled without a settled rerun
    is missing evidence — red is visible and a rerun clears it; green there would merge
    unverified code."""
    gh([_check("backend shard (3/16)", "CANCELLED"), _check("backend shard (1/16)", "SUCCESS")])
    has_workflows(True)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.FAILED
    assert r.failed == [{"name": "backend shard (3/16)", "conclusion": "CANCELLED"}]
