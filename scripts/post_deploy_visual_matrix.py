"""Shared five-surface capture matrix for the post-deploy visual gate.

One engine-agnostic module, two consumers: the deployment gate runs it on
the macmini host against the live gate, the CI preview gate runs it on
GitHub runners against the PR preview build. Escalation, wave state, and
artifact layout stay with the consumers.
"""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Route

from scripts.post_deploy_visual_browser_js import OVERLAY, PIXEL_DIFF
from scripts.post_deploy_visual_fixtures import FIXTURES, INERT_EVENT_SOURCE, RUN_TIMELINE
from scripts.post_deploy_visual_policy import (
    CHANNEL_DELTA_THRESHOLD,
    STRUCTURAL_SPECS,
    THEMES,
    VIEWPORTS,
    DiffRegion,
    classify_stable_diff,
    route_for_surface,
)
from tests.e2e._layout_assertions import structural_failures, wait_for_layout_settled


def load_ignore_registry(path: Path) -> dict[str, object]:
    """Read the known-ignore registry with its version contract."""
    registry = json.loads(path.read_text())
    if registry["version"] != 1:
        raise ValueError("unsupported known-ignore registry version")
    return registry


def pixel_crops(surface: str, viewport: str) -> dict[str, str]:
    if surface == "login":
        return {"login-card": "form"}
    if surface == "control":
        return {
            "control-header": "header",
            "control-nav": "[aria-label='Control sections']",
        }
    if surface == "home":
        crops = {
            "home-header": "#main-content header",
            "home-composer": "[data-testid='composer']",
        }
        if viewport == "desktop":
            crops["home-sidebar"] = "#main-content aside"
        return crops
    return {}


def fixture_for(path: str) -> object | None:
    if path == "/api/agents/1/run-timeline":
        return RUN_TIMELINE
    return FIXTURES.get(path)


def guard_requests(page: Page, *, surface: str) -> None:
    """Abort non-GET traffic and serve fixed fixtures for data surfaces."""
    fixture_surface = surface in {"home", "fleet", "run-timeline"}

    def route_all(route: Route) -> None:
        # Read-only hard boundary: every non-GET request the page fires is
        # aborted, no matter which endpoint it targets. Only GET traffic may
        # reach the network, and data-surface GETs are served fixed fixtures.
        if route.request.method != "GET":
            route.abort("blockedbyclient")
            return
        path = urlparse(route.request.url).path
        if path == "/__ava/deploy-state":
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"status":"inactive","generation":null}',
            )
            return
        if not path.startswith("/api/"):
            route.continue_()
            return
        if path == "/api/auth/check":
            # The gate pins the auth context per surface instead of trusting the
            # live session, so every matrix combo renders deterministically:
            # fixture-fed data surfaces render the authenticated layout, the
            # login surface always renders the form (a live authenticated
            # session would redirect away from /login), and control gets the
            # real read-only GET so a dead cookie fails loudly there.
            if fixture_surface or surface == "login":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"authenticated": surface != "login"}),
                )
            else:
                route.continue_()
            return
        fixture = fixture_for(path) if fixture_surface else None
        if fixture is not None:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(fixture))
            return
        if fixture_surface:
            route.fulfill(
                status=404, content_type="application/json", body='{"detail":"not found"}'
            )
            return
        route.continue_()

    page.route("**/*", route_all)


def new_matrix_context(
    browser: Browser,
    *,
    viewport: str,
    theme: str,
    cookie_state: dict[str, object] | None,
) -> BrowserContext:
    width, height = VIEWPORTS[viewport]
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=cast(Any, theme),
        locale="en-US",
        timezone_id="UTC",
        storage_state=cast(Any, cookie_state),
    )
    context.add_init_script(
        f"localStorage.setItem('theme', {json.dumps(theme)}); {INERT_EVENT_SOURCE}"
    )
    return context


def ignore_selectors(surface: str, registry: dict[str, object]) -> tuple[str, ...]:
    common = cast(list[dict[str, str]], registry["common"])
    routes = cast(dict[str, list[dict[str, str]]], registry["routes"])
    return tuple(entry["selector"] for entry in [*common, *routes.get(surface, [])])


def diff_regions(page: Page, actual: bytes, expected: bytes) -> tuple[int, list[DiffRegion]]:
    result = page.evaluate(
        PIXEL_DIFF,
        {
            "actualPng": base64.b64encode(actual).decode("ascii"),
            "expectedPng": base64.b64encode(expected).decode("ascii"),
            "threshold": CHANNEL_DELTA_THRESHOLD,
        },
    )
    regions = [DiffRegion(**region) for region in result["regions"]]
    return int(result["totalPixels"]), regions


def write_overlay(path: Path, page: Page, png: bytes, regions: tuple[DiffRegion, ...]) -> None:
    encoded = page.evaluate(
        OVERLAY,
        {"png": base64.b64encode(png).decode("ascii"), "regions": [asdict(r) for r in regions]},
    )
    path.write_bytes(base64.b64decode(encoded))


def capture_crop(
    page: Page,
    *,
    selector: str,
    key: str,
    ready_selector: str,
    ignore_selectors: tuple[str, ...],
    captures: Path,
    golden: Path,
) -> dict[str, object]:
    locator = page.locator(selector).first
    if locator.count() == 0 or not locator.is_visible():
        raise RuntimeError(f"pixel crop is missing or hidden: {selector}")
    masks = [page.locator(value) for value in ignore_selectors if page.locator(value).count()]
    current = []
    for frame in (1, 2):
        wait_for_layout_settled(page, ready_selector)
        path = captures / f"{key}-current-{frame}.png"
        locator.screenshot(path=path, animations="disabled", caret="hide", mask=masks)
        current.append(path.read_bytes())
        if frame == 1:
            page.wait_for_timeout(1000)
    golden_paths = [golden / f"{key}-golden-{frame}.png" for frame in (1, 2)]
    if not all(path.is_file() for path in golden_paths):
        return {"surface": key.rsplit("-", 2)[0], "status": "baseline-missing", "drifted": True}
    frame_results = []
    for frame, (actual, previous) in enumerate(zip(current, golden_paths, strict=True), 1):
        previous_artifact = captures / f"{key}-previous-{frame}.png"
        shutil.copy2(previous, previous_artifact)
        frame_results.append(diff_regions(page, actual, previous.read_bytes()))
    total_pixels = max(frame_results[0][0], frame_results[1][0])
    stable = classify_stable_diff(
        frame_results[0][1],
        frame_results[1][1],
        total_pixels=total_pixels,
    )
    if stable.stable_regions:
        write_overlay(captures / f"{key}-diff.png", page, current[1], stable.stable_regions)
    return {
        "surface": key.rsplit("-", 2)[0],
        "status": "drift" if stable.drifted else "match",
        "drifted": stable.drifted,
        "changed_pixels": stable.changed_pixels,
        "total_pixels": total_pixels,
        "ratio": stable.ratio,
        "stable_regions": [asdict(region) for region in stable.stable_regions],
    }


def inspect_combination(
    browser: Browser,
    *,
    base_url: str,
    surface: str,
    viewport: str,
    theme: str,
    cookie_state: dict[str, object] | None,
    registry: dict[str, object],
    captures: Path,
    golden: Path,
) -> dict[str, object]:
    context = new_matrix_context(browser, viewport=viewport, theme=theme, cookie_state=cookie_state)
    try:
        page = context.new_page()
        guard_requests(page, surface=surface)
        route = route_for_surface(surface)
        page.goto(f"{base_url.rstrip('/')}{route}", wait_until="domcontentloaded")
        spec = STRUCTURAL_SPECS[surface]
        ready_selector = cast(str, spec["ready"])
        wait_for_layout_settled(page, ready_selector)
        visible = cast(tuple[str, ...], spec["visible"])
        if surface == "home" and viewport == "desktop":
            visible += ("#main-content aside",)
        failures = structural_failures(
            page,
            visible_selectors=visible,
            control_selectors=cast(tuple[str, ...], spec["controls"]),
            nonempty_selectors=cast(tuple[str, ...], spec["nonempty"]),
            minimum_visible_counts={"#main-content aside": 2}
            if surface == "home" and viewport == "desktop"
            else None,
        )
        diffs = []
        for crop_surface, selector in pixel_crops(surface, viewport).items():
            key = f"{crop_surface}-{viewport}-{theme}"
            result = capture_crop(
                page,
                selector=selector,
                key=key,
                ready_selector=ready_selector,
                ignore_selectors=ignore_selectors(surface, registry),
                captures=captures,
                golden=golden,
            )
            result["crop_surface"] = crop_surface
            diffs.append(result)
        return {
            "surface": surface,
            "route": route,
            "viewport": viewport,
            "theme": theme,
            "structural_failures": failures,
            "pixel_diffs": diffs,
        }
    finally:
        context.close()


def run_matrix(
    browser: Browser,
    *,
    base_url: str,
    cookie_state: dict[str, object] | None,
    registry: dict[str, object],
    captures: Path,
    golden: Path,
) -> list[dict[str, object]]:
    """Run every surface/viewport/theme combination.

    A combination that raises is recorded as a runner-error structural
    failure instead of aborting the wave — one broken surface must not
    hide the other nineteen results.
    """
    combinations = []
    for surface in STRUCTURAL_SPECS:
        for viewport in VIEWPORTS:
            for theme in THEMES:
                try:
                    result = inspect_combination(
                        browser,
                        base_url=base_url,
                        surface=surface,
                        viewport=viewport,
                        theme=theme,
                        cookie_state=None if surface == "login" else cookie_state,
                        registry=registry,
                        captures=captures,
                        golden=golden,
                    )
                except Exception as exc:
                    result = {
                        "surface": surface,
                        "route": route_for_surface(surface),
                        "viewport": viewport,
                        "theme": theme,
                        "structural_failures": [
                            {
                                "kind": "runner-error",
                                "selector": "document",
                                "detail": f"{type(exc).__name__}: {exc}",
                                "bbox": None,
                            }
                        ],
                        "pixel_diffs": [],
                    }
                combinations.append(result)
    return combinations
