"""Host browser pass for the post-deploy visual gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from playwright.sync_api import Browser, sync_playwright

from scripts.post_deploy_visual_matrix import (
    guard_requests,
    load_ignore_registry,
    new_matrix_context,
    run_matrix,
)
from scripts.post_deploy_visual_policy import (
    P0_EXIT_CODE,
    P2_EXIT_CODE,
    STRUCTURAL_SPECS,
    SURFACE_ROUTES,
    THEMES,
    VIEWPORTS,
    attribute_surface,
    decide_escalation,
    unexpected_pixel_surfaces,
    validate_wave_id,
)
from tests.e2e._layout_assertions import structural_failures, wait_for_layout_settled

REPO_ROOT = Path(__file__).resolve().parents[1]
IGNORE_REGISTRY_PATH = REPO_ROOT / "scripts" / "post_deploy_visual_known_ignores.json"


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


def _demo(browser: Browser, base_url: str, captures: Path) -> dict[str, object]:
    context = new_matrix_context(browser, viewport="narrow", theme="light", cookie_state=None)
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
        guard_requests(page, surface="home")
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
        # Repo-pinned Playwright Chromium, headless on the host — the bundled
        # engine (uv.lock playwright==1.59.0) keeps goldens comparable across
        # runs; never the system Chrome channel, which auto-updates under them.
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_engine = f"playwright-chromium {browser.version} (headless, host)"
            if demo:
                evidence = _demo(browser, base_url, captures)
                _write_artifact(wave_dir / "probes.json", {"demo": evidence})
                _write_artifact(
                    wave_dir / "meta.json",
                    {
                        "wave_sha": wave_sha,
                        "kind": "demo",
                        "captured_at": datetime.now(UTC).isoformat(),
                        "browser_engine": browser_engine,
                    },
                )
                print(
                    json.dumps(
                        {"result": "demo", "passed": evidence["passed"], "artifacts": str(wave_dir)}
                    )
                )
                return 0 if evidence["passed"] else P0_EXIT_CODE

            if cookie_file is None:
                raise ValueError("the browser pass requires a cookie file")
            cookie_state = _load_cookie_state(str(cookie_file), base_url)
            registry = load_ignore_registry(IGNORE_REGISTRY_PATH)
            combinations = run_matrix(
                browser,
                base_url=base_url,
                cookie_state=cookie_state,
                registry=registry,
                captures=captures,
                golden=golden,
            )
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
        "browser_engine": browser_engine,
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
