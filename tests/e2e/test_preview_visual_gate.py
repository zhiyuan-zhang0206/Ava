"""CI blocking gate: the shared five-surface matrix against committed goldens.

Runs the same capture matrix as the post-deploy visual gate (five surfaces x
two viewports x two themes) against the production frontend build served by
the e2e stack, then compares the static crops with the committed golden
captures. Drift fails the test — an intentional UI change must re-mint the
goldens on the same ubuntu runner via the visual-baselines workflow before the
PR can merge (generation and comparison share one rendering environment).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Browser

from scripts.post_deploy_visual_check import _expected_capture_names
from scripts.post_deploy_visual_matrix import load_ignore_registry, run_matrix
from tests.e2e._ports import FRONTEND_URL

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = Path(__file__).parent / "__snapshots__" / "preview-gate"
CAPTURES_DIR = GOLDEN_ROOT / "captures"
META_PATH = GOLDEN_ROOT / "meta.json"
REFRESH_ENV = "PREVIEW_GATE_REFRESH"
REGISTRY_PATH = REPO_ROOT / "scripts" / "post_deploy_visual_known_ignores.json"

MINT_HINT = (
    "mint them on an ubuntu runner via the visual-baselines workflow "
    "(workflow_dispatch on this PR head)"
)


def _structural_failures(
    combinations: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            **failure,
            "surface": entry["surface"],
            "viewport": entry["viewport"],
            "theme": entry["theme"],
        }
        for entry in combinations
        for failure in cast(list[dict[str, object]], entry["structural_failures"])
    ]


def _crop_results(combinations: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        diff
        for entry in combinations
        for diff in cast(list[dict[str, object]], entry["pixel_diffs"])
    ]


def _mint_goldens(
    browser: Browser,
    structural: list[dict[str, object]],
    captures: Path,
    *,
    golden_root: Path = GOLDEN_ROOT,
) -> None:
    """Turn the current captures into the committed golden set.

    Same acceptance rule as the deployment gate's --accept-wave: a run with
    structural failures can never become the golden.
    """
    assert not structural, (
        "preview visual gate: refusing to mint goldens from a structurally "
        f"broken run: {structural}"
    )
    present = {path.name for path in captures.glob("*-current-[12].png")}
    missing = sorted(_expected_capture_names() - present)
    assert not missing, f"preview visual gate: incomplete capture set, missing {missing}"
    captures_dir = golden_root / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(captures.glob("*-current-[12].png")):
        target = captures_dir / source.name.replace("-current-", "-golden-")
        shutil.copy2(source, target)
    (golden_root / "meta.json").write_text(
        json.dumps(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "browser_engine": f"playwright-chromium {browser.version}",
                "runner": platform.platform(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.idle_restart_silent:build")
def test_preview_visual_gate(
    playwright_browser: Browser, frontend_proc: None, gateway_proc: str, tmp_path: Path
) -> None:
    """Five-surface matrix against the committed preview goldens.

    ``PREVIEW_GATE_REFRESH=1`` turns the test into a mint run (CI runner only,
    never a dev machine): current captures replace the goldens instead of being
    compared, guarded by the same structural-clean rule as --accept-wave.
    """
    registry = load_ignore_registry(REGISTRY_PATH)
    captures = tmp_path / "captures"
    minting = os.environ.get(REFRESH_ENV) == "1"
    assert not minting or sys.platform == "linux", (
        "preview-gate mint runs only on the ubuntu CI runner — generation and "
        "comparison must share one rendering environment"
    )
    golden = tmp_path / "empty-golden" if minting else CAPTURES_DIR
    combinations = run_matrix(
        playwright_browser,
        base_url=FRONTEND_URL,
        cookie_state=None,
        registry=registry,
        captures=captures,
        golden=golden,
    )
    structural = _structural_failures(combinations)
    if minting:
        _mint_goldens(playwright_browser, structural, captures)
        return
    crops = _crop_results(combinations)
    missing = sorted(
        {str(crop["surface"]) for crop in crops if crop["status"] == "baseline-missing"}
    )
    drifted = [(crop["surface"], crop["ratio"]) for crop in crops if crop["status"] == "drift"]
    assert not structural, (
        "preview visual gate: structural failures — "
        f"{[(f['surface'], f['viewport'], f['theme'], f['kind'], f['selector']) for f in structural]}"
    )
    assert not missing, (
        f"preview visual gate: golden captures missing for crops {missing} "
        f"under {CAPTURES_DIR} — {MINT_HINT}"
    )
    assert not drifted, (
        "preview visual gate: pixel drift on crops "
        f"{[(surface, f'{ratio:.3%}') for surface, ratio in drifted]} — "
        f"an intentional UI change must {MINT_HINT}; unexpected drift must be fixed"
    )
