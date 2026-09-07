"""Playwright-container half of the post-deploy visual gate."""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Route, sync_playwright

from scripts.post_deploy_visual_browser_js import OVERLAY, PIXEL_DIFF
from scripts.post_deploy_visual_fixtures import FIXTURES, INERT_EVENT_SOURCE, RUN_TIMELINE
from scripts.post_deploy_visual_policy import (
    CHANNEL_DELTA_THRESHOLD,
    P0_EXIT_CODE,
    P2_EXIT_CODE,
    STRUCTURAL_SPECS,
    SURFACE_ROUTES,
    THEMES,
    VIEWPORTS,
    DiffRegion,
    attribute_surface,
    classify_stable_diff,
    decide_escalation,
    route_for_surface,
    unexpected_pixel_surfaces,
    validate_wave_id,
)
from tests.e2e._layout_assertions import structural_failures, wait_for_layout_settled


def _pixel_crops(surface: str, viewport: str) -> dict[str, str]:
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


def _load_cookie_state(cookie_file: str, base_url: str) -> dict[str, object]:
    path = Path(cookie_file)
    text = path.read_text()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
        return {"cookies": parsed["cookies"], "origins": []}
    if isinstance(parsed, list):
        return {"cookies": parsed, "origins": []}
    cookies = []
    host = urlparse(base_url).hostname
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if (line.startswith("#") and not http_only) or not line.strip():
            continue
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) == 7:
            domain, _include_subdomains, path_value, secure, expires, name, value = fields
            cookie: dict[str, object] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path_value,
                "secure": secure.upper() == "TRUE",
                "httpOnly": http_only,
            }
            if expires != "0":
                cookie["expires"] = int(expires)
            cookies.append(cookie)
        elif "=" in line and host:
            name, value = line.strip().split("=", 1)
            cookies.append({"name": name, "value": value, "domain": host, "path": "/"})
        else:
            raise ValueError("cookie file must be Playwright JSON, Netscape format, or name=value")
    if not cookies:
        raise ValueError("cookie file contains no cookies")
    return {"cookies": cookies, "origins": []}


def _fixture_for(path: str) -> object | None:
    if path == "/api/agents/1/run-timeline":
        return RUN_TIMELINE
    return FIXTURES.get(path)


def _guard_requests(page: Page, *, surface: str) -> None:
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
        fixture = _fixture_for(path) if fixture_surface else None
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


def _context(
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


def _ignore_selectors(surface: str, registry: dict[str, object]) -> tuple[str, ...]:
    common = cast(list[dict[str, str]], registry["common"])
    routes = cast(dict[str, list[dict[str, str]]], registry["routes"])
    return tuple(entry["selector"] for entry in [*common, *routes.get(surface, [])])


def _diff_regions(page: Page, actual: bytes, expected: bytes) -> tuple[int, list[DiffRegion]]:
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


def _write_overlay(path: Path, page: Page, png: bytes, regions: tuple[DiffRegion, ...]) -> None:
    encoded = page.evaluate(
        OVERLAY,
        {"png": base64.b64encode(png).decode("ascii"), "regions": [asdict(r) for r in regions]},
    )
    path.write_bytes(base64.b64decode(encoded))


def _capture_crop(
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
        frame_results.append(_diff_regions(page, actual, previous.read_bytes()))
    total_pixels = max(frame_results[0][0], frame_results[1][0])
    stable = classify_stable_diff(
        frame_results[0][1],
        frame_results[1][1],
        total_pixels=total_pixels,
    )
    if stable.stable_regions:
        _write_overlay(captures / f"{key}-diff.png", page, current[1], stable.stable_regions)
    return {
        "surface": key.rsplit("-", 2)[0],
        "status": "drift" if stable.drifted else "match",
        "drifted": stable.drifted,
        "changed_pixels": stable.changed_pixels,
        "total_pixels": total_pixels,
        "ratio": stable.ratio,
        "stable_regions": [asdict(region) for region in stable.stable_regions],
    }


def _inspect_combination(
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
    context = _context(browser, viewport=viewport, theme=theme, cookie_state=cookie_state)
    try:
        page = context.new_page()
        _guard_requests(page, surface=surface)
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
        for crop_surface, selector in _pixel_crops(surface, viewport).items():
            key = f"{crop_surface}-{viewport}-{theme}"
            result = _capture_crop(
                page,
                selector=selector,
                key=key,
                ready_selector=ready_selector,
                ignore_selectors=_ignore_selectors(surface, registry),
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


def _demo(browser: Browser, base_url: str, captures: Path) -> dict[str, object]:
    context = _context(browser, viewport="narrow", theme="light", cookie_state=None)
    try:
        page = context.new_page()
        browser_errors: list[str] = []
        page.on(
            "console",
            lambda message: browser_errors.append(f"console {message.type}: {message.text}"),
        )
        page.on("pageerror", lambda error: browser_errors.append(f"pageerror: {error}"))
        page.on(
            "requestfailed",
            lambda request: browser_errors.append(
                f"requestfailed: {request.url}: {request.failure}"
            ),
        )
        _guard_requests(page, surface="home")
        page.goto(base_url, wait_until="domcontentloaded")
        ready = cast(str, STRUCTURAL_SPECS["home"]["ready"])
        try:
            wait_for_layout_settled(page, ready, timeout_ms=20_000)
        except Exception:
            print(
                json.dumps(
                    {
                        "demo_debug_url": page.url,
                        "demo_debug_body": page.locator("body").inner_text()[:1000],
                        "demo_debug_errors": browser_errors[-20:],
                    },
                    sort_keys=True,
                )
            )
            raise
        page.evaluate(
            """() => {
              const style = document.createElement('style'); style.id = 'ava-demo-overflow-style';
              style.nonce = document.querySelector('[nonce]')?.nonce || '';
              style.textContent = '#ava-demo-overflow{position:absolute;left:0;top:0;width:520px;height:2px}';
              document.head.appendChild(style);
              const node = document.createElement('div'); node.id = 'ava-demo-overflow';
              document.body.appendChild(node);
            }"""
        )
        overflow_red = structural_failures(page)
        wait_for_layout_settled(page, ready)
        page.screenshot(path=captures / "demo-overflow-red.png", animations="disabled")
        page.locator("#ava-demo-overflow").evaluate(
            "node => { node.remove(); document.querySelector('#ava-demo-overflow-style').remove(); }"
        )
        overflow_green = structural_failures(page)

        page.evaluate(
            """() => {
              const target = document.querySelector('[data-testid="composer-input"]');
              const rect = target.getBoundingClientRect(); const node = document.createElement('div');
              const style = document.createElement('style'); style.id = 'ava-demo-overlay-style';
              style.nonce = document.querySelector('[nonce]')?.nonce || '';
              style.textContent = `#ava-demo-overlay{position:fixed;z-index:2147483647;
                left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;background:red}`;
              document.head.appendChild(style); node.id = 'ava-demo-overlay';
              document.body.appendChild(node);
            }"""
        )
        overlay_red = structural_failures(
            page, control_selectors=("[data-testid='composer-input']",)
        )
        wait_for_layout_settled(page, ready)
        page.screenshot(path=captures / "demo-overlay-red.png", animations="disabled")
        page.locator("#ava-demo-overlay").evaluate(
            "node => { node.remove(); document.querySelector('#ava-demo-overlay-style').remove(); }"
        )
        overlay_green = structural_failures(
            page, control_selectors=("[data-testid='composer-input']",)
        )
        wait_for_layout_settled(page, ready)
        page.screenshot(path=captures / "demo-green.png", animations="disabled")
        passed = (
            any(item["kind"] == "horizontal-overflow" for item in overflow_red)
            and not overflow_green
            and any(item["kind"] == "occluded-control" for item in overlay_red)
            and not overlay_green
        )
        return {
            "passed": passed,
            "overflow": {"red": overflow_red, "green": overflow_green},
            "overlay": {"red": overlay_red, "green": overlay_green},
        }
    finally:
        context.close()


def _load_gate_state(output_root: Path) -> tuple[Path, dict[str, Any], Path]:
    state_path = output_root / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise TypeError("visual gate state must be a JSON object")
    golden_sha = state.get("golden_sha")
    golden_id = "_unaccepted" if golden_sha is None else validate_wave_id(golden_sha)
    return state_path, state, output_root / "golden" / golden_id / "captures"


def _classify_pixel_diffs(
    diffs: list[dict[str, object]], changed_paths: list[str]
) -> tuple[str, ...]:
    """Mark each drifted crop and return the surfaces that may escalate.

    ``baseline-missing`` crops are a setup state, never drift: they keep a
    visible classification in ``probes.json`` but stay out of escalation.
    """
    for diff in diffs:
        if diff["status"] == "baseline-missing":
            diff["classification"] = "baseline-missing"
            diff["matched_paths"] = []
            continue
        attribution = attribute_surface(cast(str, diff["crop_surface"]), changed_paths)
        diff["classification"] = "expected" if attribution.expected else "unexpected"
        diff["matched_paths"] = list(attribution.matched_paths)
    return unexpected_pixel_surfaces(diffs)


def run_browser_gate(
    *,
    base_url: str,
    wave_sha: str,
    output_root: Path,
    input_file: Path,
    cookie_file: Path | None,
    demo: bool,
) -> int:
    """Capture the matrix, persist artifacts, and return the severity exit code."""
    wave_dir = output_root / wave_sha
    captures = wave_dir / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    input_metadata = json.loads(input_file.read_text())
    state_path, state, golden = _load_gate_state(output_root)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            if demo:
                evidence = _demo(browser, base_url, captures)
                _write_artifact(wave_dir / "probes.json", {"demo": evidence})
                _write_artifact(
                    wave_dir / "meta.json",
                    {
                        "wave_sha": wave_sha,
                        "kind": "demo",
                        "captured_at": datetime.now(UTC).isoformat(),
                    },
                )
                print(
                    json.dumps(
                        {"result": "demo", "passed": evidence["passed"], "artifacts": str(wave_dir)}
                    )
                )
                return 0 if evidence["passed"] else P0_EXIT_CODE

            if cookie_file is None:
                raise ValueError("container run requires the mounted cookie file")
            cookie_state = _load_cookie_state(str(cookie_file), base_url)
            registry = json.loads(
                Path("/workspace/scripts/post_deploy_visual_known_ignores.json").read_text()
            )
            if registry["version"] != 1:
                raise ValueError("unsupported known-ignore registry version")
            combinations = []
            for surface in STRUCTURAL_SPECS:
                for viewport in VIEWPORTS:
                    for theme in THEMES:
                        try:
                            result = _inspect_combination(
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
        finally:
            browser.close()

    structural = [
        {
            **failure,
            "surface": entry["surface"],
            "viewport": entry["viewport"],
            "theme": entry["theme"],
        }
        for entry in combinations
        for failure in cast(list[dict[str, object]], entry["structural_failures"])
    ]
    pixel_diffs = [
        diff
        for entry in combinations
        for diff in cast(list[dict[str, object]], entry["pixel_diffs"])
        if diff["drifted"]
    ]
    changed_paths = cast(list[str], input_metadata["changed_paths"])
    unexpected = _classify_pixel_diffs(pixel_diffs, changed_paths)
    decision = decide_escalation(
        len(structural),
        unexpected,
        cast(dict[str, int], state.get("unexpected_wave_counts", {})),
        deployment_wave=bool(input_metadata["deployment_wave"]),
    )
    probes = {
        "severity": decision.severity,
        "structural_failures": structural,
        "unexpected_pixel_surfaces": unexpected,
        "matrix": combinations,
    }
    metadata = {
        **input_metadata,
        "wave_sha": wave_sha,
        "captured_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "playwright_image": "mcr.microsoft.com/playwright/python:v1.59.0-noble",
        "matrix": {
            "surfaces": list(STRUCTURAL_SPECS),
            "routes": SURFACE_ROUTES,
            "viewports": VIEWPORTS,
            "themes": THEMES,
        },
    }
    _write_artifact(wave_dir / "probes.json", probes)
    _write_artifact(wave_dir / "meta.json", metadata)
    state.update(
        {
            "gateway_started_at": input_metadata["gateway_started_at"],
            "last_wave_sha": wave_sha,
            "unexpected_wave_counts": decision.next_counts,
        }
    )
    _write_artifact(state_path, state)
    summary = {
        "severity": decision.severity,
        "structural_failure_count": len(structural),
        "unexpected_pixel_surfaces": unexpected,
        "artifacts": str(wave_dir),
    }
    if decision.severity == "P0":
        print(f"AVA_VISUAL_P0={json.dumps(summary, sort_keys=True)}")
    print(f"AVA_VISUAL_RESULT={json.dumps(summary, sort_keys=True)}")
    if decision.severity == "P0":
        return P0_EXIT_CODE
    if decision.severity == "P2":
        return P2_EXIT_CODE
    return 0


def _write_artifact(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
