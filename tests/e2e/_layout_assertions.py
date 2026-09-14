"""Reusable Playwright measurements for rendered layout contracts."""

from __future__ import annotations

import time
from typing import TypedDict, cast

from playwright.sync_api import Page


class LayoutFailure(TypedDict):
    """One machine-readable structural defect."""

    kind: str
    selector: str
    detail: str
    bbox: dict[str, float] | None


_COUNT_VISIBLE_MATCHES = """
const countVisibleMatches = (selector) => {
  return Array.from(document.querySelectorAll(selector)).filter((element) => {
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
      style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0 &&
      rect.left >= -1 && rect.top >= -1 &&
      rect.right <= window.innerWidth + 1 && rect.bottom <= window.innerHeight + 1;
  }).length;
};
"""

_VISIBLE_COUNT_PROBE = (
    """
(selectors) => {
"""
    + _COUNT_VISIBLE_MATCHES
    + """
  const counts = {};
  for (const selector of selectors) {
    counts[selector] = countVisibleMatches(selector);
  }
  return counts;
}
"""
)

_STRUCTURAL_PROBE = (
    """
({ visibleSelectors, controlSelectors, nonemptySelectors, minimumVisibleCounts }) => {
"""
    + _COUNT_VISIBLE_MATCHES
    + """
  const failures = [];
  const bbox = (rect) => ({
    x: rect.x, y: rect.y, width: rect.width, height: rect.height,
    right: rect.right, bottom: rect.bottom,
  });
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
      style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const one = (selector, kind) => {
    const element = document.querySelector(selector);
    if (!element) {
      failures.push({ kind, selector, detail: "selector did not match", bbox: null });
      return null;
    }
    return element;
  };

  const scrolling = document.scrollingElement;
  if (!scrolling || scrolling.scrollWidth > scrolling.clientWidth + 1) {
    failures.push({
      kind: "horizontal-overflow", selector: "document.scrollingElement",
      detail: scrolling ? `${scrolling.scrollWidth - scrolling.clientWidth}px overflow` : "missing",
      bbox: null,
    });
  }

  for (const selector of visibleSelectors) {
    const element = one(selector, "visible-panel");
    if (!element) continue;
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const rect = element.getBoundingClientRect();
    if (!visible(element) || rect.left < -1 || rect.top < -1 ||
        rect.right > window.innerWidth + 1 || rect.bottom > window.innerHeight + 1) {
      failures.push({
        kind: "visible-panel", selector, detail: "panel is hidden or outside the viewport",
        bbox: bbox(rect),
      });
    }
  }

  for (const selector of controlSelectors) {
    const element = one(selector, "occluded-control");
    if (!element) continue;
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    const rect = element.getBoundingClientRect();
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    if (!visible(element) || !hit || (hit !== element && !element.contains(hit))) {
      failures.push({
        kind: "occluded-control", selector,
        detail: hit ? `covered by ${hit.tagName.toLowerCase()}` : "no center-point hit",
        bbox: bbox(rect),
      });
    }
  }

  for (const selector of nonemptySelectors) {
    const element = one(selector, "empty-block");
    if (!element) continue;
    const hasText = (element.textContent || "").trim().length > 0;
    const hasVisibleChild = Array.from(element.children).some((child) => visible(child));
    if (!hasText && !hasVisibleChild) {
      failures.push({
        kind: "empty-block", selector, detail: "expected content container is empty",
        bbox: bbox(element.getBoundingClientRect()),
      });
    }
  }

  for (const [selector, minimum] of Object.entries(minimumVisibleCounts)) {
    const count = countVisibleMatches(selector);
    if (count < minimum) {
      failures.push({
        kind: "visible-panel", selector,
        detail: `expected ${minimum} visible in-viewport matches, found ${count}`,
        bbox: null,
      });
    }
  }
  return failures;
}
"""
)

_SETTLED_PREDICATE = """
({ readySelector }) => {
  const ready = document.querySelector(readySelector);
  if (!ready) return false;
  const unsettled = [
    ...document.querySelectorAll(
      '[aria-busy="true"], .animate-spin, [data-testid*="skeleton"], [data-testid$="loading"]'
    ),
  ];
  return !unsettled.some((element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  });
}
"""


def wait_for_layout_settled(page: Page, ready_selector: str, *, timeout_ms: int = 60_000) -> None:
    """Wait until the page is ready and no visible skeleton or spinner remains."""
    page.wait_for_function(
        _SETTLED_PREDICATE,
        arg={"readySelector": ready_selector},
        timeout=timeout_ms,
    )


def visible_in_viewport_counts(page: Page, selectors: tuple[str, ...]) -> dict[str, int]:
    """Count visible in-viewport matches per selector.

    The same rules as the structural probe's minimum-count check, from the one
    shared predicate — a wait built on these counts and the probe's verdict can
    never disagree about what "visible in viewport" means.
    """
    counts = page.evaluate(_VISIBLE_COUNT_PROBE, list(selectors))
    return cast(dict[str, int], counts)


def wait_for_minimum_visible_counts(
    page: Page,
    minimum_counts: dict[str, int],
    *,
    timeout_ms: int = 5_000,
    poll_ms: int = 100,
) -> dict[str, int]:
    """Wait, bounded, until every selector reaches its minimum visible count.

    A panel that mounts only after async data resolves can race an immediate
    structural probe, so a caller that declares minimums first waits for them.
    Returns the counts observed when the wait ended: satisfying the minimums on
    the happy path (the first observation already satisfies them when nothing
    is delayed — no sleep), otherwise the still-short counts at the deadline.
    """
    selectors = tuple(minimum_counts)
    counts = visible_in_viewport_counts(page, selectors)
    deadline = time.monotonic() + timeout_ms / 1000
    while not all(counts[selector] >= minimum for selector, minimum in minimum_counts.items()):
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(poll_ms)
        counts = visible_in_viewport_counts(page, selectors)
    return counts


def structural_failures(
    page: Page,
    *,
    visible_selectors: tuple[str, ...] = (),
    control_selectors: tuple[str, ...] = (),
    nonempty_selectors: tuple[str, ...] = (),
    minimum_visible_counts: dict[str, int] | None = None,
) -> list[LayoutFailure]:
    """Measure overflow, viewport placement, occlusion, and expected content.

    Panels and controls are scrolled into view (nearest) before the viewport
    checks: the gate pages scroll inside internal containers, and a panel
    below the fold is reachable content, not a defect.
    """
    result = page.evaluate(
        _STRUCTURAL_PROBE,
        {
            "visibleSelectors": visible_selectors,
            "controlSelectors": control_selectors,
            "nonemptySelectors": nonempty_selectors,
            "minimumVisibleCounts": minimum_visible_counts or {},
        },
    )
    return cast(list[LayoutFailure], result)


def no_document_horizontal_overflow(page: Page) -> bool:
    """Return whether the document fits its horizontal viewport."""
    return not any(
        failure["kind"] == "horizontal-overflow" for failure in structural_failures(page)
    )


def element_within_viewport(page: Page, selector: str) -> bool:
    """Return whether one required panel is visible and wholly in the viewport."""
    return not structural_failures(page, visible_selectors=(selector,))


def element_within_parent(page: Page, selector: str) -> bool:
    """Return whether an element's scroll width fits its parent."""
    return bool(
        page.evaluate(
            """(selector) => {
              const element = document.querySelector(selector);
              return Boolean(element && element.parentElement &&
                element.scrollWidth <= element.parentElement.clientWidth + 1);
            }""",
            selector,
        )
    )


def all_elements_within_parents(page: Page, selector: str) -> bool:
    """Return whether at least one match exists and every match fits its parent."""
    return bool(
        page.evaluate(
            """(selector) => {
              const elements = document.querySelectorAll(selector);
              return elements.length > 0 && Array.from(elements).every((element) =>
                element.parentElement && element.scrollWidth <= element.parentElement.clientWidth + 1
              );
            }""",
            selector,
        )
    )
