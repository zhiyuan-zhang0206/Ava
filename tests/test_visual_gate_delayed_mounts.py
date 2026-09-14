"""Unit contracts for the delayed-mount waits in the shared visual matrix.

The preview gate's home/desktop structural check used to race the inspector
aside: it mounts only after settings and the active agent resolve, both
asynchronously, so a single-shot probe could read one aside where two are
expected. These contracts pin both states deterministically without a browser
— a scripted page stands in for Playwright — and keep the pre-fix single-shot
read on record as the red side of the contrast.
"""

from __future__ import annotations

import time
from typing import cast

import pytest
from playwright.sync_api import Page

from scripts import post_deploy_visual_matrix as matrix
from scripts.post_deploy_visual_matrix import measure_structure, structural_minimum_counts
from scripts.post_deploy_visual_policy import STRUCTURAL_SPECS
from tests.e2e._layout_assertions import (
    structural_failures,
    wait_for_minimum_visible_counts,
)

ASIDE = "#main-content aside"


class _ScriptedPage:
    """A Playwright Page stand-in driven by a scripted aside-count sequence.

    Every observation (one ``evaluate``) consumes the next count; the last
    scripted count repeats. A list argument is the visible-count probe; a dict
    argument is the structural probe, reproduced for the one dimension under
    test — a declared minimum the current count does not meet. The other probe
    dimensions are treated as clean.
    """

    def __init__(self, aside_counts: list[int]) -> None:
        self._aside_counts = aside_counts
        self._observations = 0
        self.count_probes = 0
        self.structural_probes = 0
        self.slept: list[float] = []

    def _next_count(self) -> int:
        index = min(self._observations, len(self._aside_counts) - 1)
        self._observations += 1
        return self._aside_counts[index]

    def evaluate(self, expression: str, arg: object) -> object:
        count = self._next_count()
        if isinstance(arg, list):
            self.count_probes += 1
            return dict.fromkeys(cast(list[str], arg), count)
        self.structural_probes += 1
        payload = cast(dict[str, object], arg)
        minimums = cast(dict[str, int], payload["minimumVisibleCounts"])
        return [
            {
                "kind": "visible-panel",
                "selector": selector,
                "detail": f"expected {minimum} visible in-viewport matches, found {count}",
                "bbox": None,
            }
            for selector, minimum in minimums.items()
            if count < minimum
        ]

    def wait_for_timeout(self, timeout: float) -> None:
        self.slept.append(timeout)
        time.sleep(timeout / 1000)


def test_pre_fix_single_shot_probe_races_the_mount_the_fix_passes() -> None:
    """One scripted timeline, both sides: the second aside appears at the third
    observation; the pre-fix call reads one, the fixed measurement reads two."""
    timeline = [1, 1, 2, 2]

    racing = _ScriptedPage(list(timeline))
    old_failures = structural_failures(
        cast(Page, racing),
        visible_selectors=(*cast(tuple[str, ...], STRUCTURAL_SPECS["home"]["visible"]), ASIDE),
        control_selectors=cast(tuple[str, ...], STRUCTURAL_SPECS["home"]["controls"]),
        nonempty_selectors=cast(tuple[str, ...], STRUCTURAL_SPECS["home"]["nonempty"]),
        minimum_visible_counts={ASIDE: 2},
    )
    assert [failure["detail"] for failure in old_failures] == [
        "expected 2 visible in-viewport matches, found 1"
    ]

    fixed = _ScriptedPage(list(timeline))
    assert (
        measure_structure(
            cast(Page, fixed), surface="home", viewport="desktop", spec=STRUCTURAL_SPECS["home"]
        )
        == []
    )


def test_never_mounting_panel_fails_with_wait_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(matrix, "DELAYED_MOUNT_WAIT_MS", 300)
    page = _ScriptedPage([1])

    failures = measure_structure(
        cast(Page, page), surface="home", viewport="desktop", spec=STRUCTURAL_SPECS["home"]
    )

    assert [(failure["kind"], failure["selector"]) for failure in failures] == [
        ("visible-panel", ASIDE)
    ]
    detail = failures[0]["detail"]
    assert detail.startswith("expected 2 visible in-viewport matches, found 1")
    assert "300 ms" in detail
    assert "observed 1" in detail
    assert len(page.slept) >= 1


def test_delayed_mount_wait_returns_immediately_when_counts_hold() -> None:
    page = _ScriptedPage([2])

    counts = wait_for_minimum_visible_counts(cast(Page, page), {ASIDE: 2})

    assert counts == {ASIDE: 2}
    assert page.count_probes == 1
    assert page.slept == []


def test_delayed_mount_wait_is_bounded() -> None:
    page = _ScriptedPage([1])

    counts = wait_for_minimum_visible_counts(
        cast(Page, page), {ASIDE: 2}, timeout_ms=120, poll_ms=30
    )

    assert counts == {ASIDE: 1}
    assert 2 <= page.count_probes <= 8
    assert len(page.slept) == page.count_probes - 1


def test_minimum_counts_scope_is_home_desktop_only() -> None:
    assert structural_minimum_counts("home", "desktop") == {ASIDE: 2}
    for surface in ("login", "fleet", "control", "run-timeline"):
        for viewport in ("desktop", "narrow"):
            assert structural_minimum_counts(surface, viewport) == {}


def test_other_surfaces_keep_the_single_shot_measurement() -> None:
    page = _ScriptedPage([1])

    failures = measure_structure(
        cast(Page, page), surface="login", viewport="desktop", spec=STRUCTURAL_SPECS["login"]
    )

    assert failures == []
    assert page.count_probes == 0
    assert page.structural_probes == 1
    assert page.slept == []
